"""Background env data uploader.

Pure HTTP poster. Doesn't touch I2C — reads cached sensor values from
the `sensors` module (which owns the Grove bus and runs in its own
thread). See `sensors.py` for the rationale on single-owner sensor
access.

Reliability layer: every reading is appended to an NDJSON buffer on
flash BEFORE we try to upload. Uploads drain the buffer in batches.
When the network is down (Mac off / out of WiFi range) the buffer keeps
growing, capped at MAX_RECORDS — older readings get evicted FIFO so a
long absence can't fill flash. When the network comes back the buffer
drains catch-up at ~17 records/s.

Config: device-local at /flash/env_upload.json:
  {
    "url": "http://...:8080/api/ingest",
    "device": "living-room",
    "interval_s": 30,                      # optional, default 30
    "max_buffer_records": 10000            # optional, default 10000
  }

NB: the daemon uploads to `<url>` for single-record posts and
`<url>/batch` (derived) for batch drains. The URL field stays the same
single-record endpoint to keep `/flash/env_upload.json` backward-
compatible.

State surface for the launcher status bar:
  state() -> "off" | "init" | "ok" | "stale" | "err"
"""

import gc
import json
import os
import time

try:
    import _thread
except ImportError:
    _thread = None

try:
    import urequests as _requests
except ImportError:
    try:
        import requests as _requests
    except ImportError:
        _requests = None

import sensors


_CFG_PATH = "/flash/env_upload.json"
_PENDING_PATH = "/flash/env_pending.ndjson"

_DEFAULT_INTERVAL_S = 30
_DEFAULT_MAX_RECORDS = 10000
_TRUNCATE_SLACK = 500          # only rewrite the file when overflow >= slack
_BATCH_SIZE = 100
_MAX_DRAIN_BATCHES_PER_CYCLE = 5

_HTTP_TIMEOUT_SINGLE = 3
_HTTP_TIMEOUT_BATCH = 5

# Below this Unix epoch the RTC is treated as unset (launcher's NTP
# sync hasn't completed yet, or it failed). 2024-01-01 = 1704067200 —
# anything below that is implausible enough that we skip recording.
_NTP_MIN_EPOCH = 1_700_000_000

_STALE_FACTOR = 3


_state = "off"
_last_err = ""
_last_post_ms = 0
_started = False
_lock = None
_interval_s = _DEFAULT_INTERVAL_S


def _set_state(new_state, err=""):
    global _state, _last_err
    if _lock is not None:
        _lock.acquire()
    try:
        _state = new_state
        if err:
            _last_err = err[:80]
    finally:
        if _lock is not None:
            _lock.release()


def _mark_posted():
    global _last_post_ms, _state, _last_err
    if _lock is not None:
        _lock.acquire()
    try:
        _last_post_ms = time.ticks_ms()
        _state = "ok"
        _last_err = ""
    finally:
        if _lock is not None:
            _lock.release()


def state():
    """Combined state: errors from the sensor owner trump our own
    posting state, then we promote ok→stale once the last successful
    POST is older than _STALE_FACTOR×interval."""
    s_state = sensors.state()
    if s_state == "err":
        return "err"
    if _state == "off":
        return "off"
    if _state != "ok":
        return _state
    age_ms = time.ticks_diff(time.ticks_ms(), _last_post_ms)
    if age_ms > _interval_s * 1000 * _STALE_FACTOR:
        return "stale"
    return "ok"


def last_error():
    return _last_err


# --- Config -----------------------------------------------------------

def _load_config():
    """Returns (url, device, interval_s, max_records) or
    (None, None, None, None) if no valid config — daemon stays off."""
    try:
        with open(_CFG_PATH) as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            return None, None, None, None
        url = cfg.get("url") or ""
        if not url:
            return None, None, None, None
        device = cfg.get("device") or "cardputer"
        interval = int(cfg.get("interval_s", _DEFAULT_INTERVAL_S))
        if interval < 5:
            interval = 5
        max_rec = int(cfg.get("max_buffer_records", _DEFAULT_MAX_RECORDS))
        if max_rec < 100:
            max_rec = 100
        return url, device, interval, max_rec
    except Exception:
        return None, None, None, None


