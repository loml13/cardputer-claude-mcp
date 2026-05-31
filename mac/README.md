# macOS side — bridge daemon + ADV approval gate

This folder sets up the Mac half of the Cardputer-MCP system:

1. **The bridge daemon** — a launchd agent that owns the BLE link to the
   Cardputer and serves the MCP tools (`notify` / `ask` / `confirm` /
   `usage`) over loopback HTTP, plus an ambient usage dashboard it pushes
   to the device.
2. **The approval gate** — a Claude Code `PreToolUse` hook that routes
   shell commands and file edits to the Cardputer for physical approval.

---

## 1. Bridge daemon

`install_cardputer_bridge.sh` installs `com.cardputer.bridge.plist` as a
per-user launchd agent that runs `../mcp/server.py` with the streamable-HTTP
transport on `127.0.0.1:9000`.

```bash
# one-time venv
cd ../mcp && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# first run writes a stub env file and exits
./install_cardputer_bridge.sh
# edit ~/.config/cardputer-bridge/env (set strong random tokens), then:
./install_cardputer_bridge.sh
```

The daemon:
- is a single BLE owner (no two centrals fighting over the device),
- auto-starts on login (`RunAtLoad`) and restarts on crash (`KeepAlive`),
- gates every HTTP request behind a bearer token (see `../mcp/auth.py`),
- pushes a live `usage` frame (today's spend + 5h/7d subscription
  utilization, read from `ccusage` and the OAuth usage endpoint) on a timer.

Config lives in `~/.config/cardputer-bridge/env` (user-only, never
committed): `CARDPUTER_TOKENS`, `CARDPUTER_USAGE_INTERVAL`,
`CARDPUTER_CCUSAGE_CMD`, etc.

Point local Claude Code at it:

```bash
claude mcp add --transport http cardputer \
  http://127.0.0.1:9000/mcp --header "Authorization: Bearer <local-token>"
```

---

## 2. Approval gate (`adv_confirm_hook.py`)

A `PreToolUse` hook that intercepts **Bash** and **file-edit**
(`Write` / `Edit` / `MultiEdit` / `NotebookEdit`) tool calls and asks for
approval **on the Cardputer** before they run. It talks to the bridge
daemon's `POST /hook/confirm` route, which drives the device gesture and
returns the verdict.

### Tiers

| Tier | What | Gesture on the ADV |
|------|------|--------------------|
| **Whitelist** | read-only commands (`ls`, `cat`, `grep`, `git status`, …) | none — passes straight through |
| **Light** | ordinary commands / file edits | a single **Enter** tap (bright 3-note chirp) |
| **Danger** | `rm -rf`, `git push`, `sudo`, secret/key/system paths, … | sustained **hold-Y** (~3 s, triple chirp) |

The danger gesture is injection-resistant on purpose: no amount of tool
output or prompt injection can synthesize a sustained burst of physical key
events. Risky patterns are checked **first**, so a chained command like
`ls && rm -rf x` is treated as danger.

### Graceful when the device is away

If the Cardputer is off / out of range / the daemon isn't running, the hook
returns `ask` — Claude Code's normal in-terminal `y/n` prompt — instead of
blocking you. **The ADV is an optional gate, never a dependency**; with only
a Mac, everything still works.

### Install

Register it in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|Write|Edit|MultiEdit|NotebookEdit",
        "hooks": [
          { "type": "command",
            "command": "python3 /absolute/path/to/mac/adv_confirm_hook.py" }
        ]
      }
    ]
  }
}
```

(Hook changes take effect on the next Claude Code session.)

### Tuning & kill-switches

Drop any of these next to the env file to override the built-in defaults
(one regex per line; `#` comments allowed):

- `~/.config/cardputer-bridge/safe_patterns.txt` — whitelist
- `~/.config/cardputer-bridge/risky_patterns.txt` — danger / hold-Y
- `~/.config/cardputer-bridge/sensitive_paths.txt` — file paths that make an
  edit "danger"

Pause the whole gate without editing settings:

- `export ADV_CONFIRM_DISABLED=1`, or
- `touch ~/.config/cardputer-bridge/hook_disabled` (delete to re-enable).

---

## Files

| File | Purpose |
|------|---------|
| `install_cardputer_bridge.sh` | two-phase installer for the launchd agent |
| `com.cardputer.bridge.plist` | launchd job template (`__PLACEHOLDERS__` filled by the installer) |
| `adv_confirm_hook.py` | the `PreToolUse` approval hook |
