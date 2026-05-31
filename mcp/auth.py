"""Bearer-token auth for the streamable-HTTP transport of cardputer-mcp.

Why this exists: an MCP tunnel carries encrypted traffic to your upstream
MCP server but **does not authenticate to it** — upstream auth is the
operator's responsibility (see the MCP-tunnels docs). Without a check
here, anyone who learned your tunnel route (e.g. a leaked
`cardputer.<domain>/mcp` URL) could buzz — or worse, drive a `confirm`
banner on — the device in your pocket. So every HTTP request must carry a
known bearer token.

The token also doubles as a *trustworthy* agent-identity source: each
token maps to a short label (`claude-code`, `managed-agent`, `ci-bot`, …)
that we render on the device's `ask`/`confirm` banner so the user knows
*who* is asking before they hold Y. The label is derived from which token
authenticated, not from caller-supplied free text, so a misled agent
can't lie about its own identity on the danger screen.

The stdio transport (legacy/local fallback) doesn't go through this — it
runs as the local user over a private process pipe, like the original
build.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


def parse_token_map(raw: str | None) -> dict[str, str]:
    """Parse a ``"token=label,token2=label2"`` string into ``{token: label}``.

    This is the format used for the ``CARDPUTER_TOKENS`` environment
    variable so secrets and their human labels travel together. Blank
    input yields an empty map (which denies everything — fail closed).
    Malformed entries (no ``=``, or an empty token) are skipped rather
    than crashing the daemon at boot.
    """
    if not raw:
        return {}
    out: dict[str, str] = {}
    for pair in raw.split(","):
        if "=" not in pair:
            continue
        token, _, label = pair.partition("=")
        token = token.strip()
        label = label.strip()
        if not token:
            continue
        out[token] = label or "agent"
    return out


def label_for_authorization(
    header: str | None, token_map: dict[str, str]
) -> str | None:
    """Return the agent label for an ``Authorization`` header, or ``None``.

    ``None`` means "unauthorized" — the middleware turns that into a 401.
    The scheme match is case-insensitive (``Bearer`` / ``bearer``); the
    token itself is compared exactly against the configured map.
    """
    if not header:
        return None
    parts = header.split(" ", 1)
    if len(parts) != 2:
        return None
    scheme, token = parts[0].strip(), parts[1].strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token_map.get(token)


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject any request whose bearer token isn't in ``token_map`` with a
    401, before it reaches the MCP session handler.

    With an empty ``token_map`` every request is denied — fail closed, so a
    misconfigured daemon never silently accepts the world.
    """

    def __init__(self, app, token_map: dict[str, str]):
        super().__init__(app)
        self._token_map = token_map

    async def dispatch(self, request: Request, call_next):
        header = request.headers.get("authorization")
        if label_for_authorization(header, self._token_map) is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)
