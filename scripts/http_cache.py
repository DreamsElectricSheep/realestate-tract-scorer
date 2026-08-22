"""
Polite, cached HTTP client.

Design goal: make rate limiting structurally impossible to trip, not a thing we
remember to be careful about.

Five layers of protection:
  1. On-disk cache      — a URL already fetched is NEVER re-fetched within its TTL.
                          Re-running an ingest costs zero requests.
  2. Per-host throttle  — enforced minimum delay between calls to the same host.
  3. Daily budget       — persisted across runs, so three runs in one day cannot
                          collectively blow a provider's per-day cap.
  4. Backoff            — exponential + jitter on 429/5xx, honors Retry-After.
  5. Timeouts           — on every request, always.

There are no polling loops anywhere in this project. Every dataset here updates
annually or quarterly; ingests are run on demand or monthly, never on a tight cron.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import threading
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import requests

from config import CACHE, LOGS, REQUEST_TIMEOUT, USER_AGENT

log = logging.getLogger("http_cache")

# ---------------------------------------------------------------- policy
# (min_seconds_between_requests, max_requests_per_day)
# Conservative on purpose. These are ceilings we choose, well under provider limits.
HOST_POLICY: dict[str, tuple[float, int]] = {
    "api.census.gov":             (1.1, 450),    # keyless cap is ~500/day/IP
    "geocoding.geo.census.gov":   (1.1, 400),
    "www2.census.gov":            (2.0, 200),    # bulk static files
    "api.usa.gov":                (1.2, 900),    # FBI CDE: ~1000/hr
    "api.stlouisfed.org":         (1.0, 300),    # FRED
    "www.huduser.gov":            (1.5, 200),
    "files.zillowstatic.com":     (2.0, 60),     # big static CSVs
    "www.fhfa.gov":               (2.0, 60),
    "download.geofabrik.de":      (5.0, 10),     # multi-GB, handled out-of-band
    "_default":                   (1.5, 300),
}

_STATE_FILE = LOGS / "request_budget.json"
_lock = threading.Lock()
_last_call: dict[str, float] = {}


def _policy(host: str) -> tuple[float, int]:
    return HOST_POLICY.get(host, HOST_POLICY["_default"])


def _load_budget() -> dict:
    today = date.today().isoformat()
    if _STATE_FILE.exists():
        try:
            st = json.loads(_STATE_FILE.read_text())
            if st.get("date") == today:
                return st
        except Exception:
            pass
    return {"date": today, "counts": {}}


def _save_budget(st: dict) -> None:
    try:
        _STATE_FILE.write_text(json.dumps(st, indent=2))
    except Exception as e:
        log.warning("could not persist request budget: %s", e)


class BudgetExceeded(RuntimeError):
    """Raised instead of hammering a provider that we've hit our self-imposed cap on."""


def _cache_path(url: str, params: dict | None) -> Path:
    blob = url + "|" + json.dumps(params or {}, sort_keys=True)
    h = hashlib.sha256(blob.encode()).hexdigest()[:32]
    host = urlparse(url).netloc.replace(":", "_")
    d = CACHE / host
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{h}.json"


