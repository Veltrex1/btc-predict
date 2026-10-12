import json

import numpy as np

from pipeline.micro import settle_fair
from recorder.run import parse_cf


def test_settle_fair_converges_and_is_monotone():
    target = np.array([100_000.0])
    # one tick left, average $1 above the target: settles YES with near certainty
    assert settle_fair(np.array([100_000.0]), np.array([100_001.0]), np.array([59]), target,
                       np.array([1.0]), 10.0)[0] > 0.99
    # ...and a big final-second drop can't drag 59 locked ticks far: $600 below moves the average only $10
    assert settle_fair(np.array([99_400.0]), np.array([100_011.0]), np.array([59]), target,
                       np.array([1.0]), 10.0)[0] > 0.99
    # 10 s left, 50 of 60 ticks locked $5 above target, spot $5 below: expected average +$3.33,
    # remaining sd = 10 * sqrt(10*11*21/6) / 60 = $3.27, so P = Phi(1.02) = 0.846
    p = settle_fair(np.array([99_995.0]), np.array([100_005.0]), np.array([50]), target, np.array([10.0]), 10.0)[0]
    from scipy.special import ndtr
    assert abs(p - ndtr((10 / 3) / (10 * np.sqrt(385) / 60))) < 1e-12
    # monotone in the locked-in partial average
    ps = [settle_fair(np.array([100_000.0]), np.array([a]), np.array([30]), target, np.array([30.0]), 10.0)[0]
          for a in (99_990, 100_000, 100_010)]
    assert ps[0] < ps[1] < ps[2]
    # before the final minute, at the money is ~50/50 and more time means less certainty
    assert abs(settle_fair(np.array([100_000.0]), np.array([np.nan]), np.array([0]), target, np.array([300.0]), 10.0)[0] - 0.5) < 1e-9
    near = settle_fair(np.array([100_020.0]), np.array([np.nan]), np.array([0]), target, np.array([90.0]), 10.0)[0]
    far = settle_fair(np.array([100_020.0]), np.array([np.nan]), np.array([0]), target, np.array([800.0]), 10.0)[0]
    assert near > far > 0.5


def test_parse_cf_messages():
    one = {"index_id": "BRTI", "received_at": 1710000000123,
           "data": json.dumps({"type": "value", "id": "BRTI", "time": 1710000000123, "value": "68000.12"}),
           "avg_60s_data": {"value": "68000.12000000", "window_size": 3},
           "last_60s_windowed_average_15min": {"value": "68000.23000000", "window_size": 14}}
    row = parse_cf(one, "1hz")
    assert row[1:] == ("1hz", 1710000000123, 68000.12, 68000.12, 68000.23, 14)
    outside = dict(one)
    outside.pop("last_60s_windowed_average_15min")
    assert parse_cf(outside, "1hz")[5:] == (None, None)
    five = {"index_id": "BRTI", "value_usd": "68000.12000000", "source_ts_ms": 1710000000323, "received_at": 1}
    assert parse_cf(five, "5hz")[1:4] == ("5hz", 1710000000323, 68000.12)
