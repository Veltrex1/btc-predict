import base64

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from server.kalshi_client import KalshiClient, order_payload, sign


def test_sign_rsa_pss_verifies():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    msg = "1700000000000GET/trade-api/v2/portfolio/balance"
    sig = base64.b64decode(sign(key, msg))
    key.public_key().verify(sig, msg.encode(), padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                           salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())


def test_sign_ed25519_verifies():
    key = ed25519.Ed25519PrivateKey.generate()
    msg = "1700000000000POST/trade-api/v2/portfolio/events/orders"
    key.public_key().verify(base64.b64decode(sign(key, msg)), msg.encode())


def test_headers_sign_full_path_without_query():
    key = ed25519.Ed25519PrivateKey.generate()
    c = KalshiClient("kid", key, base="https://api.elections.kalshi.com/trade-api/v2")
    h = c.headers("GET", "/portfolio/positions?limit=5")
    msg = h["KALSHI-ACCESS-TIMESTAMP"] + "GET" + "/trade-api/v2/portfolio/positions"
    key.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), msg.encode())
    assert h["KALSHI-ACCESS-KEY"] == "kid"


def test_order_payload_maps_to_yes_leg():
    b = order_payload("T", "buy", "yes", 1.93, 0.52)
    assert (b["side"], b["price"], b["count"], b["reduce_only"]) == ("bid", "0.5200", "1.93", False)
    b = order_payload("T", "sell", "yes", 1.93, 0.60)
    assert (b["side"], b["price"], b["reduce_only"]) == ("ask", "0.6000", True)
    b = order_payload("T", "buy", "no", 2.0, 0.30)    # buy NO at 30c == sell YES at 70c
    assert (b["side"], b["price"], b["reduce_only"]) == ("ask", "0.7000", False)
    b = order_payload("T", "sell", "no", 2.0, 0.25)   # sell NO at 25c == buy YES at 75c
    assert (b["side"], b["price"], b["reduce_only"]) == ("bid", "0.7500", True)
    assert b["time_in_force"] == "immediate_or_cancel"


class FakeResp:
    def __init__(self, body, status=200):
        self._b, self.status_code, self.content, self.text = body, status, b"x", str(body)

    def json(self):
        return self._b


def test_book_and_place_parsing(monkeypatch):
    c = KalshiClient("kid", ed25519.Ed25519PrivateKey.generate())
    calls = []

    def fake(method, url, params=None, json=None, headers=None, timeout=None):
        calls.append((method, url, json))
        if url.endswith("/orderbook"):
            return FakeResp({"orderbook_fp": {"no_dollars": [["0.2500", "10.00"], ["0.2700", "33.00"]],
                                              "yes_dollars": [["0.7000", "5.00"], ["0.7200", "8.00"]]}})
        return FakeResp({"order_id": "o1", "fill_count": "2.00", "remaining_count": "0.00",
                         "average_fill_price": "0.7200", "average_fee_paid": "0.0141", "ts_ms": 1})

    monkeypatch.setattr(c.s, "request", fake)
    b = c.book("T")
    assert (b.yes_bid, b.yes_bid_size, b.yes_ask, b.yes_ask_size) == (0.72, 8.0, 0.73, 33.0)
    assert b.size_at("buy", "yes") == 33.0 and b.size_at("buy", "no") == 8.0
    f = c.place("T", "buy", "no", 2, 0.28)   # NO ask = 1 - 0.72
    assert calls[-1][2]["side"] == "ask" and calls[-1][2]["price"] == "0.7200"
    assert f.filled == 2.0 and abs(f.avg_price - 0.28) < 1e-9 and f.avg_fee == 0.0141