def _batch_url(single_url):
    """Derive the batch endpoint from the single-record one. We just
    append '/batch' so users with an existing /flash/env_upload.json
    don't have to update their config."""
    return single_url.rstrip("/") + "/batch"


def _wifi_ok():
    try:
        import network
        return bool(network.WLAN(network.STA_IF).isconnected())
    except Exception:
        return False


# --- Pending NDJSON buffer --------------------------------------------

def _append_pending(record):
    """Append one JSON line. Failure (full flash, etc.) is recorded but
    doesn't propagate — losing a single reading is preferable to taking
    down the daemon."""
    try:
        line = json.dumps(record) + "\n"
        with open(_PENDING_PATH, "a") as f:
            f.write(line)
        return True
    except Exception as e:
        _set_state("err", "buffer write: " + repr(e)[:60])
        return False


def _pending_line_count():
    """Return number of lines in the buffer (0 if file missing)."""
    try:
        n = 0
        with open(_PENDING_PATH) as f:
            for _ in f:
                n += 1
        return n
    except OSError:
        return 0
    except Exception:
        return 0


def _truncate_pending(keep_last_n):
    """Rewrite the pending file keeping only the last `keep_last_n`
    lines. Uses a tmp file + rename for atomic replacement."""
    tmp = _PENDING_PATH + ".tmp"
    try:
        # First pass: count
        total = _pending_line_count()
        skip = total - keep_last_n
        if skip <= 0:
            return
        # Second pass: copy from line `skip` to end into tmp
        with open(_PENDING_PATH) as src, open(tmp, "w") as dst:
            i = 0
            for line in src:
                if i >= skip:
                    dst.write(line)
                i += 1
        os.rename(tmp, _PENDING_PATH)
    except Exception as e:
        _set_state("err", "buffer truncate: " + repr(e)[:60])
        # Best-effort cleanup of the tmp file.
        try:
            os.remove(tmp)
        except OSError:
            pass


def _truncate_if_overflow(max_records):
    n = _pending_line_count()
    if n > max_records + _TRUNCATE_SLACK:
        _truncate_pending(max_records)


def _read_first_batch(batch_size):
    """Read up to `batch_size` lines from the head of the pending file.
    Returns (records: list[dict], lines_consumed: int). Skips malformed
    lines (they count toward lines_consumed so they get discarded on
    the next truncate)."""
    records = []
    lines_consumed = 0
    try:
        with open(_PENDING_PATH) as f:
            for line in f:
                lines_consumed += 1
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except Exception:
                    pass    # discard malformed line via lines_consumed
                if len(records) >= batch_size:
                    break
        return records, lines_consumed
    except OSError:
        return [], 0


def _drop_head_lines(n):
    """Remove the first `n` lines from the pending file. Used after a
    successful batch upload to advance past the just-posted records."""
    if n <= 0:
        return
    tmp = _PENDING_PATH + ".tmp"
    try:
        kept = False
        with open(_PENDING_PATH) as src, open(tmp, "w") as dst:
            i = 0
            for line in src:
                if i >= n:
                    dst.write(line)
                    kept = True
                i += 1
        if kept:
            os.rename(tmp, _PENDING_PATH)
        else:
            # All consumed — remove the empty file.
            os.remove(tmp)
            try:
                os.remove(_PENDING_PATH)
            except OSError:
                pass
    except Exception as e:
        _set_state("err", "buffer drop-head: " + repr(e)[:60])
        try:
            os.remove(tmp)
        except OSError:
            pass


# --- HTTP -------------------------------------------------------------

def _post_batch(batch_url, records):
    """POST a batch of records. Returns True on HTTP 2xx."""
    if _requests is None:
        _set_state("err", "no urequests")
        return False
    r = None
    try:
        gc.collect()
        r = _requests.post(batch_url, json={"readings": records},
                           timeout=_HTTP_TIMEOUT_BATCH)
        ok = 200 <= r.status_code < 300
        if not ok:
            _set_state("err", "http {}".format(r.status_code))
        return ok
    except Exception as e:
        _set_state("err", "post: " + repr(e)[:60])
        return False
    finally:
        if r is not None:
            try:
                r.close()
            except Exception:
                pass


