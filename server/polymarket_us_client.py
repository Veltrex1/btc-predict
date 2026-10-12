"""Polymarket US API client (authenticated parts), per docs.polymarket.us/api-reference/authentication.

Headers: X-PM-Access-Key (key id), X-PM-Timestamp (ms), X-PM-Signature =
base64(Ed25519 sign(timestamp + METHOD + path)), where the private key is the
first 32 bytes of the base64-decoded Secret Key from polymarket.us/developer.
Market data (order books) is public on gateway.polymarket.us and needs no key.

    uv run python -m server.polymarket_us_client --check     # read-only: positions + current BTC book
"""
from __future__ import annotations

import base64
import os
import time

import requests
from cryptography.hazmat.primitives.asymmetric import ed25519

API = os.environ.get("POLYMARKET_US_API", "https://api.polymarket.us")
GATEWAY = "https://gateway.polymarket.us"


def decode_secret(secret: str) -> tuple[bytes, str]:
    """The portal's secret may be standard base64, base64url or hex; return (bytes, encoding)."""
    s = "".join(secret.split()).strip('"').strip("'")
    if len(s) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in s):
        return bytes.fromhex(s), "hex"
    for name, fn in (("base64", base64.b64decode), ("base64url", base64.urlsafe_b64decode)):
        try:
            padded = s + "=" * (-len(s) % 4)
            raw = fn(padded) if name == "base64url" else fn(padded, validate=True)
            return raw, name
        except Exception:  # noqa: BLE001
            continue
    raise ValueError("secret is not base64, base64url or hex")


def private_key_from_secret(secret: str) -> ed25519.Ed25519PrivateKey:
    raw, _ = decode_secret(secret)
    if len(raw) not in (32, 64):
        # e.g. a PKCS#8 DER blob: the 32-byte seed is the last 32 bytes of the 48-byte structure
        if len(raw) == 48:
            raw = raw[16:]
        else:
            raise ValueError(f"secret decodes to {len(raw)} bytes; expected 32 or 64")
    return ed25519.Ed25519PrivateKey.from_private_bytes(raw[:32])


class PolymarketUSClient:
    def __init__(self, key_id: str, secret_b64: str, base: str = API, timeout: float = 10.0):
        self.key_id = key_id.strip().strip('"').strip("'")
        self.key = private_key_from_secret(secret_b64.strip().strip('"').strip("'"))
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.s = requests.Session()

    @classmethod
    def from_env(cls) -> "PolymarketUSClient":
        return cls(os.environ["POLYMARKET_KEY_ID"], os.environ["POLYMARKET_SECRET_KEY"])

    def headers(self, method: str, path: str) -> dict:
        ts = str(int(time.time() * 1000))
        sig = base64.b64encode(self.key.sign(f"{ts}{method}{path.split('?')[0]}".encode())).decode()
        return {"X-PM-Access-Key": self.key_id, "X-PM-Timestamp": ts, "X-PM-Signature": sig,
                "Content-Type": "application/json"}

    def get(self, path: str, params=None) -> dict:
        r = self.s.get(self.base + path, params=params, headers=self.headers("GET", path), timeout=self.timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"GET {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    def positions(self) -> dict:
        return self.get("/v1/portfolio/positions")


def public_book(slug: str) -> dict | None:
    r = requests.get(f"{GATEWAY}/v1/markets/{slug}/book", timeout=10)
    return r.json().get("marketData") if r.status_code == 200 else None


def main() -> None:
    import argparse
    from datetime import datetime, timezone

    from recorder.run import parse_pmus, pmus_slug
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    if not args.check:
        ap.print_help()
        return
    # shape checks that never print the secrets themselves
    import re
    kid, sec = os.environ.get("POLYMARKET_KEY_ID", ""), os.environ.get("POLYMARKET_SECRET_KEY", "")
    quoted = kid != kid.strip() or '"' in kid or "'" in kid
    is_uuid = bool(re.fullmatch(r"[0-9a-fA-F-]{36}", kid.strip()))
    print(f"key id: {len(kid)} chars, looks like a UUID: {is_uuid}, has spaces/quotes: {quoted}")
    kinds = {"letters/digits": sum(c.isalnum() for c in sec), "+": sec.count("+"), "/": sec.count("/"),
             "-": sec.count("-"), "_": sec.count("_"), "=": sec.count("="),
             "whitespace": sum(c.isspace() for c in sec), "other": sum(not (c.isalnum() or c in "+/-_=" or c.isspace()) for c in sec)}
    print(f"secret: {len(sec)} chars; character types: {kinds}")
    try:
        raw, enc = decode_secret(sec)
        print(f"secret decodes as {enc} to {len(raw)} bytes (expected 32 or 64)")
    except Exception as e:  # noqa: BLE001
        print(f"secret could not be decoded: {e}")
    c = PolymarketUSClient.from_env()
    pos = c.positions()
    print("authenticated OK; positions response keys:", list(pos.keys()) if isinstance(pos, dict) else type(pos))
    now = datetime.now(timezone.utc)
    slug = pmus_slug(now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0))
    md = public_book(slug)
    print(slug, "book:", parse_pmus(md)[2:7] if md else "not listed right now")


if __name__ == "__main__":
    main()
