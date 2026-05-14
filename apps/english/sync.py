"""HTTP client for syncing word batches with the Mac companion app.

Wire protocol:

  POST /sync/checkin?t=<token>
    { "device_id": str,
      "prev_batch_id": int|null,
      "reviews": [ {"word_id": int, "viewed": bool,
                    "duration_ms": int, "played_audio": bool}, ... ] }
  -> { "batch_id": int, "date": "YYYY-MM-DD",
       "words": [ {"id": int, "headword": str, "pos": str, "ipa": str,
                   "def_en": str, "example": str, "pinyin": str,
                   "audio_sha": str}, ... ] }

  GET /audio/<id>.wav?t=<token>
  -> binary audio/wav

The Mac generates audio with `say` + `afconvert` (16 kHz, 16-bit, mono).
Audio is keyed by content sha so the device skips downloads if it already
has the same bytes.

Local layout under /flash/english/:
  english.json (configuration, loaded by app.py from /flash/english.json)
  words.json   (cached current batch)
  state.json   ({"prev_batch_id": N})
  audio/<id>.wav (one per word in the current batch)

On sync failure we fall back to whatever's already on disk so the user
can still browse offline.
"""

import gc
import json
import os
import socket
import time

try:
    import network
except ImportError:
    network = None

try:
    import requests as _requests
except ImportError:
    import urequests as _requests

_CFG_PATH = "/flash/english.json"
_DATA_DIR = "/flash/english"
_AUDIO_DIR = _DATA_DIR + "/audio"
_WORDS_PATH = _DATA_DIR + "/words.json"
_STATE_PATH = _DATA_DIR + "/state.json"

_HTTP_TIMEOUT = 5         # per-request budget once we know Mac is reachable
_PROBE_TIMEOUT = 1.5      # how long we wait to confirm Mac is on the LAN


def _ensure_dirs():
    for d in (_DATA_DIR, _AUDIO_DIR):
        try:
            os.mkdir(d)
        except OSError:
            pass


def load_config():
    """Return cfg dict or None if missing/invalid. Required keys:
    host, port, token. Optional: device_id (default 'cardputer-1')."""
    try:
        with open(_CFG_PATH) as f:
            cfg = json.load(f)
    except Exception:
        return None
    if not isinstance(cfg, dict):
        return None
    host = cfg.get("host") or ""
    port = cfg.get("port") or 0
    token = cfg.get("token") or ""
    if not host or not port:
        return None
    cfg.setdefault("device_id", "cardputer-1")
    cfg["token"] = token
    return cfg


def wifi_ok():
    if network is None:
        return False
    try:
        return bool(network.WLAN(network.STA_IF).isconnected())
    except Exception:
        return False


def server_reachable(cfg, timeout_s=_PROBE_TIMEOUT):
    """Quick TCP probe to cfg.host:cfg.port before any HTTP attempt.

    Why: urequests' `timeout` arg doesn't reliably cover the TCP connect
    phase on this MicroPython port — a Mac that's off / out-of-network
    causes lwIP to retransmit SYNs for tens of seconds, which froze the
    English app on entry. A direct socket.connect with settimeout *does*
    honor the timeout, so we use it as a fast liveness check.
    """
    s = socket.socket()
    try:
        s.settimeout(timeout_s)
        ai = socket.getaddrinfo(cfg["host"], cfg["port"])
        if not ai:
            return False
        s.connect(ai[0][-1])
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _base_url(cfg):
    return "http://{}:{}".format(cfg["host"], cfg["port"])


def _qs_token(cfg):
    t = cfg.get("token") or ""
    return ("?t=" + t) if t else ""


