#!/usr/bin/env python3
"""
Polls the myindoor.global widget API and writes the latest good snapshot to
a local cache file the renderer reads. Runs as its own thread/process so a
slow or dead network never blocks drawing.

Two endpoints (confirmed working via real browser network inspection,
2026-09-xx):
  - config:     /api/widget/v1/widgets/<id>/?secret_key=<key>
                thresholds/colors, changes rarely.
  - timeseries: /api/widget/v1/widget/<id>/timeseries/?secret_key=<key>
                live readings.

Polling: the sensors report every `interval` (INTERVAL_5MIN on the widgets
seen so far) and the API carries no "measured at" time, so a new reading is
recognised by the values changing. The timeseries is checked once a minute
on the minute (what the web widget does), plus once shortly after each
expected update -- learned from when the values last changed -- so new
numbers show within seconds rather than up to a minute late. A failed call
is retried after 15s/30s/60s instead of waiting for the next slot.
"""
import json
import logging
import logging.handlers
import os
import time
import urllib.error
import urllib.request

CONFIG_PATH = os.environ.get("KIOSK_CONFIG", "/etc/kiosk-renderer/config.json")
CACHE_PATH = os.environ.get("KIOSK_CACHE", "/var/lib/kiosk-renderer/latest.json")
API_LOG_PATH = os.environ.get("KIOSK_API_LOG", "/var/log/kiosk-api.log")
WIDGET_HEALTH_LOG_PATH = os.environ.get(
    "KIOSK_WIDGET_HEALTH_LOG", "/var/log/kiosk-widget-health.log"
)

# The web widget allows 30s; 8s timed out ~2% of calls on the bench.
REQUEST_TIMEOUT_SEC = 20
RETRY_DELAYS_SEC = (15, 30, 60)
AFTER_UPDATE_SEC = 20  # extra check this long after an expected update
INTERVAL_SEC = {"INTERVAL_1MIN": 60, "INTERVAL_5MIN": 300, "INTERVAL_10MIN": 600,
                "INTERVAL_15MIN": 900, "INTERVAL_30MIN": 1800, "INTERVAL_1HOUR": 3600}
DEFAULT_INTERVAL_SEC = 300


def _make_logger(name, path):
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=5 * 1024 * 1024, backupCount=2
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    return logger


api_log = _make_logger("kiosk.api", API_LOG_PATH)
widget_health_log = _make_logger("kiosk.widget_health", WIDGET_HEALTH_LOG_PATH)


def load_config():
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def _fetch(url):
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "airvoice-kiosk-renderer/1.0"})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
            body = resp.read()
            latency_ms = int((time.monotonic() - t0) * 1000)
            api_log.info("OK status=%s latency=%dms url=%s", resp.status, latency_ms, url)
            return json.loads(body)
    except urllib.error.HTTPError as e:
        latency_ms = int((time.monotonic() - t0) * 1000)
        api_log.info("FAIL http_status=%s latency=%dms url=%s", e.code, latency_ms, url)
        return None
    except Exception as e:
        latency_ms = int((time.monotonic() - t0) * 1000)
        api_log.info("FAIL error=%s latency=%dms url=%s", type(e).__name__, latency_ms, url)
        return None


def _atomic_write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _widget_looks_healthy(timeseries):
    """Best-effort sanity check: does the payload actually contain readable
    index values, or did the API return something empty/malformed. An empty
    live block is still healthy during rest time, when the widget shows
    weekly averages instead."""
    try:
        extra = (timeseries.get("meta") or {}).get("extra") or {}
        if extra.get("is_rest_time"):
            return True, "ok (rest time)"
        indexes = timeseries["response"]["data"]["indexes"]
        if not indexes:
            return False, "empty indexes"
        for key, block in indexes.items():
            values = block.get("values")
            if values:
                return True, "ok"
        return False, "no non-empty value series"
    except (KeyError, TypeError, AttributeError):
        return False, "unexpected payload shape"


