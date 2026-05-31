# cardputer-claude-mcp

**English** | [中文](README.zh-CN.md)

![Cardputer-Adv running cardputer-claude-mcp](docs/device.jpg)

Turn an [M5Stack Cardputer-Adv](https://docs.m5stack.com/en/core/Cardputer-Adv)
into a physical control surface for an AI coding agent.

A small MicroPython app on the handheld talks over BLE to a Mac-side MCP
bridge, so an agent (Claude Code, etc.) can:

- **notify / ask / confirm** — push banners, multiple-choice questions, and
  *physically-confirmed* approvals to the device,
- **usage** — show a live, always-on dashboard (today's spend + 5h/7d
  subscription utilization + battery) with a resident pixel-crab mascot,
- **gate the shell** — a Claude Code hook routes Bash commands *and* file
  edits to the device for approval before they run.

The headline trick is the **approval gate**: routine commands pass through a
whitelist, ordinary ones take a single **Enter** on the device, and
destructive ones (`rm -rf`, `git push`, `sudo`, edits to secrets) require a
sustained **hold-Y** gesture that prompt-injection can't synthesize. If the
device is away, it falls back to the normal terminal prompt — the Cardputer
is an *optional* gate, never a dependency.

| Ordinary action — one Enter | Destructive action — hold Y |
|:---:|:---:|
| ![ordinary approval](docs/approve.jpg) | ![danger confirm](docs/danger.jpg) |

## Layout

```
device/cardputer_mcp.py   MicroPython app: BLE GATT service, idle dashboard,
                          mascot animation, notify/ask/confirm/usage screens
mcp/server.py             Mac bridge: BLE owner + MCP tools + usage monitor +
                          the /hook/confirm route for the approval gate
mcp/auth.py               bearer-token auth for the HTTP transport
mac/                      launchd bridge daemon + the PreToolUse approval hook
mac/README.md             how to install & tune the Mac side  ← start here
```

## Quick start

1. **Bridge:** `cd mcp && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`,
   then run `mac/install_cardputer_bridge.sh` (see [`mac/README.md`](mac/README.md)).
2. **Device:** the app is large, so it ships as **`.mpy`** — compile with a
   matching `mpy-cross` and upload to `/flash/apps/`, deleting the `.py`
   (importing the source form OOM-crashes the UIFlow launcher). It runs
   inside the M5Stack UIFlow 2.0 launcher on a Cardputer-Adv.
3. **Approval gate:** register `mac/adv_confirm_hook.py` as a `PreToolUse`
   hook in `~/.claude/settings.json` (snippet in `mac/README.md`).

## Hardware notes (Cardputer-Adv)

- Audio is an **ES8311 codec + NS4150B amp**. `M5.Speaker.tone()` only sounds
  if the app's main loop calls **`M5.update()`** every iteration — without it
  the chirps are silent. The speaker amp is also muted while the 3.5 mm jack
  is engaged.
- The device app must be deployed as compiled `.mpy`, not source.

## Security

The bridge gates every HTTP request behind a bearer token; tokens live only
in `~/.config/cardputer-bridge/env` (never committed). The hold-Y confirm
gesture is the trust anchor for irreversible operations.

## License

[MIT](LICENSE) © 2026 neu
