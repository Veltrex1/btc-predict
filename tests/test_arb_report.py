import subprocess
import time

import pytest

from server import arb_trader as at


def make(tmp_path, name, mode="shadow"):
    clients = {"kalshi": object(), "pm": object()} if mode == "live" else {}  # live mode insists on both clients
    t = at.ArbTrader(at.Config(mode=mode), db_path=tmp_path / name, **clients)
    t.source = "mac"
    return t


def add(t, mode, status, pnl=None, edge=0.03, filled=5.0):
    settled = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()) if pnl is not None else None
    t.db.execute("INSERT INTO attempts (mode, status, quoted_edge, k_filled, pnl, settled_ts) VALUES (?,?,?,?,?,?)",
                 (mode, status, edge, filled, pnl, settled))
    t.db.commit()


class FakeHTTP:
    """Stands in for the dashboard server: feeds the posted body straight into store_report."""

    def __init__(self, server_db, fail=False):
        self.server_db, self.fail, self.posts = server_db, fail, []

    def post(self, url, json, headers, timeout):
        self.posts.append((url, headers))
        if self.fail:
            raise ConnectionError("server unreachable")
        at.store_report(json, self.server_db)

        class R:
            def raise_for_status(self):
                pass
        return R()


def test_push_sends_new_rows_once_and_resends_settled_ones(tmp_path, monkeypatch):
    monkeypatch.setenv("ARB_REPORT_URL", "https://dash/arb/report")
    monkeypatch.setenv("BTC_API_TOKEN", "tok")
    t, srv = make(tmp_path, "mac.sqlite", "live"), tmp_path / "server.sqlite"
    t.http = FakeHTTP(srv)
    add(t, "live", "hedged")
    assert t.push_report() == 1
    assert t.http.posts[0] == ("https://dash/arb/report", {"Authorization": "Bearer tok"})
    assert t.push_report() == 0          # nothing new, still sends a heartbeat
    t.db.execute("UPDATE attempts SET pnl = 0.4, settled_ts = ?, synced = 0",
                 (time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),))
    t.db.commit()
    assert t.push_report() == 1          # the settled row goes again and replaces the old copy
    c = at.sqlite3.connect(srv)
    assert c.execute("SELECT count(*), sum(pnl) FROM remote_attempts").fetchone() == (1, 0.4)


def test_push_failure_keeps_rows_for_next_time_and_is_silent_without_url(tmp_path, monkeypatch):
    t = make(tmp_path, "mac.sqlite", "live")
    add(t, "live", "hedged")
    monkeypatch.delenv("ARB_REPORT_URL", raising=False)
    assert t.push_report() == 0
    monkeypatch.setenv("ARB_REPORT_URL", "https://dash/arb/report")
    t.http = FakeHTTP(tmp_path / "s.sqlite", fail=True)
    with pytest.raises(ConnectionError):
        t.push_report()
    assert t.db.execute("SELECT synced FROM attempts").fetchone()[0] == 0
    t.http = FakeHTTP(tmp_path / "s.sqlite")
    assert t.push_report() == 1


def test_summary_combines_local_and_reported_live_and_lists_runners(tmp_path, monkeypatch):
    monkeypatch.setenv("ARB_REPORT_URL", "https://dash/arb/report")
    srv_db = tmp_path / "server.sqlite"
    server = at.ArbTrader(at.Config(mode="shadow"), db_path=srv_db)      # the droplet's own shadow trader
    add(server, "shadow", "hedged", pnl=0.30)
    mac = make(tmp_path, "mac.sqlite", "live")
    mac.http = FakeHTTP(srv_db)
    add(mac, "live", "hedged", pnl=0.50)
    add(mac, "live", "unwound", pnl=-0.20, edge=0.05)
    mac.push_report()
    s = at.summary(srv_db)
    assert s["shadow"]["pnl"] == pytest.approx(0.30) and s["shadow"]["attempts"] == 1
    live = s["live"]
    assert live["attempts"] == 2 and live["hedged"] == 1 and live["unwound"] == 1
    assert live["pnl"] == pytest.approx(0.30) and live["pnl_today"] == pytest.approx(0.30)
    assert live["avg_quoted_edge"] == pytest.approx(0.04)
    assert s["running"][0]["source"] == "mac" and s["running"][0]["mode"] == "live"
    assert s["running"][0]["age_s"] < 5


