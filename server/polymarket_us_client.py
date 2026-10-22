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

    def _req(self, method: str, path: str, params=None, body=None, retries: int = 3) -> dict:
        for attempt in range(retries):
            r = self.s.request(method, self.base + path, params=params, json=body,
                               headers=self.headers(method, path), timeout=self.timeout)
            if r.status_code == 429 and attempt < retries - 1:
                time.sleep(1.0 * 2 ** attempt)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
            return r.json() if r.content else {}
        raise RuntimeError(f"{method} {path} rate limited")

    def get(self, path: str, params=None) -> dict:
        return self._req("GET", path, params=params)

    def positions(self) -> dict:
        return self.get("/v1/portfolio/positions")

    def balance(self) -> dict:
        """USD cash and buying power."""
        b = next((x for x in self.get("/v1/account/balances").get("balances", []) if x.get("currency") == "USD"), {})
        return {"cash": float(b.get("currentBalance") or 0), "buying_power": float(b.get("buyingPower") or 0)}

    def place(self, slug: str, intent: str, yes_price: float, qty: float, preview: bool = False) -> dict:
        """Immediate-or-cancel limit order. Prices always refer to the YES (Up) side, so buying Down at q
        is intent ORDER_INTENT_BUY_SHORT at yes_price 1 - q.

        Returns {order_id, filled, avg_yes_price, fee, raw}. What filled is read from the create reply's
        executions first (an order that expires unfilled is not always retrievable afterwards: GET /v1/order/{id}
        can answer 404). Only if that reply does not settle the matter is the order looked up, and if its fate
        still cannot be confirmed this raises FillUnknown instead of guessing."""
        body = {"marketSlug": slug, "intent": intent, "type": "ORDER_TYPE_LIMIT",
                "price": {"value": f"{yes_price:.2f}", "currency": "USD"}, "quantity": qty,
                "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
                "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC"}
        if preview:
            return self._req("POST", "/v1/order/preview", body={"request": body})
        r = self._req("POST", "/v1/orders", body={**body, "synchronousExecution": True, "maxBlockTime": "3"})
        oid = r.get("id", "")
        execs = r.get("executions") or []
        order = {}
        if not _is_final(execs):
            order = self._settled_order(oid)
            if not order or order.get("state") not in FINAL_STATES:
                got = fill_summary(order, execs)
                if not (got["filled"] and order.get("state") in (None, "ORDER_STATE_PARTIALLY_FILLED")):
                    raise FillUnknown(oid, r)
        return {"order_id": oid, **fill_summary(order, execs), "raw": {"create": r, "order": order}}

    def _settled_order(self, order_id: str, tries: int = 6, delay: float = 0.5) -> dict:
        """Poll the order until it reaches a final state; tolerates 404 (not visible yet / already gone)."""
        order: dict = {}
        for _ in range(tries):
            try:
                order = self.order(order_id)
            except RuntimeError as e:
                if " 404" not in str(e):
                    raise
            if order.get("state") in FINAL_STATES:
                break
            time.sleep(delay)
        return order

    def order(self, order_id: str) -> dict:
        return self.get(f"/v1/order/{order_id}").get("order", {})


class FillUnknown(RuntimeError):
    """An order was accepted but whether it filled could not be confirmed: stop trading and look at the account."""

    def __init__(self, order_id: str, reply: dict):
        super().__init__(f"could not confirm the outcome of Polymarket order {order_id}: {str(reply)[:300]}")
        self.order_id = order_id


FINAL_STATES = ("ORDER_STATE_FILLED", "ORDER_STATE_CANCELED", "ORDER_STATE_EXPIRED", "ORDER_STATE_REJECTED")
FINAL_EXECUTIONS = ("EXECUTION_TYPE_FILL", "EXECUTION_TYPE_CANCELED", "EXECUTION_TYPE_EXPIRED",
                    "EXECUTION_TYPE_REJECTED", "EXECUTION_TYPE_DONE_FOR_DAY")


def _is_final(executions: list[dict]) -> bool:
    """True when the executions in the create reply already end the order (filled, expired, canceled, rejected)."""
    return any(e.get("type") in FINAL_EXECUTIONS for e in executions)


def fill_summary(order: dict, executions: list[dict]) -> dict:
    """Filled quantity, average YES price and commission, from the order if it reports them, else from executions."""
    filled = float(order.get("cumQuantity") or 0)
    avg = (order.get("avgPx") or {}).get("value") if isinstance(order.get("avgPx"), dict) else order.get("avgPx")
    fee = order.get("commissionNotionalTotalCollected")
    if not filled and executions:
        fills = [(float(e.get("lastShares") or 0), float((e.get("lastPx") or {}).get("value") or 0))
                 for e in executions if e.get("type") in ("EXECUTION_TYPE_FILL", "EXECUTION_TYPE_PARTIAL_FILL")]
        filled = sum(q for q, _ in fills)
        avg = sum(q * p for q, p in fills) / filled if filled else None
        for e in reversed(executions):  # the last execution carries the running commission total
            fee = (e.get("order") or {}).get("commissionNotionalTotalCollected") or fee
            if fee:
                break
    fee = fee.get("value") if isinstance(fee, dict) else fee
    return {"filled": filled, "avg_yes_price": float(avg) if avg not in (None, "") else None,
            "fee": float(fee) if fee not in (None, "") else None}


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
