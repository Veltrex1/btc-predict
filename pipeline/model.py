"""Predictors: an analytic Brownian baseline and a calibrated gradient-boosted model.

Both return P(window settles above target). The pickled Predictor also carries
the lock policy (threshold tau, minimum lead time) chosen on validation data.
"""
from __future__ import annotations

import pickle
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.isotonic import IsotonicRegression

from .config import MODEL_PATH
from .features import FEATURES


@dataclass
class Policy:
    tau: float = 0.80          # lock ABOVE when p >= tau, BELOW when p <= 1 - tau
    min_lead_s: float = 90.0   # never lock with less than this many seconds left


@dataclass
class Predictor:
    kind: str = "baseline"     # "baseline" or "gbm"
    settle_mode: str = "avg60"
    policy: Policy = field(default_factory=Policy)
    clf: HistGradientBoostingClassifier | None = None
    iso: IsotonicRegression | None = None
    meta: dict = field(default_factory=dict)

    def predict(self, rows: pd.DataFrame) -> np.ndarray:
        if self.kind == "baseline" or self.clf is None:
            return rows["p_bm"].to_numpy(dtype=float)
        raw = self.clf.predict_proba(rows[used_features(self.clf)].to_numpy(dtype=np.float32))[:, 1]
        return self.iso.predict(raw) if self.iso is not None else raw

    def save(self, path: str | Path = MODEL_PATH) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(self, f)

    @staticmethod
    def load(path: str | Path = MODEL_PATH) -> "Predictor":
        path = Path(path)
        if not path.exists():
            return Predictor()
        with open(path, "rb") as f:
            return pickle.load(f)


def used_features(clf) -> list[str]:
    return getattr(clf, "btc_features", FEATURES)


def fit_gbm(train: pd.DataFrame, calib: pd.DataFrame | None, seed: int = 0) -> tuple:
    # features with no data in this source (e.g. taker imbalance on Coinbase) are left out
    cols = [f for f in FEATURES if train[f].notna().any()]
    clf = HistGradientBoostingClassifier(
        max_iter=400, learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=200,
        l2_regularization=1.0, early_stopping=True, validation_fraction=0.1,
        n_iter_no_change=20, random_state=seed,
    )
    clf.fit(train[cols].to_numpy(dtype=np.float32), train["label"].to_numpy())
    clf.btc_features = cols
    iso = None
    if calib is not None and len(calib) > 1000:
        raw = clf.predict_proba(calib[cols].to_numpy(dtype=np.float32))[:, 1]
        iso = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip").fit(raw, calib["label"].to_numpy())
    return clf, iso
