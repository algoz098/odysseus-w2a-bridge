#!/usr/bin/env python3
"""Load .env into the extension's settings, over the app's own API.

Why a script and not a shell one-liner
--------------------------------------
The password contains a ``!``. Hand-typed into a double-quoted shell
string it is a history-expansion hazard, and into an ``httpie``/``curl``
command line it lands in shell history and in ``ps`` output. This reads
the file, keeps the secret in Python, and posts it over the app's admin
route -- so the value never appears in a command line, a log, or git.

Usage::

    python3 tests/load_credentials.py            # load from .env
    python3 tests/load_credentials.py --verify   # load, then prove auth works
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
import urllib.error

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(REPO_ROOT, ".env")
BASE_URL = os.environ.get("ODYSSEUS_E2E_BASE_URL", "http://127.0.0.1:7011")
COOKIE = os.environ.get("ODYSSEUS_SESSION_COOKIE", "")


def _fail(msg: str) -> "NoReturn":
    print(f"  [FAIL] {msg}")
    raise SystemExit(1)


def read_env(path: str = ENV_PATH) -> dict:
    values: dict = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip("'\"")
    except FileNotFoundError:
        _fail(f"{path} not found — copy .env.example and fill it in")
    return values


def post(path: str, body: dict) -> dict:
    req = urllib.request.Request(BASE_URL + path, method="POST")
    req.add_header("Cookie", COOKIE)
    req.add_header("Content-Type", "application/json")
    data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, data, timeout=60) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:300]
        _fail(f"POST {path} -> HTTP {e.code}: {detail}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true",
                    help="after loading, prove the credentials authenticate")
    args = ap.parse_args()

    values = read_env()
    email = values.get("WHATS2API_EMAIL")
    password = values.get("WHATS2API_PASSWORD")
    if not email or not password:
        _fail("WHATS2API_EMAIL / WHATS2API_PASSWORD missing from .env")

    print(f"loading credentials for {email} …")
    result = post("/ext-api/whats2api_bridge/admin/config",
                  {"api_email": email, "api_password": password})
    if not result.get("ok"):
        _fail(f"the extension refused the config write: {result}")
    print("  [ok] settings written (password never echoed)")

    if args.verify:
        print("verifying against the live provider …")
        verify(email, password)
    return 0


def verify(email: str, password: str) -> None:
    """Mint a JWT directly and list connections, in-process.

    Does not go through the extension so a failure here is unambiguously
    the credential and not the extension's state.
    """
    import asyncio
    import tempfile

    os.environ.setdefault("DATABASE_URL",
                          "sqlite:///" + tempfile.mktemp(suffix=".db"))
    sys.path.insert(0, os.path.expanduser("~/Documents/odysseus"))
    sys.path.insert(0, REPO_ROOT)
    import conftest  # noqa: F401  -- isolates settings + db

    import httpx

    async def _run():
        async with httpx.AsyncClient(
            base_url="https://api.whats2api.com", timeout=30,
        ) as client:
            r = await client.post(
                "/authentication",
                json={"email": email, "password": password,
                      "strategy": "local"},
                headers={"Content-Type": "application/json",
                         "Accept": "application/json"},
            )
            print(f"  POST /authentication -> HTTP {r.status_code}")
            if r.status_code >= 400:
                _fail(f"login refused: {r.text[:200]}")
            token = (r.json() or {}).get("accessToken")
            if not token:
                _fail(f"login returned no accessToken: {r.text[:200]}")
            print(f"  [ok] accessToken minted ({len(token)} chars)")

            r = await client.get("/connections",
                                 headers={"Authorization": f"Bearer {token}"})
            print(f"  GET /connections      -> HTTP {r.status_code}")
            if r.status_code >= 400:
                _fail(f"list_connections refused: {r.text[:200]}")
            data = r.json()
            # whats2api is a Feathers app: a paginated envelope, not a list.
            rows = data.get("data") if isinstance(data, dict) else data
            if not isinstance(rows, list):
                _fail(f"unexpected /connections shape: {str(data)[:200]}")
            print(f"  [ok] {len(rows)} connection(s) on the account:")
            for c in rows:
                print("       ", json.dumps({
                    k: c.get(k) for k in
                    ("_id", "name", "engine", "phone", "status")
                }))
                for hook in (c.get("webhooks") or []):
                    print("        webhook:", hook.get("url"))

    asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())