def _drain_pending(batch_url):
    """Try to send up to _MAX_DRAIN_BATCHES_PER_CYCLE batches per call.
    Stops early on the first failure (transient network blip — keep the
    rest for next cycle)."""
    for _ in range(_MAX_DRAIN_BATCHES_PER_CYCLE):
        records, lines_consumed = _read_first_batch(_BATCH_SIZE)
        if not records:
            # Either file is empty or all remaining lines are malformed.
            if lines_consumed > 0:
                _drop_head_lines(lines_consumed)
            return
        if _post_batch(batch_url, records):
            _drop_head_lines(lines_consumed)
            _mark_posted()
        else:
            return    # transient failure, try again next cycle


# --- Main loop --------------------------------------------------------

def _loop():
    global _interval_s
    url, device, interval, max_records = _load_config()
    if url is None:
        _set_state("off")
        return
    _interval_s = interval
    batch_url = _batch_url(url)
    _set_state("init")

    while True:
        # Pull whatever the sensor owner has cached. None until the
        # sensors thread has produced its first reading.
        snap = sensors.latest()
        now_epoch = int(time.time())

        # Skip recording if NTP hasn't synced — better to drop a reading
        # than to write a record with a bogus 1970-ish timestamp.
        if snap is not None and now_epoch >= _NTP_MIN_EPOCH:
            # The launcher sensor thread updates ~every 500 ms. If the
            # snapshot is older than 2× our interval, the sensor owner
            # is wedged — don't record stale data.
            age_ms = time.ticks_diff(time.ticks_ms(), snap.get("ts_ms", 0))
            if age_ms <= interval * 1000 * 2:
                record = {
                    "ts": now_epoch,
                    "device": device,
                }
                # Pass sensor floats through verbatim — JSON encodes IEEE
                # 754 doubles with shortest-roundtrip so the DB sees the
                # driver's full native precision (SHT30 ~0.0027°C step,
                # QMP6988 ~0.0006 hPa step). Earlier code rounded to 2
                # decimals which threw away most of that for no reason.
                if snap.get("temp_c") is not None:
                    record["temp_c"] = snap["temp_c"]
                if snap.get("humidity") is not None:
                    record["humidity"] = snap["humidity"]
                if snap.get("pressure_pa") is not None:
                    record["pressure_hpa"] = snap["pressure_pa"] / 100.0
                if snap.get("co2_ppm") is not None:
                    record["co2_ppm"] = int(snap["co2_ppm"])
                # Only record if we have at least one real value beyond
                # ts + device — else we're appending empty rows.
                if len(record) > 2:
                    _append_pending(record)
                    _truncate_if_overflow(max_records)

        # Try to drain regardless of whether we appended (covers the
        # case where the network was down for a while and we have a
        # backlog to catch up on).
        wifi_up = _wifi_ok()
        if wifi_up:
            _drain_pending(batch_url)
        else:
            _set_state("err", "no wifi")

        # Adaptive sleep: when wifi is down (or NTP not synced) we want
        # to retry quickly so the status bar recovers within seconds of
        # the network coming back, not on the next 30 s tick. When
        # everything is healthy we use the full configured interval.
        # Without this, a Cardputer that boots before its WiFi finishes
        # connecting would show red ENV for an entire interval — and
        # that's exactly the boot-time bug users hit, since wifi_init
        # is non-blocking and connect typically takes 5-15 s.
        healthy = wifi_up and now_epoch >= _NTP_MIN_EPOCH and snap is not None
        time.sleep(interval if healthy else min(interval, 3))


def start():
    """Idempotent. Spawns the daemon thread on first call."""
    global _started, _lock
    if _started:
        return
    if _thread is None:
        _set_state("off")
        return
    if _lock is None:
        try:
            _lock = _thread.allocate_lock()
        except Exception:
            _lock = None
    _started = True
    try:
        _thread.start_new_thread(_loop, ())
    except Exception as e:
        _started = False
        _set_state("err", "thread start: " + repr(e))
