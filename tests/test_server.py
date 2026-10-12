import pandas as pd
import pytest

from .conftest import synthetic_bars


@pytest.fixture()
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_LIVE_DB_PATH", str(tmp_path / "live.sqlite"))
    monkeypatch.setenv("BTC_MODEL_PATH", str(tmp_path / "none.pkl"))
    import importlib

    import pipeline.config
    import server.app as appmod
    importlib.reload(pipeline.config)
    appmod = importlib.reload(appmod)
    now = pd.Timestamp("2026-01-03 12:07:30")
    monkeypatch.setattr(appmod, "utcnow", lambda: now)
    appmod.S = appmod.State()
    bars = synthetic_bars(days=3, seed=1)
    appmod.S.bars = bars[bars.index < now.floor("1min")]
    appmod.S.price_ts = __import__("time").time()
    return appmod, now


def test_signal_locks_and_reports(live):
    appmod, now = live
    S = appmod.S
    target = float(S.bars["close"].iloc[-1]) * 0.995  # price well above target -> confident ABOVE
    S.price = float(S.bars["close"].iloc[-1])
    W = now.floor("15min")
    S.market = {"ticker": "KXBTC15M-TEST", "close_time": W + pd.Timedelta(minutes=15), "target": target,
                "yes_bid": 0.95, "yes_ask": 0.97, "no_ask": 0.05}
    sig = appmod.compute_signal()
    assert sig["status"] == "ok" and sig["state"] == "LOCKED" and sig["side"] == "ABOVE"
    assert sig["target_source"] == "kalshi" and sig["prob_above"] > 0.9
    from pipeline.kalshi import fee_per_contract
    assert abs(sig["edge_above"] - (sig["prob_above"] - 0.97 - fee_per_contract(0.97))) < 1e-12
    # the lock never flips even if price collapses
    S.price = target * 0.99
    sig2 = appmod.compute_signal()
    assert sig2["side"] == "ABOVE" and sig2["prob_above"] < 0.1
    text = appmod.swiftbar_text(sig2, appmod.stats())
    assert text.splitlines()[0].startswith("▲ ABOVE")
    # settle it and check stats
    S.db.execute("UPDATE windows SET result='yes'")
    st = appmod.stats("2026-01")
    assert st["settled_calls"] == 1 and st["accuracy"] == 1.0 and st["coverage"] == 1.0


def test_signal_waits_when_uncertain(live):
    appmod, now = live
    S = appmod.S
    S.price = float(S.bars["close"].iloc[-1])
    S.market = None  # falls back to proxy target
    sig = appmod.compute_signal()
    assert sig["status"] == "ok" and sig["target_source"] == "proxy"
    assert sig["state"] in ("WATCHING", "LOCKED")
    assert "|" in appmod.swiftbar_text(sig, appmod.stats()).splitlines()[0]