def test_summary_works_before_anything_reports(tmp_path):
    t = at.ArbTrader(at.Config(mode="shadow"), db_path=tmp_path / "x.sqlite")
    add(t, "shadow", "hedged", pnl=0.1)
    s = at.summary(tmp_path / "x.sqlite")
    assert s["running"] == [] and s["shadow"]["attempts"] == 1 and "live" not in s


def test_existing_database_without_synced_column_is_migrated(tmp_path):
    db = at.sqlite3.connect(tmp_path / "old.sqlite")
    db.executescript(at.SCHEMA.replace(",\n    synced INTEGER DEFAULT 0", ""))
    db.execute("INSERT INTO attempts (mode, status) VALUES ('shadow', 'hedged')")
    db.commit()
    db.close()
    t = at.ArbTrader(at.Config(mode="shadow"), db_path=tmp_path / "old.sqlite")
    assert "synced" in [r[1] for r in t.db.execute("PRAGMA table_info(attempts)")]
    t.db.execute("UPDATE attempts SET synced = 0")
    assert t.db.execute("SELECT count(*) FROM attempts WHERE synced = 0").fetchone()[0] == 1


@pytest.mark.parametrize("body", [
    {"source": "bad source!", "mode": "live", "rows": []},
    {"source": "mac", "mode": "yolo", "rows": []},
    {"source": "mac", "mode": "live", "rows": "nope"},
    {"source": "mac", "mode": "live", "rows": [{"id": 1}] * 1001},
])
def test_store_report_rejects_bad_input(tmp_path, body):
    with pytest.raises(ValueError):
        at.store_report(body, tmp_path / "s.sqlite")


def test_endpoint_returns_400_on_bad_report():
    import server.app as appmod
    resp = appmod.post_arb_report({"source": "x y", "mode": "live"})
    assert resp.status_code == 400 and b"bad source" in resp.body


def test_balances_keep_last_good_value_when_rate_limited(monkeypatch):
    import server.app as appmod
    calls = iter([{"kalshi": 19.4, "polymarket": 45.0},
                  {"kalshi_error": "429 too many requests", "polymarket": 45.0}])
    monkeypatch.setattr(appmod, "_balances", lambda: next(calls))
    monkeypatch.setattr(at, "summary", lambda *a, **k: {})
    appmod._BAL.update(ts=0.0, data={})
    first = appmod.get_balances()
    assert first["kalshi"] == 19.4 and first["total"] == pytest.approx(64.4)
    appmod._BAL["ts"] = 0.0  # cache expired
    second = appmod.get_balances()
    assert second["kalshi"] == 19.4 and "kalshi_error" not in second


def test_mac_script_quotes_secrets_and_survives_sourcing(tmp_path):
    script = f'''
    ENVF={tmp_path}/env; touch "$ENVF"
    {subprocess.check_output(["sed", "-n", "/^setenv()/,/^}/p", "deploy/mac_arb.sh"], text=True)}
    setenv POLYMARKET_SECRET_KEY 'ab+/c=d$e f"g'
    setenv ARB_MODE live
    setenv ARB_MODE shadow
    set -a; . "$ENVF"; set +a
    printf '%s|%s' "$POLYMARKET_SECRET_KEY" "$ARB_MODE"
    '''
    out = subprocess.check_output(["bash", "-c", script], text=True)
    assert out == 'ab+/c=d$e f"g|shadow'
    assert (tmp_path / "env").read_text().count("ARB_MODE=") == 1
