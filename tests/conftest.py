import numpy as np
import pandas as pd
import pytest


def synthetic_bars(days: int = 20, seed: int = 0, sigma: float = 0.0008, start="2026-01-01") -> pd.DataFrame:
    """Random-walk 1-minute bars with 10-second sub-steps (so high/low/typical are realistic)."""
    rng = np.random.default_rng(seed)
    n = days * 1440
    steps = rng.normal(0, sigma / np.sqrt(6), size=(n, 6))
    path = 80000 * np.exp(np.cumsum(steps.ravel())).reshape(n, 6)
    opens = np.concatenate([[80000.0], path[:-1, -1]])
    idx = pd.date_range(start, periods=n, freq="1min")
    return pd.DataFrame({
        "open": opens,
        "high": np.maximum(path.max(axis=1), opens),
        "low": np.minimum(path.min(axis=1), opens),
        "close": path[:, -1],
        "volume": rng.uniform(1, 10, n),
        "taker_buy": rng.uniform(0, 1, n),
    }, index=idx)


@pytest.fixture(scope="session")
def bars():
    return synthetic_bars()
