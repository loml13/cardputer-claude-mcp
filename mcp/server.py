"""cardputer-mcp — pocket pager for AI agents, exposed over MCP.

Iteration 2: real BLE transport.

This process speaks stdio MCP to its client (Claude Code, Cursor, etc.)
and bridges tool calls to a Cardputer running the `cardputer_mcp.py`
device app over Bluetooth Low Energy.

Architecture:

  MCP client  ──stdio──▶  this process  ──BLE/bleak──▶  Cardputer
                                          (a5cd0001-…)

Tool calls become BLE writes; the device's acknowledgments resolve
in-flight asyncio Futures keyed by a per-call `id`. Disconnect events
fail all in-flight RPCs cleanly so the client gets a real error
rather than a hung tool.

Why FastMCP rather than the low-level Server API: this server's tool
surface is small (five tools at full build-out), each with a clean
typed signature. FastMCP's decorator style keeps the call-site code
close to the description text, which is what we'll iterate on most
often as we tune Claude's tool-selection behavior.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import shlex
import sys
import time
import urllib.request
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Literal, Optional

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError
from mcp.server.fastmcp import Context, FastMCP

from auth import label_for_authorization


# ---- protocol constants --------------------------------------------
#
# Keep these in sync with buddy/references/mcp_protocol.md and the
# device-side cardputer_mcp.py. If you change a UUID here, change it
# in all three places — there's no central manifest because grepping
# `a5cd` is faster than maintaining a config file.

SERVICE_UUID = "a5cd0001-c0de-4abe-9c1a-4d5e6f7a8b90"
RX_UUID = "a5cd0002-c0de-4abe-9c1a-4d5e6f7a8b90"  # host → device
TX_UUID = "a5cd0003-c0de-4abe-9c1a-4d5e6f7a8b90"  # device → host

# Device advertises as CardputerMCP_<6 hex>; we filter on the prefix.
NAME_PREFIX = "CardputerMCP_"

SCAN_TIMEOUT_S = 5.0
HELLO_TIMEOUT_S = 5.0
DEFAULT_RPC_TIMEOUT_S = 30.0

# When connection fails, suppress retries for this long so we don't
# stall every tool call with a fresh 5-second scan when the device
# is simply not in range. The MCP client will see a fast "unavailable"
# result instead.
FAIL_BACKOFF_S = 30.0

# Reconnect watchdog cadence (see _reconnect_watchdog). Fast right after a
# drop so the daemon links up quickly once the device launches its app, then
# slow once the device is plainly absent so a powered-off Cardputer never
# causes continuous scanning.
RECONNECT_FAST_S = 15.0
RECONNECT_SLOW_S = 60.0
RECONNECT_FAST_TRIES = 8  # ~2 min of fast retries before backing off

# Where we remember the device's BLE address after first successful
# connect. macOS hands out a per-host UUID rather than the real MAC —
# fine, it's stable across reboots of the laptop.
PAIR_CACHE_DIR = Path.home() / ".cardputer-mcp"
PAIR_CACHE_FILE = PAIR_CACHE_DIR / "paired.json"


def _log(line: str) -> None:
    """Write to stderr, which is what Claude Code surfaces in its MCP
    log pane. Never write to stdout — that's the MCP protocol stream
    and any non-protocol bytes there corrupt the transport."""
    print(f"[cardputer-mcp] {line}", file=sys.stderr, flush=True)


def _load_cached_address() -> Optional[str]:
    try:
        with open(PAIR_CACHE_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    addr = data.get("address")
    return addr if isinstance(addr, str) else None


def _save_cached_address(addr: str, name: str) -> None:
    try:
        PAIR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        PAIR_CACHE_FILE.write_text(
            json.dumps(
                {"address": addr, "name": name, "paired_at": int(time.time())}
            )
        )
    except OSError as e:
        _log(f"cache save failed: {e}")


# ---- bridge --------------------------------------------------------


class Bridge:
    """Manages one BLE connection to a Cardputer and correlates RPCs.

    The lifecycle is lazy: `ensure_connected()` is a no-op when the
    link is already up, otherwise it scans (or uses the cached
    address) and waits for the device's `hello` event before
    returning. Every tool call is one or more 20-byte RX writes
    matched to a TX ack by a generated `id` string.

    State machine, simplified:

        disconnected  ──ensure_connected──▶  scanning ──▶ connecting
                                                                │
                                                                ▼
        ◀──disconnected_callback──  connected  ◀── hello-received
    """

    def __init__(self) -> None:
        self.client: Optional[BleakClient] = None
        self.hello: Optional[dict] = None

        self._rx_buf = bytearray()
        self._pending: dict[str, asyncio.Future] = {}
        self._connect_lock = asyncio.Lock()
        self._hello_event = asyncio.Event()

        # Serializes the chunked write of ONE message so two concurrent
        # tool calls (now possible — many agents share one daemon) can't
        # interleave their 20-byte fragments on the RX characteristic and
        # corrupt the device's line reassembly. Scoped to the write loop
        # only — NOT the blocking wait — so the device's own pre-emption
        # (a confirm can pre-empt a pending ask) still works.
        self._write_lock = asyncio.Lock()

        # Suppress reconnect storms when the device is plainly absent —
        # without this, every tool call eats 5 s of scan time before
        # returning "unavailable", which makes Claude wait forever
        # when the user just hasn't powered the device on.
        self._last_fail_at: Optional[float] = None

    # --- connection lifecycle ---------------------------------------

    async def ensure_connected(self) -> None:
        if self.client and self.client.is_connected and self.hello is not None:
            return

        if (
            self._last_fail_at is not None
            and (time.monotonic() - self._last_fail_at) < FAIL_BACKOFF_S
        ):
            raise ConnectionError(
                f"device not found in last {int(FAIL_BACKOFF_S)} s "
                "(power-on Cardputer, launch the MCP app, then retry)"
            )

        async with self._connect_lock:
            # Re-check under the lock — another caller may have raced
            # us through scan/connect while we were waiting.
            if self.client and self.client.is_connected and self.hello is not None:
                return
            try:
                await self._connect()
                self._last_fail_at = None
            except Exception:
                self._last_fail_at = time.monotonic()
                raise

    async def _connect(self) -> None:
        addr = _load_cached_address()
        if addr:
            try:
                _log(f"connecting to cached address {addr}")
                await self._open_client(addr)
                return
            except (BleakError, asyncio.TimeoutError, ConnectionError) as e:
                _log(f"cached address failed ({e}); falling back to scan")

        addr, name = await self._scan()
        if addr is None:
            raise ConnectionError("no Cardputer-MCP device found in BLE scan")

        _log(f"connecting to discovered {name} ({addr})")
        await self._open_client(addr)
        _save_cached_address(addr, name or "")

    async def _scan(self) -> tuple[Optional[str], Optional[str]]:
        _log(f"scanning for {NAME_PREFIX}* ({SCAN_TIMEOUT_S} s)")
        try:
            # `return_adv=True` makes discover() return a dict
            # {addr: (device, AdvertisementData)} so we can read RSSI
            # and pick the strongest signal when multiple devices are
            # in range.
            discovered = await BleakScanner.discover(
                timeout=SCAN_TIMEOUT_S,
                return_adv=True,
            )
        except BleakError as e:
            _log(f"scan failed: {e}")
            return None, None

        candidates: list[tuple[int, str, str]] = []
        for addr, (device, adv) in discovered.items():
            name = device.name or (adv.local_name if adv else "") or ""
            # Two routes to discovery: name prefix (active scan) or
            # service UUID (passive scan). The device tries to put both
            # in its advertising payload but the radio sometimes
            # rejects rich payloads — see the cascade fallback in
            # `_advertise` on the device side.
            adv_uuids = [str(u).lower() for u in (adv.service_uuids or [])] if adv else []
            if name.startswith(NAME_PREFIX) or SERVICE_UUID in adv_uuids:
                rssi = adv.rssi if (adv and adv.rssi is not None) else -127
                candidates.append((rssi, addr, name or "Cardputer"))

        if not candidates:
            return None, None

        # Strongest RSSI wins. If two devices have the same RSSI, the
        # tuple comparison falls through to address, which is arbitrary
        # but stable — fine for a tiebreaker we don't expect to hit.
        candidates.sort(reverse=True)
        _, addr, name = candidates[0]
        return addr, name

    async def _open_client(self, addr: str) -> None:
        # Tear down any prior client. We've seen bleak hold a "live"
        # client reference that returns is_connected=False but still
        # refuses a new connect() until explicitly disconnected.
        if self.client is not None:
            with suppress(Exception):
                await self.client.disconnect()

        self._rx_buf = bytearray()
        # Fail any RPCs that were somehow still pending from a prior
        # connection — they'd never resolve against a fresh peer.
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("reconnecting"))
        self._pending.clear()
        self.hello = None
        self._hello_event.clear()

        self.client = BleakClient(
            addr, disconnected_callback=self._on_disconnect_sync
        )
        await self.client.connect()
        await self.client.start_notify(TX_UUID, self._on_tx)

        # The device sends a `hello` event a moment after the central
        # subscribes to TX. If it doesn't arrive within HELLO_TIMEOUT_S
        # we're probably talking to something that looks like our
        # service but isn't (or an old firmware that doesn't speak the
        # current protocol). Abort the connection so the next call
        # retries cleanly.
        try:
            await asyncio.wait_for(
                self._hello_event.wait(), timeout=HELLO_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            with suppress(Exception):
                await self.client.disconnect()
            raise ConnectionError(
                "device connected but didn't send hello within "
                f"{HELLO_TIMEOUT_S} s (wrong firmware?)"
            )

        caps = (self.hello or {}).get("caps") or []
        _log(f"connected; caps={caps}; mtu={(self.hello or {}).get('mtu')}")

    def _on_disconnect_sync(self, _client: BleakClient) -> None:
        # bleak calls this synchronously from its own thread/loop.
        # Resolve in-flight futures with an error so the tools return
        # promptly rather than hanging on their wait_for(). Future
        # callbacks scheduled via set_exception run on the loop that
        # owns the future, so this is safe across threads.
        _log("BLE disconnected")
        self.hello = None
        self._hello_event.clear()
        for mid, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(
                    ConnectionError("device disconnected mid-call")
                )
        self._pending.clear()

    # --- inbound stream ---------------------------------------------

    def _on_tx(self, _char, data: bytearray) -> None:
        """Called by bleak whenever the device pushes bytes on TX.

        TX is chunked at 20 bytes by the device to stay under the
        default ATT MTU; we accumulate until we see a `\\n` and then
        parse one JSON object per line.
        """
        self._rx_buf.extend(data)
        while b"\n" in self._rx_buf:
            line, _, rest = self._rx_buf.partition(b"\n")
            self._rx_buf = bytearray(rest)
            try:
                msg = json.loads(line.decode())
            except (ValueError, UnicodeError) as e:
                _log(f"bad TX line: {e!r} raw={bytes(line)!r}")
                continue
            self._dispatch(msg)

    def _dispatch(self, msg: dict) -> None:
        if "event" in msg:
            ev = msg["event"]
            if ev == "hello":
                self.hello = msg
                self._hello_event.set()
            elif ev == "heartbeat":
                # Heartbeats are advisory in iter 2. Iter 3+ will use
                # them for battery display and DND-state propagation.
                pass
            else:
                _log(f"unknown event: {ev}")
            return

        if "ack" in msg:
            mid = msg.get("id")
            if not isinstance(mid, str):
                # Hello/heartbeat won't have ids, but a malformed ack
                # without one is a protocol error from the device.
                _log(f"ack without id: {msg!r}")
                return
            if msg.get("pending"):
                # Delivery confirmation — the device has received the
                # request but the resolution will arrive later (after
                # user input or timeout). Don't resolve the future yet.
                return
            fut = self._pending.pop(mid, None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
            return

        _log(f"unknown TX shape: {msg!r}")

    # --- outbound RPC ------------------------------------------------

    async def send(
        self,
        cmd: str,
        payload: dict,
        rpc_timeout_s: float = DEFAULT_RPC_TIMEOUT_S,
        agent: str = "mcp-client",
    ) -> dict:
        """Send one command, await its ack. Returns the ack dict.

        On no-connection / write-fail / timeout, returns a synthetic
        ack with `ok: false` and an `err` so the tool layer can map
        cleanly to a user-visible string without bubbling exceptions.
        """
        try:
            await self.ensure_connected()
        except ConnectionError as e:
            return {"ack": cmd, "ok": False, "err": f"unavailable: {e}"}
        assert self.client is not None

        # Capability gate: tools the device doesn't advertise in
        # `hello.caps` short-circuit here without ever hitting the
        # radio. Older firmware staying compatible with newer tools
        # is the whole reason we negotiate caps.
        caps = (self.hello or {}).get("caps") or []
        if caps and cmd not in caps and cmd not in ("ping", "cancel"):
            return {
                "ack": cmd,
                "ok": False,
                "err": f"device firmware does not advertise '{cmd}'",
            }

        mid = uuid.uuid4().hex[:8]
        msg = {"cmd": cmd, "id": mid, "agent": agent, **payload}
        data = (json.dumps(msg) + "\n").encode()

        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[mid] = fut

        try:
            mtu = (self.hello or {}).get("mtu") or 20
            async with self._write_lock:
                for i in range(0, len(data), mtu):
                    await self.client.write_gatt_char(
                        RX_UUID, data[i : i + mtu], response=False
                    )
        except (BleakError, OSError) as e:
            self._pending.pop(mid, None)
            return {"ack": cmd, "ok": False, "err": f"ble write failed: {e}"}

        try:
            return await asyncio.wait_for(fut, timeout=rpc_timeout_s)
        except asyncio.TimeoutError:
            self._pending.pop(mid, None)
            # Best-effort: tell the device to abandon the request so
            # the LCD doesn't sit on a stale question.
            with suppress(Exception):
                cancel = (
                    json.dumps({"cmd": "cancel", "id": uuid.uuid4().hex[:8], "target_id": mid})
                    + "\n"
                ).encode()
                async with self._write_lock:
                    for i in range(0, len(cancel), mtu):
                        await self.client.write_gatt_char(
                            RX_UUID, cancel[i : i + mtu], response=False
                        )
            return {"ack": cmd, "ok": False, "err": "rpc timeout"}
        except ConnectionError as e:
            return {"ack": cmd, "ok": False, "err": str(e)}


# ---- MCP surface ---------------------------------------------------

bridge = Bridge()
mcp = FastMCP("cardputer")

# Populated by build_http_app() in HTTP mode: maps bearer token -> agent
# label. Read by _agent_label() so the device banner can show *which*
# agent is asking. Empty in stdio mode (there's no HTTP request to read a
# token from), where the label falls back to "local".
_TOKEN_MAP: dict[str, str] = {}


def _agent_label(ctx) -> str:
    """Resolve the requesting agent's banner label from its bearer token.

    The label is derived from WHICH token authenticated (mapped in
    `_TOKEN_MAP`), not from anything the caller can put in the tool
    arguments — so a misled or injected agent can't forge its own
    identity on the device's `ask`/`confirm` screen. stdio mode (no HTTP
    request) resolves to "local".
    """
    if ctx is None:
        return "local"
    req = getattr(getattr(ctx, "request_context", None), "request", None)
    if req is None:
        return "local"
    label = label_for_authorization(req.headers.get("authorization"), _TOKEN_MAP)
    return label or "agent"


@mcp.tool()
async def notify(
    ctx: Context,
    title: str,
    body: str = "",
    urgency: Literal["info", "warn", "crit"] = "info",
) -> str:
    """Display a non-blocking notification on the user's Cardputer.

    The Cardputer is a credit-card-sized handheld device the user
    carries with them. Use this tool when you want the user to glance
    at something — a status update, a result, a heads-up — without
    interrupting their main screen.

    Returns once the notification is shown on the device. The
    Cardputer LCD is 240×135 pixels, so keep `title` to ~20 characters
    and `body` to ~3 short lines. `urgency` controls the alert sound:
    'info' is a soft chirp, 'warn' is a louder double-beep, 'crit'
    is an urgent triple-beep. Prefer 'info' for most uses; reserve
    'crit' for things the user needs to react to within seconds.

    Do not call this in rapid succession — agents that spam
    notifications get muted by the device's per-agent rate limit
    (roughly 1 per 60 s) in a later iteration. Returns 'shown',
    'unavailable: <reason>', or 'failed: <reason>'.
    """
    title = title[:64]
    body = body[:240]
    # Notify is non-blocking on the device, so the RPC should resolve
    # within milliseconds. 10 s is generous slack for radio + device
    # render — if it exceeds that something is wrong.
    result = await bridge.send(
        "notify",
        {"title": title, "body": body, "urgency": urgency},
        rpc_timeout_s=10,
        agent=_agent_label(ctx),
    )
    if result.get("ok"):
        return "shown"
    if result.get("dnd"):
        # Device is in Do Not Disturb; a non-critical notify was
        # suppressed. Surface it so the agent knows it wasn't seen.
        return "dnd"
    err = result.get("err", "unknown")
    if err.startswith("unavailable"):
        return err
    return f"failed: {err}"


@mcp.tool()
async def ask(
    ctx: Context,
    question: str,
    choices: list[str],
    timeout_s: int = 60,
) -> str:
    """Ask the user a multiple-choice question on their Cardputer.

    BLOCKING — returns once the user presses a number key (1–4) on
    the device's QWERTY keyboard, or after `timeout_s` seconds have
    elapsed. Use when you need a quick decision from the user and
    don't want to interrupt their main screen, especially if they
    might be away from their laptop.

    The Cardputer LCD is 240×135 pixels, so keep `question` short
    (~60 chars wraps to 2 lines) and provide 2–4 short choices (each
    ≤ ~32 chars). The user picks by pressing the digit that matches
    their choice; ESC on the device cancels. Returns one of:

      - the exact choice string the user selected
      - 'timeout' if the user didn't respond in `timeout_s` seconds
      - 'cancelled' if the user pressed ESC on the device, or if
         a follow-up cancel was requested
      - 'unavailable: <reason>' if the device is not connected

    Prefer this over blocking your assistant message with a question
    when the user might not be at their laptop. Do NOT use this for
    destructive operations — call the `confirm` tool (iter 3+)
    instead, which requires a physical hold-to-confirm gesture.
    """
    if len(choices) < 2:
        return "error: need at least 2 choices"
    if len(choices) > 4:
        return "error: at most 4 choices (LCD is small)"
    if timeout_s < 1 or timeout_s > 600:
        return "error: timeout_s must be between 1 and 600"

    # RPC timeout is the device's own input timeout + 10 s slack for
    # radio jitter and the device's grace window. Without slack the
    # host can race the device and return "rpc timeout" when the user
    # genuinely was about to answer.
    rpc_timeout = timeout_s + 10
    result = await bridge.send(
        "ask",
        {
            "question": question[:120],
            "choices": [str(c)[:32] for c in choices],
            "timeout_s": timeout_s,
        },
        rpc_timeout_s=rpc_timeout,
        agent=_agent_label(ctx),
    )

    if result.get("ok") and "choice" in result:
        return str(result["choice"])
    if result.get("timed_out"):
        return "timeout"
    if result.get("cancelled"):
        return "cancelled"
    if result.get("dnd"):
        return "dnd"
    err = result.get("err", "unknown")
    if err.startswith("unavailable"):
        return err
    return f"failed: {err}"


@mcp.tool()
async def confirm(
    ctx: Context,
    title: str,
    timeout_s: int = 30,
) -> str:
    """Demand physical confirmation from the user before executing a
    destructive operation.

    This tool is for IRREVERSIBLE actions only — production deploys,
    force pushes, DROP TABLE / DELETE without WHERE, unstaged-file
    deletions, financial transactions, paid API calls with large
    side effects, etc. The user must complete a sustained ~3-second
    physical gesture on the Cardputer's Y key: on the current firmware
    the hardware keyboard emits no auto-repeat, so the gesture is
    rapid Y tapping (the on-screen prompt says "TAP Y fast for 3s") —
    a progress bar fills as they tap and resets if they stop for more
    than ~300 ms. A single tap is not enough.

    The point is that no amount of tool-output content or prompt
    injection can synthesize a sustained burst of physical key events.
    If you're about to do something the user couldn't un-do in a
    minute, use this instead of trusting an `ask` or your own
    assistant-message confirmation.

    Returns one of:
      - 'confirmed' — user completed the ~3 s physical Y gesture
      - 'cancelled' — user pressed N or ESC on the device
      - 'timeout' — user did not respond within `timeout_s` seconds
      - 'unavailable: <reason>' — device not connected

    `title` should fit roughly 18 characters on the device's 240×135
    LCD ("FORCE PUSH origin/main" or "DROP customers"). Keep it
    declarative. The user reading this on a tiny screen must
    instantly recognize the operation.

    Do NOT use this for routine yes/no decisions — that's what
    `ask` is for. Do NOT call this rapidly; every invocation demands
    a deliberate 3-second physical gesture, which is exhausting if
    abused. Reserve this for the handful of actions per session
    where wrong = bad.
    """
    title = title[:64]
    if timeout_s < 5 or timeout_s > 120:
        return "error: timeout_s must be between 5 and 120"

    # RPC timeout is the device's own deadline + slack for radio
    # jitter and the device's hold-detection grace window. Without
    # slack the host can race the device and report rpc-timeout
    # while the user is mid-hold.
    rpc_timeout = timeout_s + 10
    result = await bridge.send(
        "confirm",
        {"title": title, "danger": True, "timeout_s": timeout_s},
        rpc_timeout_s=rpc_timeout,
        agent=_agent_label(ctx),
    )

    if result.get("ok") and result.get("confirmed"):
        # We surface the recorded hold duration to encourage tools
        # that want to log it — most callers will just check the
        # 'confirmed' prefix and move on.
        hold_ms = result.get("hold_ms", 0)
        return f"confirmed (held {hold_ms} ms)"
    if result.get("cancelled"):
        return "cancelled"
    if result.get("timed_out"):
        return "timeout"
    err = result.get("err", "unknown")
    if err.startswith("unavailable"):
        return err
    return f"failed: {err}"


# ---- usage monitor (ambient dashboard on the device idle screen) ----
#
# A background task owned by the daemon (which is the single BLE owner)
# periodically reads `ccusage` and pushes a `usage` frame to the device,
# so the Cardputer's idle screen shows live Claude spend without any
# agent having to call a tool. Self-contained: no external cron, no
# second BLE central. Disabled by setting the interval <= 0.

USAGE_INTERVAL_S = int(os.environ.get("CARDPUTER_USAGE_INTERVAL") or "1800")
# `--offline` makes ccusage use its bundled pricing table instead of
# fetching LiteLLM's pricing JSON over the network on every run — that
# fetch can stall 20-30 s behind a slow proxy. Invoking the installed
# binary directly (not `npx`) avoids npx's per-run resolution overhead,
# which was even slower. Override via CARDPUTER_CCUSAGE_CMD (e.g. an
# absolute path when the binary isn't on the daemon's PATH).
USAGE_CCUSAGE_CMD = (
    os.environ.get("CARDPUTER_CCUSAGE_CMD") or "ccusage daily --json --offline"
)

# ccusage only sees local token counts, so it can't tell you how much of
# your *subscription* you've burned. The 5-hour / 7-day rolling limits the
# `/usage` slash command shows come from Anthropic's OAuth usage endpoint —
# the same one Claude Code calls. We read the live OAuth access token from
# the macOS Keychain (Claude Code stores + refreshes it there) and GET that
# endpoint to surface the real utilization percentages on the device.
USAGE_LIMITS_URL = "https://api.anthropic.com/api/oauth/usage"
# Item created by Claude Code; `-w` prints the secret (JSON blob) only.
USAGE_KEYCHAIN_SERVICE = "Claude Code-credentials"


def _fmt_tokens(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "0 tok"
    if n >= 1e6:
        return f"{n / 1e6:.1f}M token"
    if n >= 1e3:
        return f"{n / 1e3:.0f}K token"
    return f"{int(n)} token"


async def _compute_limits() -> Optional[dict]:
    """Return {'h5': int, 'd7': int} subscription utilization percentages.

    None on any failure (no token, expired token, endpoint hiccup) so the
    dashboard degrades to cost/tokens only instead of breaking the frame.
    """
    proc = await asyncio.create_subprocess_exec(
        "security", "find-generic-password", "-s", USAGE_KEYCHAIN_SERVICE, "-w",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    if proc.returncode != 0 or not out:
        _log("limits: no keychain token (security exited "
             f"{proc.returncode})")
        return None
    try:
        token = json.loads(out)["claudeAiOauth"]["accessToken"]
    except (ValueError, KeyError) as e:
        _log(f"limits: keychain blob unparseable: {e!r}")
        return None

    def _fetch() -> bytes:
        req = urllib.request.Request(
            USAGE_LIMITS_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read()

    try:
        body = await asyncio.get_event_loop().run_in_executor(None, _fetch)
        data = json.loads(body)
        return {
            "h5": int(round(float(data["five_hour"]["utilization"]))),
            "d7": int(round(float(data["seven_day"]["utilization"]))),
        }
    except Exception as e:  # urllib HTTPError, JSON, KeyError, timeout…
        _log(f"limits: usage endpoint failed: {e!r}")
        return None


async def _compute_usage() -> Optional[dict]:
    """Run ccusage and return preformatted dashboard strings, or None.

    Strings are formatted host-side so the device just blits them — it
    has no locale/float facilities worth using on a 240×135 LCD.
    """
    argv = shlex.split(USAGE_CCUSAGE_CMD)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
    if proc.returncode != 0 or not out:
        _log(f"ccusage exited {proc.returncode}; no usage")
        return None
    data = json.loads(out.decode())
    days = data.get("daily") if isinstance(data, dict) else data
    if not days:
        return None
    # ccusage labels each day's bucket in `period` ("2026-05-31"); match
    # today's date and fall back to the most recent bucket if today has no
    # activity yet. The grand total comes from the top-level `totals` block
    # when present (cheaper and exact), else summed from the daily rows.
    today = datetime.date.today().isoformat()
    cur = next((d for d in days if d.get("period") == today), days[-1])
    today_cost = float(cur.get("totalCost", 0) or 0)
    today_tok = cur.get("totalTokens", 0) or 0
    totals = data.get("totals") if isinstance(data, dict) else None
    if totals and totals.get("totalCost") is not None:
        total_cost = float(totals.get("totalCost") or 0)
    else:
        total_cost = sum(float(d.get("totalCost", 0) or 0) for d in days)
    frame = {
        "today_cost": f"${today_cost:,.2f}",
        "today_tok": _fmt_tokens(today_tok),
        "total": f"${total_cost:,.0f} / {len(days)}d",
    }
    # Real subscription limits (best-effort) — the 5h/7d gauges the device
    # shows on the right. Omitted keys just leave the gauges blank.
    limits = await _compute_limits()
    if limits:
        frame.update(limits)
    return frame


async def _usage_refresh_loop() -> None:
    if USAGE_INTERVAL_S <= 0:
        _log("usage monitor disabled (CARDPUTER_USAGE_INTERVAL <= 0)")
        return
    _log(f"usage monitor on; refreshing every {USAGE_INTERVAL_S}s")
    while True:
        try:
            usage = await _compute_usage()
            if usage:
                # send() connects on demand and is cheap when already
                # connected; a missing device just yields 'unavailable'
                # which we log and shrug off until the next tick.
                res = await bridge.send(
                    "usage", usage, rpc_timeout_s=10, agent="usage-monitor"
                )
                if not res.get("ok"):
                    _log(f"usage push skipped: {res.get('err')}")
        except Exception as e:
            _log(f"usage refresh error: {e!r}")
        await asyncio.sleep(USAGE_INTERVAL_S)


async def _reconnect_watchdog() -> None:
    """Keep the BLE link up without waiting for the usage tick.

    Idle and effectively free when connected (a periodic boolean check, no
    radio use). When disconnected it nudges the bridge to reconnect on a
    fast cadence so the daemon links up within ~15 s of the device launching
    its app — instead of waiting up to one usage interval — then backs off
    to a slow cadence if the device stays absent, so a powered-off Cardputer
    never causes continuous scanning.
    """
    _log(
        f"reconnect watchdog on (fast={int(RECONNECT_FAST_S)}s, "
        f"slow={int(RECONNECT_SLOW_S)}s)"
    )
    misses = 0
    while True:
        if bridge.client and bridge.client.is_connected and bridge.hello is not None:
            misses = 0
            await asyncio.sleep(RECONNECT_FAST_S)
            continue
        # Scan on the watchdog's own cadence rather than the tool-call
        # backoff (which exists to keep notify/ask/confirm latency low).
        bridge._last_fail_at = None
        try:
            await bridge.ensure_connected()
            misses = 0
            _log("reconnect watchdog: linked")
        except Exception:
            misses += 1
        await asyncio.sleep(
            RECONNECT_FAST_S if misses <= RECONNECT_FAST_TRIES else RECONNECT_SLOW_S
        )


# ---- HTTP transport (the cloud-bridge path, via an MCP tunnel) ------


def _allowed_hosts(
    host: str,
    port: int,
    tunnel_domain: Optional[str] = None,
    extra: Optional[list] = None,
) -> list:
    """Build the Host allow-list for the streamable-HTTP transport.

    The MCP transport does DNS-rebinding protection: it 421s any `Host`
    header not in this list. Its matcher only does EXACT matches and
    `host:*` port wildcards — it does NOT understand `*.domain` prefix
    wildcards, so we must enumerate the concrete hosts the daemon can
    actually receive:

    - loopback `127.0.0.1:port` / `localhost:port` — local Claude Code.
    - `cardputer.<tunnel_domain>` — what the tunnel's mcp-proxy forwards
      when it routes the `cardputer` subdomain (the bare domain too, in
      case a proxy preserves it differently).
    - `host.docker.internal[:port]` — in case the proxy rewrites Host to
      the upstream target instead of preserving the original.
    - `CARDPUTER_ALLOWED_HOSTS` (comma-separated) — escape hatch: if you
      ever see "Invalid Host header: X" in the proxy/daemon logs, add X
      here without a code change.

    This is defense-in-depth; the bearer token (checked first, in
    BearerAuthMiddleware) is the real gate.
    """
    allowed = [
        f"{host}:{port}",
        f"127.0.0.1:{port}",
        f"localhost:{port}",
        f"host.docker.internal:{port}",
        "host.docker.internal",
    ]
    if tunnel_domain:
        allowed += [tunnel_domain, f"cardputer.{tunnel_domain}"]
    env_extra = os.environ.get("CARDPUTER_ALLOWED_HOSTS")
    if env_extra:
        allowed += [h.strip() for h in env_extra.split(",") if h.strip()]
    if extra:
        allowed += list(extra)
    return allowed


def build_http_app(
    token_map: dict,
    host: str = "127.0.0.1",
    port: int = 9000,
    tunnel_domain: Optional[str] = None,
    extra_allowed_hosts: Optional[list] = None,
):
    """Build the streamable-HTTP ASGI app for the same three tools.

    Reuses the module-level `mcp`/`bridge` verbatim — only the transport
    changes. Two things are load-bearing and easy to get wrong:

    1. **Host allow-list** (see `_allowed_hosts`) — without the right
       entries the transport 421s tunneled requests via DNS-rebinding
       protection.
    2. **Bearer auth.** The tunnel does not authenticate to us, so we wrap
       the app in BearerAuthMiddleware (see auth.py). It's added last, so
       it runs FIRST (outermost) — auth before transport-security.

    `token_map` is stashed in the module global so the tools can resolve a
    request's token to an agent label for the device banner.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    from auth import BearerAuthMiddleware

    global _TOKEN_MAP
    _TOKEN_MAP = token_map

    mcp.settings.host = host
    mcp.settings.port = port
    # Keep DNS-rebinding host protection on (defense-in-depth). We leave
    # allowed_origins at its default ([]): server-side callers (Managed
    # Agents, the Messages API) send no Origin header, which always passes;
    # the matcher has no real wildcard for origins anyway, and bearer auth
    # is the actual gate.
    mcp.settings.transport_security = TransportSecuritySettings(
        allowed_hosts=_allowed_hosts(host, port, tunnel_domain, extra_allowed_hosts)
    )

    app = mcp.streamable_http_app()

    # Plain-HTTP confirm route for the local Bash PreToolUse hook. The hook
    # POSTs {title, timeout_s}; we drive the device's physical Y-hold
    # gesture through the same single BLE owner and return {"approved":
    # bool}. Inserted at the front so it resolves before the MCP mount, and
    # still behind BearerAuthMiddleware — the hook sends the local token.
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def _hook_confirm(request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        title = str(body.get("title") or "shell command")[:64]
        try:
            timeout_s = int(body.get("timeout_s") or 30)
        except (TypeError, ValueError):
            timeout_s = 30
        timeout_s = max(5, min(120, timeout_s))
        # danger=True -> hold-Y gesture (irreversible ops); danger=False ->
        # light single-Enter approve (ordinary commands). Default True so a
        # caller that omits it gets the stronger gate.
        danger = bool(body.get("danger", True))
        result = await bridge.send(
            "confirm",
            {"title": title, "danger": danger, "timeout_s": timeout_s},
            rpc_timeout_s=timeout_s + 10,
            agent="bash-hook",
        )
        return JSONResponse(
            {
                "approved": bool(result.get("ok") and result.get("confirmed")),
                "cancelled": bool(result.get("cancelled")),
                "timed_out": bool(result.get("timed_out")),
                "err": result.get("err"),
            }
        )

    app.routes.insert(0, Route("/hook/confirm", _hook_confirm, methods=["POST"]))
    app.add_middleware(BearerAuthMiddleware, token_map=token_map)
    return app


# ---- entrypoint -----------------------------------------------------


def main() -> None:
    _log(f"starting (pid={os.getpid()})")
    # Default transport is stdio, which is what `claude mcp add` (no
    # --transport) expects — the original local-only path. Setting
    # CARDPUTER_HTTP=1 switches to the streamable-HTTP daemon that an MCP
    # tunnel exposes to cloud agents AND that local Claude Code can reach
    # over loopback (`claude mcp add --transport http`). One BLE owner,
    # one gate, both transports.
    if os.environ.get("CARDPUTER_HTTP"):
        import uvicorn

        from auth import parse_token_map

        host = os.environ.get("CARDPUTER_HTTP_HOST", "127.0.0.1")
        port = int(os.environ.get("CARDPUTER_HTTP_PORT", "9000"))
        token_map = parse_token_map(os.environ.get("CARDPUTER_TOKENS"))
        tunnel_domain = os.environ.get("CARDPUTER_TUNNEL_DOMAIN")
        if not token_map:
            _log(
                "WARNING: CARDPUTER_TOKENS is empty — every HTTP request "
                "will be rejected 401 (fail-closed). Set token=label pairs."
            )
        app = build_http_app(
            token_map, host=host, port=port, tunnel_domain=tunnel_domain
        )
        _log(f"http transport on {host}:{port} (tunnel_domain={tunnel_domain})")

        async def _serve_http() -> None:
            # Start the usage monitor on the same loop that owns the BLE
            # bridge, then serve. We drive uvicorn via Server.serve()
            # (not uvicorn.run, which would spin up its own loop) so the
            # background task and the transport share one event loop.
            usage_task = asyncio.create_task(_usage_refresh_loop())
            watchdog_task = asyncio.create_task(_reconnect_watchdog())
            config = uvicorn.Config(app, host=host, port=port, log_config=None)
            try:
                await uvicorn.Server(config).serve()
            finally:
                usage_task.cancel()
                watchdog_task.cancel()

        asyncio.run(_serve_http())
        return

    # stdio (legacy / local fallback). Same pattern: launch the usage
    # monitor alongside the stdio transport on one shared loop.
    async def _serve_stdio() -> None:
        usage_task = asyncio.create_task(_usage_refresh_loop())
        watchdog_task = asyncio.create_task(_reconnect_watchdog())
        try:
            await mcp.run_stdio_async()
        finally:
            usage_task.cancel()
            watchdog_task.cancel()

    asyncio.run(_serve_stdio())


if __name__ == "__main__":
    main()
