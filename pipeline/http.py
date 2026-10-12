"""Small retrying HTTP helper shared by the backfills."""
from __future__ import annotations

import time

import requests

UA = {"User-Agent": "btc-predict-research/0.1"}


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update(UA)
    return s


def get(s: requests.Session, url: str, params=None, retries: int = 10, timeout: float = 30.0,
        ok_404: bool = False) -> requests.Response | None:
    delay = 1.0
    for attempt in range(retries):
        try:
            r = s.get(url, params=params, timeout=timeout)
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(delay)
            delay *= 2
            continue
        if r.status_code == 404 and ok_404:
            return None
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(float(r.headers.get("Retry-After", delay)))
            delay = min(delay * 2, 30)
            continue
        r.raise_for_status()
        return r
    raise RuntimeError(f"giving up on {url} after {retries} attempts")
