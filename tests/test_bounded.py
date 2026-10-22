import json
import sqlite3

import numpy as np
import pandas as pd

from pipeline import loop, ticks
from recorder import compact
from recorder.run import SCHEMA

DAY = 86_400_000


def make_db(path, n_secs=7200, start_ms=1_791_000_000_000):
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    rng = np.random.default_rng(0)
    rows = []
    for k in range(n_secs):
        for venue, off in (("coinbase", 0.0), ("kraken", 1.0), ("gemini", -1.0)):
            for sub in (100, 600):
                mid = 80_000 + k * 0.1 + off + rng.normal(0, 0.2)
                rows.append((start_ms + k * 1000 + sub, venue, "bbo", mid - 0.5, mid + 0.5, None, None, None))
    c.executemany("INSERT INTO spot VALUES (?,?,?,?,?,?,?,?)", rows)
    c.commit()
    return c, start_ms


def reference_mid(c, t0, t1):
    s = pd.read_sql("SELECT ts, venue, bid, ask FROM spot WHERE ts >= ? AND ts < ?", c, params=(t0, t1))
    s = s.assign(mid=(s.bid + s.ask) / 2, sec=s.ts // 1000)
    return s.groupby(["sec", "venue"])["mid"].mean().unstack().ffill(limit=5).median(axis=1)


def test_sql_spot_mid_matches_pandas(tmp_path):
    db = tmp_path / "t.sqlite"
    c, t0 = make_db(db, n_secs=600)
    ref = reference_mid(c, t0, t0 + 600_000)
    got = ticks.spot_mid_1s(ticks.connect(db), t0, t0 + 600_000)
    pd.testing.assert_series_equal(got.sort_index(), ref.sort_index(), check_names=False, check_index_type=False)


def test_compact_rolls_old_spot_and_keeps_recent(tmp_path):
    db = tmp_path / "t.sqlite"
    c, t0 = make_db(db, n_secs=7200)                     # two hours of ticks
    now = t0 + 2 * DAY + 3_600_000                       # first hour is older than 2 days, second is not
    before = ticks.spot_mid_1s(ticks.connect(db), t0, t0 + 7_200_000)
    c.close()
    out = compact.run(db, now_ms=now)
    assert out["spot_rows_rolled"] == 3600 * 3 * 2
    c = sqlite3.connect(db)
    assert c.execute("SELECT min(ts) FROM spot").fetchone()[0] >= t0 + 3_600_000
    assert c.execute("SELECT count(*) FROM spot_1s").fetchone()[0] == 3600 * 3
    # the composite per-second mid is unchanged by compaction
    after = ticks.spot_mid_1s(ticks.connect(db), t0, t0 + 7_200_000)
    pd.testing.assert_series_equal(after.sort_index(), before.sort_index(), check_names=False, check_index_type=False)


def test_compact_trims_old_feeds(tmp_path):
    db = tmp_path / "t.sqlite"
    c = sqlite3.connect(db)
    c.executescript(SCHEMA)
    now = 1_791_000_000_000
    c.executemany("INSERT INTO brti VALUES (?,?,?,?,?,?,?)",
                  [(0, f, now - age, 1.0, None, None, None) for age in (DAY, 5 * DAY) for f in ("1hz", "5hz")])
    c.commit()
    c.close()
    out = compact.run(db, now_ms=now)
    assert out["brti_deleted"] == 1  # only the 5 Hz row older than 3 days; 1 Hz is kept forever
    c = sqlite3.connect(db)
    assert c.execute("SELECT count(*) FROM brti").fetchone()[0] == 3


def test_cycle_writes_report_when_a_candidate_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(loop, "DATA_DIR", tmp_path)
    monkeypatch.setattr(loop, "REPORT_DIR", tmp_path)
    monkeypatch.setattr(loop, "HISTORY", tmp_path / "h.jsonl")
    monkeypatch.setattr(loop, "LATEST", tmp_path / "latest.md")

    def fake(args, timeout):
        if args[0] == "--step":
            return True, ""
        if args[1] == "cand_arb":
            return True, 'noise\nRESULT [{"candidate": "arb", "windows": 400, "pnl_recent": 0.01, ' \
                         '"pnl_prior": 0.02, "t_stat": 3.0, "promote": true}]'
        return False, "timed out after 20 min"
    monkeypatch.setattr(loop, "run_isolated", fake)
    res = loop.cycle(refresh=True, maker_windows=1)
    assert any(r.get("promote") for r in res)
    assert sum("error" in r for r in res) == len(loop.CANDIDATES) - 1
    md = (tmp_path / "latest.md").read_text()
    assert "| arb | 400 |" in md and "error" in md
    assert json.loads((tmp_path / "loop_latest.json").read_text())["results"]
