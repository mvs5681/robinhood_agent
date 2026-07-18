#!/usr/bin/env python3
"""One-time Robinhood OAuth 2.0 authorization-code + PKCE flow.

Registers an OAuth client, opens your browser to the RH login page,
catches the callback on localhost:8080, exchanges the code for tokens,
and appends RH_ACCESS_TOKEN / RH_REFRESH_TOKEN / RH_CLIENT_ID to .env.

Run once, then keep .env out of git.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import os
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

import httpx

REGISTER_URL = "https://agent.robinhood.com/oauth/trading/register"
AUTHORIZE_URL = "https://robinhood.com/oauth"
TOKEN_URL = "https://api.robinhood.com/oauth2/token/"
REDIRECT_URI = "http://localhost:8080/callback"
SCOPE = "internal"


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------


def _pkce_pair() -> tuple[str, str]:
    """Return (verifier, S256 challenge)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# ---------------------------------------------------------------------------
# Callback server — catches the authorization code
# ---------------------------------------------------------------------------


_auth_code: str | None = None
_auth_error: str | None = None


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global _auth_code, _auth_error
        parsed = urllib.parse.urlparse(self.path)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        if "code" in params:
            _auth_code = params["code"]
            msg = b"<h2>Authorization successful! You can close this tab.</h2>"
        elif "error" in params:
            _auth_error = params.get("error_description", params["error"])
            msg = f"<h2>Error: {_auth_error}</h2>".encode()
        else:
            msg = b"<h2>Waiting for authorization...</h2>"

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(msg)

    def log_message(self, *args):
        pass  # silence default access log


def _run_callback_server(port: int = 8080) -> None:
    server = http.server.HTTPServer(("localhost", port), _CallbackHandler)
    server.handle_request()   # handle exactly one request, then exit
    server.server_close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    env_path = Path(__file__).parent.parent / ".env"

    # Step 1 — Dynamic client registration
    print("Registering OAuth client…")
    resp = httpx.post(
        REGISTER_URL,
        json={
            "client_name": "GEX Trading Agent",
            "redirect_uris": [REDIRECT_URI],
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
            "scope": SCOPE,
        },
        timeout=10,
    )
    resp.raise_for_status()
    client_id = resp.json()["client_id"]
    print(f"  client_id: {client_id}")

    # Step 2 — Build authorization URL with PKCE
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)

    auth_params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    auth_url = AUTHORIZE_URL + "?" + urllib.parse.urlencode(auth_params)

    # Step 3 — Start callback server then open browser
    print("\nStarting local callback server on :8080…")
    t = threading.Thread(target=_run_callback_server, daemon=True)
    t.start()

    print(f"\nOpening browser:\n  {auth_url}\n")
    webbrowser.open(auth_url)
    print("Log in to Robinhood and authorize the app. Waiting for callback…")
    t.join(timeout=120)

    if _auth_error:
        print(f"\nAuthorization failed: {_auth_error}", file=sys.stderr)
        sys.exit(1)
    if not _auth_code:
        print("\nTimeout — no callback received.", file=sys.stderr)
        sys.exit(1)

    print(f"  Got authorization code: {_auth_code[:8]}…")

    # Step 4 — Exchange code for tokens
    print("\nExchanging code for tokens…")
    token_resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": _auth_code,
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    token_resp.raise_for_status()
    tokens = token_resp.json()

    access_token = tokens["access_token"]
    refresh_token = tokens.get("refresh_token", "")

    # Step 5 — Write to .env
    existing = env_path.read_text() if env_path.exists() else ""

    def _set_var(text: str, key: str, value: str) -> str:
        import re
        pattern = rf"^{key}=.*$"
        replacement = f"{key}={value}"
        if re.search(pattern, text, re.MULTILINE):
            return re.sub(pattern, replacement, text, flags=re.MULTILINE)
        return text.rstrip("\n") + f"\n{replacement}\n"

    content = existing
    content = _set_var(content, "RH_CLIENT_ID", client_id)
    content = _set_var(content, "RH_ACCESS_TOKEN", access_token)
    content = _set_var(content, "RH_REFRESH_TOKEN", refresh_token)

    env_path.write_text(content)
    print(f"\nTokens written to {env_path}")
    print("  RH_CLIENT_ID, RH_ACCESS_TOKEN, RH_REFRESH_TOKEN set.")
    print("\nDone. Run scripts/run_live.py to start the agent.")


if __name__ == "__main__":
    main()