def get_json(
    url: str,
    params: dict | None = None,
    *,
    ttl_days: int = 30,
    max_retries: int = 5,
    headers: dict | None = None,
) -> object | None:
    """
    Cached, throttled, backed-off GET returning parsed JSON.

    Returns None on permanent failure rather than raising, so a single bad tract
    can never abort a 3,000-county ingest. Callers must distinguish None from {}.
    """
    cp = _cache_path(url, params)
    if cp.exists():
        age_days = (time.time() - cp.stat().st_mtime) / 86400
        if age_days < ttl_days:
            try:
                return json.loads(cp.read_text())
            except Exception:
                cp.unlink(missing_ok=True)  # corrupt entry, refetch

    host = urlparse(url).netloc
    min_delay, daily_cap = _policy(host)

    with _lock:
        budget = _load_budget()
        used = budget["counts"].get(host, 0)
        if used >= daily_cap:
            raise BudgetExceeded(
                f"{host}: self-imposed daily cap of {daily_cap} reached "
                f"({used} used). Cached data still serves. Resume tomorrow, or raise "
                f"the cap in http_cache.HOST_POLICY if the provider allows it."
            )
        # throttle
        last = _last_call.get(host, 0.0)
        wait = min_delay - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        _last_call[host] = time.time()
        budget["counts"][host] = used + 1
        _save_budget(budget)

    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)

    delay = 2.0
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(
                url, params=params, headers=hdrs, timeout=REQUEST_TIMEOUT
            )
        except requests.RequestException as e:
            log.warning("%s attempt %d/%d network error: %s", host, attempt, max_retries, e)
            if attempt == max_retries:
                return None
            time.sleep(delay + random.uniform(0, 1.5))
            delay *= 2
            continue

        if r.status_code == 200:
            # Census answers a keyless or malformed query with HTTP 200 + an HTML
            # page (it redirects to missing_key.html). Trusting the status code
            # here is how you end up with a database full of nothing.
            if "missing_key" in r.url or "<html" in r.text[:200].lower():
                if "missing_key" in r.url:
                    log.error(
                        "Census rejected the request for a MISSING API KEY. "
                        "Get a free one at https://api.census.gov/data/key_signup.html "
                        "and put it in /opt/realestate/.keys.json as {\"census\": \"...\"}"
                    )
                else:
                    log.warning("%s returned HTML, not JSON (status 200)", url)
                return None
            try:
                data = r.json()
            except ValueError:
                log.warning("%s returned non-JSON (status 200, %d bytes)", url, len(r.content))
                return None
            try:
                cp.write_text(json.dumps(data))
            except Exception as e:
                log.warning("cache write failed: %s", e)
            return data

        if r.status_code in (204, 404):
            return None

        if r.status_code in (429, 500, 502, 503, 504):
            retry_after = r.headers.get("Retry-After")
            sleep_for = float(retry_after) if (retry_after or "").isdigit() else delay
            sleep_for += random.uniform(0, 1.5)
            log.warning(
                "%s HTTP %d, backing off %.1fs (attempt %d/%d)",
                host, r.status_code, sleep_for, attempt, max_retries,
            )
            time.sleep(sleep_for)
            delay *= 2
            continue

        log.warning("%s HTTP %d — giving up: %s", url, r.status_code, r.text[:200])
        return None

    return None


def download_file(url: str, dest: Path, *, min_bytes: int = 1024) -> Path | None:
    """
    Streamed download for large static files (OSM extracts, Zillow CSVs).
    Skips if a plausible file already exists — big files are never re-pulled.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size >= min_bytes:
        log.info("%s already present (%.1f MB) — skipping download",
                 dest.name, dest.stat().st_size / 1e6)
        return dest

    host = urlparse(url).netloc
    min_delay, _ = _policy(host)
    last = _last_call.get(host, 0.0)
    wait = min_delay - (time.time() - last)
    if wait > 0:
        time.sleep(wait)
    _last_call[host] = time.time()

    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with requests.get(
            url, headers={"User-Agent": USER_AGENT}, stream=True, timeout=REQUEST_TIMEOUT
        ) as r:
            if r.status_code != 200:
                log.error("download %s -> HTTP %d", url, r.status_code)
                return None
            total = int(r.headers.get("Content-Length", 0))
            done = 0
            mark = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    if total and done - mark > 100 * (1 << 20):  # log each 100 MB
                        mark = done
                        log.info("  %s: %.0f/%.0f MB (%.0f%%)",
                                 dest.name, done / 1e6, total / 1e6, 100 * done / total)
        tmp.rename(dest)
        log.info("downloaded %s (%.1f MB)", dest.name, dest.stat().st_size / 1e6)
        return dest
    except requests.RequestException as e:
        log.error("download failed %s: %s", url, e)
        tmp.unlink(missing_ok=True)
        return None


def budget_report() -> dict:
    """Current day's request usage per host, against the self-imposed caps."""
    budget = _load_budget()
    return {
        h: {"used": n, "cap": _policy(h)[1]}
        for h, n in sorted(budget["counts"].items())
    }
