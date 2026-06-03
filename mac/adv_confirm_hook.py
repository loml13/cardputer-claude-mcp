#!/usr/bin/env python3
"""Claude Code PreToolUse hook: route risky Bash commands to the Cardputer.

Wiring: registered in ~/.claude/settings.json as a PreToolUse hook with
matcher "Bash". Claude Code feeds the tool call as JSON on stdin; we decide
whether the command is "risky" (see RISKY_PATTERNS / the override file) and,
if so, push it to the ADV via the bridge daemon's /hook/confirm route. The
user approves with the physical ~3s Y-hold gesture on the device.

Policy (chosen by the user):
  - Scope: only risky commands are gated; everything else proceeds through
    Claude Code's normal permission flow (we emit no decision for those).
  - Gate when reachable: the device decides. Hold Y -> allow; press N/ESC
    or just ignore the prompt until it times out -> DENY.
  - Graceful when unreachable: if the ADV is off / not carried / the bridge
    daemon isn't running, we can't get a verdict, so we fall back to "ask"
    -> Claude Code's normal in-terminal y/n prompt. You're never locked out,
    and we never silently auto-run. Set ADV_CONFIRM_DISABLED=1 to bypass the
    hook entirely.

Decision protocol: we print a PreToolUse JSON decision on stdout. "allow"
skips the terminal prompt (the device WAS the prompt); "deny" blocks with a
reason. No output + exit 0 == defer to normal flow.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

ENV_PATH = os.path.expanduser("~/.config/cardputer-bridge/env")
PATTERNS_OVERRIDE = os.path.expanduser("~/.config/cardputer-bridge/risky_patterns.txt")
SAFE_OVERRIDE = os.path.expanduser("~/.config/cardputer-bridge/safe_patterns.txt")
SENSITIVE_OVERRIDE = os.path.expanduser("~/.config/cardputer-bridge/sensitive_paths.txt")

# File-editing tools we also gate (alongside Bash). The matcher in
# settings.json must list these too, or the hook never fires for them.
EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

# Other built-in tools with outside-world side effects. When the ADV is
# connected these route their would-be terminal confirmation to it; when it
# isn't, they fall back to the terminal. Read-only tools (Read/Grep/Glob/LS)
# are deliberately NOT in the matcher, so the hook never fires for them.
EFFECTFUL_TOOLS = {"WebFetch", "WebSearch", "Task", "KillShell"}

# MCP tools are matched via "mcp__.*" in settings.json. We can't know each
# server's semantics, so classify by the verb in the bare tool name: read-ish
# names defer silently (no buzz), everything else is treated as a write and
# routed to the ADV light confirm. Tune by editing this list.
_MCP_READ_VERB = re.compile(
    r"(^|_)(search|list|get|read|find|view|show|status|info|stats|health|"
    r"callers|callees|callee|context|explore|node|nodes|files|impact|query|"
    r"fetch|check|describe|resources|ls|dir|diff|log|grep|blame)(_|$)",
    re.IGNORECASE,
)

# Default risky patterns (case-insensitive regex, matched against the raw
# command string). Tune by dropping a risky_patterns.txt next to the env
# file — one regex per line, blank lines and #-comments ignored; that file,
# if present, REPLACES this list.
RISKY_PATTERNS = [
    r"\brm\s+(-\w*[rf]|--force|--recursive)",   # rm -rf / -f / -r
    r"\bsudo\b",
    r"\bgit\s+push\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\b",
    r"\bgit\s+branch\s+-D\b",
    r"\blaunchctl\s+\w*\s*(bootout|unload|remove)\b",
    r"\b(kill|killall|pkill)\b",
    r"\bdd\b",
    r"\bmkfs",
    r"\b(shutdown|reboot|halt)\b",
    r"\bchmod\s+(-R\b|\d*777)",
    r"\bchown\s+-R\b",
    r"\b(shred|truncate)\b",
    r"\.env(\.\w+)?\b",                          # touching .env / .env.local (secrets)
    r">\s*/dev/(?!null|std(out|err))",           # write to a real device (not /dev/null)
    r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba)?sh\b",   # curl … | sh
    r"\bdrop\s+table\b",
    r"\bdelete\s+from\b",
    r"\bnpm\s+publish\b",
    r":\s*\(\s*\)\s*\{",                         # fork bomb
]

# Whitelist: clearly read-only / inspection commands that pass straight
# through with no approval at all. Checked AFTER the risky list (so a
# chained risky command can't sneak past) and only for commands with no
# shell operators / effectful flags (see _is_safe). Override by dropping a
# safe_patterns.txt next to the env file (one regex per line; replaces
# this list). Anchored to the command start.
SAFE_PATTERNS = [
    r"^\s*(ls|pwd|cd|echo|cat|bat|head|tail|less|more|nl|column|wc|file|stat|tree|hexdump|xxd|od|strings)\b",
    r"^\s*(grep|egrep|fgrep|rg|ag|ack)\b",
    r"^\s*(find|fd|locate|which|type|whereis|whatis)\b",
    r"^\s*(whoami|id|date|uname|hostname|uptime|arch|sw_vers|locale|tty|groups)\b",
    r"^\s*(df|du|free|ps|env|printenv)\b",
    r"^\s*(basename|dirname|realpath|readlink|sort|uniq|cut|tr|diff|cmp|comm|join|paste)\b",
    r"^\s*(jq|yq|md5|md5sum|shasum|sha256sum|cksum)\b",
    r"^\s*git\s+(status|log|diff|show|branch|remote|rev-parse|ls-files|blame|describe|shortlog|reflog|tag|stash\s+list|config\s+--(get|list))\b",
    r"^\s*(python3?|node|deno|bun|ruby|go|cargo|rustc|java|gcc|clang|cmake|make)\s+(--?version|-V|version)\b",
    r"^\s*python3?\s+-m\s+py_compile\b",
    r"^\s*(npm|pnpm|yarn|pip3?|brew|gem)\s+(ls|list|--version|-v|view|info|outdated|--help)\b",
]

# Command contains shell chaining / redirection / substitution -> never
# auto-allowed (we can't vouch for what the second half does); falls to the
# light confirm instead.
_UNSAFE_SHELL = re.compile(r"[;&|<>`]|\$\(")
# Effectful flags that make an otherwise-safe command mutate things.
_EFFECTFUL = re.compile(r"(^|\s)(-exec(dir)?\b|-delete\b|-ok\b|-i\b|--in-place\b|-fprint)")

# Sensitive file paths: editing one demands the hold-Y danger gesture
# instead of the light Enter. Matched (case-insensitive) against the full
# target path. Override with sensitive_paths.txt next to the env file.
SENSITIVE_PATTERNS = [
    r"\.env(\.|$|/)",                            # .env, .env.local
    r"/\.ssh/", r"\bid_(rsa|ed25519|ecdsa|dsa)\b", r"authorized_keys", r"known_hosts",
    r"\.pem$", r"\.key$", r"\.p12$", r"\.keystore$", r"\.pfx$",
    r"/\.aws/", r"/\.gnupg/", r"/\.kube/", r"/\.docker/config",
    r"\.npmrc$", r"\.netrc$", r"\.pgpass$", r"\.pypirc$",
    r"(secret|credential|password|token)s?\b",
    r"^/etc/", r"^/usr/", r"^/System/", r"^/Library/",   # system paths
]

CONFIRM_TIMEOUT_S = 30      # device-side deadline for the approve gesture
HTTP_SLACK_S = 15           # client waits a bit longer than the device

# File kill-switch: if this exists, the hook defers everything (same as
# ADV_CONFIRM_DISABLED=1) — handy to pause gating without editing settings.
DISABLE_FLAG = os.path.expanduser("~/.config/cardputer-bridge/hook_disabled")


def _emit(decision: str, reason: str = "") -> None:
    """Print a PreToolUse permission decision and exit."""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,        # "allow" | "deny" | "ask"
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def _defer() -> None:
    """No opinion — let Claude Code's normal permission flow handle it."""
    sys.exit(0)