def _readings(timeseries):
    """Comparable fingerprint of the numbers the widget shows."""
    resp = (timeseries or {}).get("response") or {}
    extra = ((timeseries or {}).get("meta") or {}).get("extra") or {}
    blocks = {
        "indoor": ((resp.get("data") or {}).get("indexes")) or {},
        "outdoor": ((resp.get("outdoor") or {}).get("indexes")) or {},
        "weekly": extra.get("weekly_averages") or {},
    }
    return json.dumps(blocks, sort_keys=True)


def _interval_sec(config):
    key = ((config or {}).get("response") or {}).get("interval")
    return INTERVAL_SEC.get(key, DEFAULT_INTERVAL_SEC)


def _next_poll(now, last_change_at, interval):
    """Next minute boundary, or AFTER_UPDATE_SEC past the next expected
    update if that comes sooner."""
    nxt = (int(now) // 60 + 1) * 60
    if last_change_at:
        expected = last_change_at + interval
        while expected + AFTER_UPDATE_SEC <= now:
            expected += interval
        nxt = min(nxt, expected + AFTER_UPDATE_SEC)
    return nxt


def run(poll_config_every_sec=1800, poll_data_every_sec=None):
    """poll_data_every_sec is accepted for older config.json files and ignored."""
    cfg = load_config()
    base = cfg["api_base"].rstrip("/")
    widget_id = cfg["widget_id"]
    secret_key = cfg["secret_key"]

    config_url = f"{base}/api/widget/v1/widgets/{widget_id}/?secret_key={secret_key}"
    timeseries_url = f"{base}/api/widget/v1/widget/{widget_id}/timeseries/?secret_key={secret_key}"

    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)

    last_config = None
    last_config_fetch = 0
    last_good_timeseries = None
    last_good_at = None
    last_readings = None
    last_change_at = None
    failures = 0

    # Keep what the previous run cached, so a restart doesn't flash the boot
    # screen or forget when the data last changed.
    try:
        with open(CACHE_PATH) as f:
            prev = json.load(f)
        last_config = prev.get("config")
        last_good_timeseries = prev.get("timeseries")
        last_good_at = prev.get("last_good_at")
        last_change_at = prev.get("last_change_at")
        last_readings = _readings(last_good_timeseries) if last_good_timeseries else None
    except (OSError, ValueError, AttributeError):
        pass

    while True:
        now = time.monotonic()

        if last_config is None or last_config_fetch == 0 or (now - last_config_fetch) >= poll_config_every_sec:
            fetched = _fetch(config_url)
            if fetched is not None:
                last_config = fetched
                last_config_fetch = now

        timeseries = _fetch(timeseries_url)
        healthy, reason = (False, "no response")
        if timeseries is not None:
            healthy, reason = _widget_looks_healthy(timeseries)
            if healthy:
                last_good_timeseries = timeseries
                last_good_at = time.time()
                readings = _readings(timeseries)
                if readings != last_readings:
                    wall = time.time()
                    if last_change_at:
                        gap = int(wall - last_change_at)
                        interval = _interval_sec(last_config)
                        note = " GAP (longer than 2 intervals)" if gap > 2 * interval else ""
                        widget_health_log.info("data changed after %ds%s", gap, note)
                    else:
                        widget_health_log.info("data changed (first reading)")
                    last_readings = readings
                    last_change_at = wall

        widget_health_log.info(
            "healthy=%s reason=%s stale_for_sec=%s unchanged_for_sec=%s",
            healthy,
            reason,
            "n/a" if last_good_at is None else int(time.time() - last_good_at),
            "n/a" if last_change_at is None else int(time.time() - last_change_at),
        )

        snapshot = {
            "config": last_config,
            "timeseries": last_good_timeseries,
            "last_good_at": last_good_at,
            "last_change_at": last_change_at,
            "fetched_at": time.time(),
        }
        _atomic_write_json(CACHE_PATH, snapshot)

        if healthy:
            failures = 0
            wall = time.time()
            delay = _next_poll(wall, last_change_at, _interval_sec(last_config)) - wall
        else:
            delay = RETRY_DELAYS_SEC[min(failures, len(RETRY_DELAYS_SEC) - 1)]
            failures += 1
        time.sleep(max(1.0, delay))


if __name__ == "__main__":
    run()