def load_state():
    try:
        with open(_STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    _ensure_dirs()
    try:
        with open(_STATE_PATH, "w") as f:
            json.dump(state, f)
    except Exception:
        pass


def load_cached_words():
    """Return (batch_id, words[]) or (None, [])."""
    try:
        with open(_WORDS_PATH) as f:
            data = json.load(f)
        return data.get("batch_id"), data.get("words", [])
    except Exception:
        return None, []


def _save_words(batch_id, words):
    _ensure_dirs()
    with open(_WORDS_PATH, "w") as f:
        json.dump({"batch_id": batch_id, "words": words}, f)


def _existing_audio_sha(word_id):
    """Cheap sha cache check: we don't actually hash, we keep a sidecar
    .sha file next to each .wav. If sidecar matches expected, skip download."""
    path = "{}/{}.sha".format(_AUDIO_DIR, word_id)
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return None


def _save_audio_sha(word_id, sha):
    try:
        with open("{}/{}.sha".format(_AUDIO_DIR, word_id), "w") as f:
            f.write(sha)
    except Exception:
        pass


def audio_path(word_id):
    return "{}/{}.wav".format(_AUDIO_DIR, word_id)


def _clean_old_audio(keep_ids):
    """Delete audio files for words that aren't in keep_ids."""
    keep = set(str(i) for i in keep_ids)
    try:
        entries = os.listdir(_AUDIO_DIR)
    except OSError:
        return
    for name in entries:
        if name.endswith(".wav") or name.endswith(".sha"):
            wid = name.rsplit(".", 1)[0]
            if wid not in keep:
                try:
                    os.remove(_AUDIO_DIR + "/" + name)
                except OSError:
                    pass


def checkin(cfg, prev_batch_id, reviews, status_cb=None):
    """POST reviews + prev_batch_id, receive next batch. Returns the parsed
    response dict on success, or None on any failure.

    status_cb(text) -> optional UI hook."""
    if status_cb:
        status_cb("checkin...")
    body = json.dumps({
        "device_id": cfg["device_id"],
        "prev_batch_id": prev_batch_id,
        "reviews": reviews or [],
    })
    url = _base_url(cfg) + "/sync/checkin" + _qs_token(cfg)
    r = None
    try:
        gc.collect()
        r = _requests.post(url, data=body,
                           headers={"Content-Type": "application/json"},
                           timeout=_HTTP_TIMEOUT)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None
    finally:
        if r is not None:
            try:
                r.close()
            except Exception:
                pass


def download_audio(cfg, word_id, expected_sha, status_cb=None):
    """Fetch /audio/<id>.wav and write to local audio dir. Skip if the
    sidecar sha matches expected_sha AND the .wav file is non-empty.
    Returns True on success or skip. On error or partial download, the
    target file is removed so the next call retries cleanly instead of
    leaving a 0-byte placeholder."""
    if expected_sha and _existing_audio_sha(word_id) == expected_sha:
        try:
            if os.stat(audio_path(word_id))[6] > 0:
                return True
        except OSError:
            pass  # sha matched but file missing — re-download
    if status_cb:
        status_cb("dl {}".format(word_id))
    url = "{}/audio/{}.wav{}".format(_base_url(cfg), word_id, _qs_token(cfg))
    r = None
    target = audio_path(word_id)
    try:
        gc.collect()
        r = _requests.get(url, timeout=_HTTP_TIMEOUT)
        if r.status_code != 200:
            return False
        body = r.content
        if not body:
            # Mac was mid-write to the file when we asked for it — empty
            # body. Don't create a 0-byte placeholder.
            return False
        with open(target, "wb") as f:
            f.write(body)
        # Verify what we wrote — if disk reports 0 bytes, the write
        # didn't actually persist; treat as failure.
        try:
            if os.stat(target)[6] == 0:
                os.remove(target)
                return False
        except OSError:
            return False
        if expected_sha:
            _save_audio_sha(word_id, expected_sha)
        return True
    except Exception:
        # Remove any partially-written file so the retry path sees a
        # clean miss instead of a stale empty/short file.
        try:
            os.remove(target)
        except OSError:
            pass
        return False
    finally:
        if r is not None:
            try:
                r.close()
            except Exception:
                pass


def ensure_synced(cfg, force=False, status_cb=None):
    """Returns dict {"batch_id": int|None, "words": [...], "online": bool}.

    Always returns something usable: on success populates a fresh batch from
    Mac and downloads audio; on failure falls back to the last cached batch
    so the user can still browse offline."""
    _ensure_dirs()
    cached_id, cached_words = load_cached_words()
    if cfg is None or not wifi_ok() or not server_reachable(cfg):
        return {"batch_id": cached_id, "words": cached_words, "online": False}

    state = load_state()
    prev_id = state.get("prev_batch_id")
    resp = checkin(cfg, prev_id, [], status_cb=status_cb)
    if not resp:
        return {"batch_id": cached_id, "words": cached_words, "online": False}

    batch_id = resp.get("batch_id")
    words = resp.get("words", []) or []
    # Download all audio for the new batch.
    for w in words:
        sha = w.get("audio_sha") or ""
        wid = w.get("id")
        if wid is None:
            continue
        download_audio(cfg, wid, sha, status_cb=status_cb)
    # Persist new batch + state, prune stale audio.
    _save_words(batch_id, words)
    save_state({"prev_batch_id": batch_id})
    _clean_old_audio([w.get("id") for w in words if w.get("id") is not None])
    return {"batch_id": batch_id, "words": words, "online": True}


def submit_reviews(cfg, batch_id, reviews, status_cb=None):
    """Upload reviews without rotating to a new batch. We still POST to
    /sync/checkin but ignore whatever batch comes back — the Mac uses the
    presence of the same batch_id as 'no rotation requested'. We just don't
    persist the response."""
    if cfg is None or not wifi_ok() or not reviews:
        return False
    if not server_reachable(cfg):
        return False
    body = json.dumps({
        "device_id": cfg["device_id"],
        "prev_batch_id": batch_id,
        "reviews": reviews,
        "skip_rotation": True,
    })
    url = _base_url(cfg) + "/sync/checkin" + _qs_token(cfg)
    r = None
    try:
        gc.collect()
        r = _requests.post(url, data=body,
                           headers={"Content-Type": "application/json"},
                           timeout=_HTTP_TIMEOUT)
        return r.status_code == 200
    except Exception:
        return False
    finally:
        if r is not None:
            try:
                r.close()
            except Exception:
                pass
