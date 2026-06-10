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
destructive ones (`rm -rf`, `git push`, `sudo`, edits to secrets) take a
single **Y** press — a different key from the ordinary prompt's Enter, and a
physical keypress that prompt-injection can't synthesize. If the device is
away, it falls back to the normal terminal prompt — the Cardputer is an
*optional* gate, never a dependency.

| Ordinary action — press Enter | Destructive action — press Y |
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
2. **Device:** `device/cardputer_mcp.py` is **not standalone** — it deploys
   into the [cardputer-claude-os](https://github.com/dakshaymehta/cardputer-claude-os)
   UIFlow launcher bundle, which provides the app menu, NimBLE bring-up, and
   the matrix-keyboard driver that this app's `run()` relies on. Compile it to
   **`.mpy`** (matching `mpy-cross`), drop it into that bundle's `/flash/apps/`,
   and delete the `.py` (importing the source form OOM-crashes the launcher).
   This repo intentionally ships only the app, not that launcher framework.
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
in `~/.config/cardputer-bridge/env` (never committed). A physical Y press on
the device is the trust anchor for irreversible operations.

**BLE threat model.** The BLE link itself is unauthenticated (this NimBLE
build has no bonding), so a hostile neighbor in radio range could advertise
a fake `CardputerMCP_*` peripheral that acks every confirm. To close that
hole, provision a shared secret: write the same long random string to
`~/.cardputer-mcp/secret` (Mac) and `/flash/mcp_secret.txt` (device). The
firmware then signs confirmed acks with HMAC-SHA256 and the bridge rejects
any approval that doesn't verify. With no secret on either side, behavior
is the legacy unsigned mode — fine on a desk you trust, not in a shared
office.

**Headless sessions.** In contexts with no terminal to answer a fallback
prompt (env `LARK_CHANNEL=1` or `ADV_CONFIRM_HEADLESS=1`), an unreachable
device resolves light-tier approvals to *allow* and danger-tier ones to
*deny* — the gate never gets more permissive for risky commands just
because nobody is watching.

## Acknowledgements

- Built on [cardputer-claude-os](https://github.com/dakshaymehta/cardputer-claude-os) — the Cardputer ↔ Claude BLE bundle this extends.
- Usage dashboard inspired by [cardputer-claude-usage](https://github.com/chixi4/cardputer-claude-usage).
- Spend / token figures come from [ccusage](https://github.com/ryoppippi/ccusage).

## License

[MIT](LICENSE) © 2026 neu