def _load_patterns(override_path: str, default: list) -> list:
    if os.path.isfile(override_path):
        out = []
        with open(override_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    out.append(line)
        if out:
            return out
    return default


def _matches_any(command: str, patterns: list) -> bool:
    for pat in patterns:
        try:
            if re.search(pat, command, re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def _is_risky(command: str) -> bool:
    return _matches_any(command, _load_patterns(PATTERNS_OVERRIDE, RISKY_PATTERNS))


def _is_safe(command: str) -> bool:
    """True for clearly read-only commands that should pass without asking.

    Conservative: no shell chaining/redirection/substitution, no effectful
    flags, and the leading command must be in the safe list. Risky-check
    runs first, so this only sees not-already-dangerous commands.
    """
    # Benign stderr redirects (2>/dev/null, 2>&1, >/dev/null) are extremely
    # common on read-only commands; strip them before the operator guard so
    # they don't bump an otherwise-safe command into the confirm tier.
    probe = re.sub(r"\d*>>?\s*/dev/null|\d*>&\d", " ", command)
    if _UNSAFE_SHELL.search(probe) or _EFFECTFUL.search(command):
        return False
    return _matches_any(command, _load_patterns(SAFE_OVERRIDE, SAFE_PATTERNS))


def _is_sensitive(path: str) -> bool:
    """True if editing this path should demand the hold-Y danger gesture."""
    return _matches_any(path, _load_patterns(SENSITIVE_OVERRIDE, SENSITIVE_PATTERNS))


def _edit_title(tool: str, tool_input: dict) -> tuple[str, str]:
    """Return (device_title, path) for a file-editing tool call."""
    path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    # Show the last two path components so the user knows which file
    # without the full absolute path eating the tiny screen.
    short = "/".join(path.rsplit("/", 2)[-2:]) if "/" in path else path
    verb = "Write" if tool == "Write" else "Edit"
    return f"{verb} {short}"[:48], path


def _is_mcp_readonly(tool: str) -> bool:
    """True if an mcp__server__name tool looks read-only (defer, no buzz)."""
    bare = tool.split("__")[-1]
    return bool(_MCP_READ_VERB.search(bare))


def _other_title(tool: str, tool_input: dict) -> str:
    """Short device label for a non-Bash, non-edit tool call (<=48 chars)."""
    if tool == "WebFetch":
        return f"WebFetch {tool_input.get('url', '')}"[:48]
    if tool == "WebSearch":
        return f"Search {tool_input.get('query', '')}"[:48]
    if tool == "Task":
        what = tool_input.get("description") or tool_input.get("subagent_type") or "agent"
        return f"Task {what}"[:48]
    if tool.startswith("mcp__"):
        return f"MCP {tool.split('__')[-1]}"[:48]
    return tool[:48]


def _read_local_token() -> str | None:
    """First token from CARDPUTER_TOKENS in the daemon env file."""
    try:
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line.startswith("CARDPUTER_TOKENS="):
                    raw = line.split("=", 1)[1].strip().strip('"').strip("'")
                    first = raw.split(",", 1)[0]
                    return first.split("=", 1)[0].strip() or None
    except OSError:
        pass
    return None


def _is_non_interactive() -> bool:
    """True when stderr isn't a tty — meaning we're running headless
    (lark-channel-bridge daemon, CI, `claude -p` pipe, etc.) and the
    terminal-prompt fallback can't reach a human. CLI mode → False.
    """
    try:
        return not os.isatty(2)
    except (AttributeError, OSError):
        return True


def _maybe_fail_open(decision: str, reason: str) -> tuple[str, str]:
    """Convert `ask` (terminal fallback) into `allow` when no terminal
    can answer — otherwise the deferral silently denies in bridge mode.
    Logs the original reason so the audit trail stays honest.
    """
    if decision == "ask" and _is_non_interactive():
        return "allow", f"non-interactive (no tty) — fail-open; {reason}"
    return decision, reason


def _ask_device(title: str, danger: bool) -> tuple[str, str]:
    """POST to the daemon's /hook/confirm. Returns (decision, reason).

    decision is a PreToolUse verdict: "allow" (approved on device), "deny"
    (device said no / ignored while reachable), or "ask" (couldn't reach the
    device, so defer to the normal terminal prompt instead of locking the
    user out). `danger` picks the gesture: hold-Y vs single Enter.

    In non-interactive contexts (e.g. inside lark-channel-bridge), the
    "ask" fallback is automatically converted to "allow" via
    `_maybe_fail_open` — no terminal to answer = silent deny otherwise.
    """
    token = _read_local_token()
    if not token:
        return _maybe_fail_open("ask", "no bridge token; falling back to terminal prompt")
    port = os.environ.get("CARDPUTER_HTTP_PORT", "9000")
    url = f"http://127.0.0.1:{port}/hook/confirm"
    payload = json.dumps(
        {"title": title, "timeout_s": CONFIRM_TIMEOUT_S, "danger": danger}
    ).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=CONFIRM_TIMEOUT_S + HTTP_SLACK_S) as r:
            data = json.loads(r.read())
    except urllib.error.URLError as e:
        # Daemon down / not listening — can't reach the gate, defer.
        return _maybe_fail_open("ask", f"ADV bridge unreachable ({e.reason}); asking in terminal")
    except Exception as e:
        return _maybe_fail_open("ask", f"ADV confirm error ({e}); asking in terminal")

    gesture = "held Y" if danger else "pressed Enter"
    if data.get("approved"):
        return "allow", f"approved on ADV ({gesture})"
    if data.get("cancelled"):
        return "deny", "denied on ADV (N/ESC)"
    if data.get("timed_out"):
        return "deny", "no response on ADV (you didn't approve in time)"
    # Daemon reachable but the device itself is offline/disconnected
    # ('unavailable: ...') or the RPC stalled — treat as unreachable and
    # fall back to the terminal rather than hard-denying.
    err = str(data.get("err") or "device unavailable")
    return _maybe_fail_open("ask", f"ADV {err}; asking in terminal")


def main() -> None:
    if os.environ.get("ADV_CONFIRM_DISABLED") or os.path.isfile(DISABLE_FLAG):
        _defer()

    try:
        event = json.load(sys.stdin)
    except Exception:
        _defer()

    tool = event.get("tool_name")
    tool_input = event.get("tool_input") or {}

    # File edits: sensitive paths (.env / keys / system dirs) demand the
    # hold-Y gesture; every other edit gets the light single-Enter approve.
    # No pass-through tier — when the ADV is around, all edits go through it;
    # when it isn't, _ask_device returns "ask" and the terminal handles it.
    if tool in EDIT_TOOLS:
        title, path = _edit_title(tool, tool_input)
        if not path:
            _defer()
        decision, reason = _ask_device(title, danger=_is_sensitive(path))
        _emit(decision, reason)

    if tool == "Bash":
        command = tool_input.get("command", "")
        if not command:
            _defer()

        # Bash, three tiers. Risky is checked FIRST so a chained dangerous
        # command (e.g. "ls && rm -rf x") can't slip through the whitelist.
        #   risky  -> ADV hold-Y    (irreversible ops)
        #   safe   -> pass through  (no prompt at all)
        #   else   -> ADV light Enter approve (ordinary commands)
        # Unreachable device falls back to the terminal in either ADV tier.
        title = command.strip().replace("\n", " ")[:48]
        if _is_risky(command):
            decision, reason = _ask_device(title, danger=True)
            _emit(decision, reason)
        if _is_safe(command):
            _emit("allow", "whitelisted read-only command")
        decision, reason = _ask_device(title, danger=False)
        _emit(decision, reason)

    # Other side-effecting tools: route the would-be terminal confirmation to
    # the ADV (single-Enter approve) when it's connected; _ask_device returns
    # "ask" -> terminal fallback when it isn't. MCP reads and any unrecognized
    # tool defer silently, so they never buzz the device.
    if tool and tool.startswith("mcp__"):
        # The cardputer MCP server IS the approval device. Gating its own
        # tools through itself is circular — notify would buzz to ask
        # permission to buzz, and confirm would nest a modal inside the
        # modal it's trying to raise — so always defer them.
        if tool.startswith("mcp__cardputer__"):
            _defer()
        if _is_mcp_readonly(tool):
            _defer()
        decision, reason = _ask_device(_other_title(tool, tool_input), danger=False)
        _emit(decision, reason)

    if tool in EFFECTFUL_TOOLS:
        decision, reason = _ask_device(_other_title(tool, tool_input), danger=False)
        _emit(decision, reason)

    # Unrecognized tool: no opinion, defer to Claude Code's normal flow.
    _defer()


if __name__ == "__main__":
    main()
