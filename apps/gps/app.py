"""GPS receiver — M5Stack GPS Unit V1.1 (AT6668 + MAX2659 LNA).

Multi-GNSS: GPS / BD2 / BD3 / GALILEO / GLONASS / QZSS. NMEA 0183 over
UART at 115200 8N1 (the V1.1 module ships at 115200, not the 9600
quoted in older docs).

Wiring: Grove HY2.0-4P PortA. Yellow=TXD on G1 (MCU RX), white=RXD on
G2 (MCU TX). The same wires are used by the I2C bus that drives the
ENV.III / CO2 units, so this app and the env app are mutually
exclusive — launcher only runs one at a time anyway.

Reception: indoors with a tiny ceramic patch antenna a cold start can
take many minutes. Move within sight of a window for a fix.

Optional: Supabase upload. Drop a `/flash/gps_upload.json` containing
{"url": "https://<ref>.supabase.co", "key": "<anon>", "device": "<id>"}
and fixes are batched + POSTed to the `gps_log` table. Without that
file uploads are silently disabled.

Keys
  ESC       quit
"""
import gc
import json
import time

import M5
import network
from machine import Pin, UART
from M5 import Lcd
from hardware.matrix_keyboard import MatrixKeyboard
from startup.cardputeradv.framework import KeyCode

_BAUD = 115200
_RX = 1
_TX = 2

_CFG_FILE = "/flash/gps_upload.json"
_BUFFER_FILE = "/flash/gps_pending.ndjson"  # newline-delimited JSON
_SAMPLE_PERIOD_MS = 5000          # one point per 5 s when fix valid
_DISK_FLUSH_PERIOD_MS = 60_000    # memory → flash file every 1 min
_UPLOAD_PERIOD_MS = 600_000       # flash → Supabase every 10 min
_UPLOAD_CHUNK = 200               # rows per POST (~30 KB per request)
_HARD_CAP_ROWS = 10_000           # ~14 h offline at 5 s sampling
_GC_PERIOD_MS = 30_000            # explicit GC keeps fragmentation low

_BG = 0x000000
_DIM = 0x666666
_FG = 0xFFFFFF
_LAT_C = 0xFF8C64
_LON_C = 0x64C8FF
_SAT_C = 0xFFD040
_ALT_C = 0xB4FF8C
_TIME_C = 0xCCCCCC
_OK = 0x00DD66
_WAIT = 0xFFD040
_ERR = 0xFF4444
_OFF = 0x444444
_HDR_BG = 0x232332
_HDR_FG = 0xFFD040

_LABEL_FONT = M5.Lcd.FONTS.DejaVu12
_VAL_FONT = M5.Lcd.FONTS.DejaVu18

_PAD = 6
_HDR_H = 22
_ROW_TOP = 23
_ROW_H = 18


# ---- NMEA parsing -------------------------------------------------------

def _checksum_ok(line):
    """Validate a NMEA sentence: XOR of bytes between '$' and '*' equals
    the two hex digits after '*'. Filters out line noise from the shared
    bus."""
    if not line.startswith("$") or "*" not in line:
        return False
    body, _, cs = line[1:].partition("*")
    if len(cs) < 2:
        return False
    x = 0
    for c in body:
        x ^= ord(c)
    try:
        return x == int(cs[:2], 16)
    except ValueError:
        return False


def _to_deg(coord, hemi):
    """NMEA ddmm.mmmm / dddmm.mmmm → signed decimal degrees."""
    if not coord or not hemi:
        return None
    try:
        f = float(coord)
    except ValueError:
        return None
    deg = int(f // 100)
    minutes = f - deg * 100
    d = deg + minutes / 60.0
    if hemi in ("S", "W"):
        d = -d
    return d


def _parse_rmc(fields, state):
    if len(fields) < 10:
        return
    state["time"] = fields[1]
    state["lat"] = _to_deg(fields[3], fields[4])
    state["lon"] = _to_deg(fields[5], fields[6])
    try:
        state["spd_kmh"] = float(fields[7]) * 1.852
    except (ValueError, TypeError):
        state["spd_kmh"] = None
    try:
        state["course"] = float(fields[8])
    except (ValueError, TypeError):
        state["course"] = None
    state["date"] = fields[9]
    state["valid"] = fields[2] == "A"


def _parse_gga(fields, state):
    if len(fields) < 10:
        return
    try:
        state["fix"] = int(fields[6])
    except (ValueError, TypeError):
        state["fix"] = 0
    try:
        state["sats"] = int(fields[7])
    except (ValueError, TypeError):
        state["sats"] = 0
    try:
        state["hdop"] = float(fields[8])
    except (ValueError, TypeError):
        state["hdop"] = None
    try:
        state["alt"] = float(fields[9])
    except (ValueError, TypeError):
        state["alt"] = None


def _parse_gsv(fields, state):
    # Total satellites in view is field [3] (constant across the 1..N
    # GSV messages of a constellation; AT6668 emits GP/BD/GA/GL/QZ
    # separately, so we sum into a per-constellation dict).
    if len(fields) < 4:
        return
    talker = fields[0][1:3]  # 'GP', 'BD', 'GA', 'GL', 'QZ', 'GN'
    try:
        n = int(fields[3])
    except (ValueError, TypeError):
        return
    state.setdefault("in_view", {})[talker] = n


# ---- Supabase upload ----------------------------------------------------

def _load_upload_cfg():
    """Read /flash/gps_upload.json. Returns dict or None to disable
    uploads entirely. Source-code default is no upload — credentials
    live device-local per CLAUDE.md."""
    try:
        with open(_CFG_FILE) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return None
    if not cfg.get("url") or not cfg.get("key"):
        return None
    cfg.setdefault("device", "cardputer")
    cfg["endpoint"] = cfg["url"].rstrip("/") + "/rest/v1/gps_log"
    return cfg


def _iso_utc(date_str, time_str):
    """NMEA date=ddmmyy + time=hhmmss[.ss] → ISO 8601 UTC string."""
    if not date_str or not time_str:
        return None
    if len(date_str) != 6 or len(time_str) < 6:
        return None
    return "20{}-{}-{}T{}:{}:{}Z".format(
        date_str[4:6], date_str[2:4], date_str[:2],
        time_str[:2], time_str[2:4], time_str[4:6],
    )


def _build_sample(state, device):
    if not state.get("valid") or state.get("lat") is None:
        return None
    s = {"lat": state["lat"], "lon": state["lon"], "device": device}
    for k in ("alt", "spd_kmh", "course", "sats", "hdop"):
        v = state.get(k)
        if v is not None:
            s[k] = v
    ts = _iso_utc(state.get("date"), state.get("time"))
    if ts:
        s["gps_ts"] = ts
    return s


def _count_rows(path):
    n = 0
    try:
        with open(path, "rb") as f:
            for _ in f:
                n += 1
    except OSError:
        pass
    return n


def _append_disk(path, samples):
    if not samples:
        return 0
    try:
        with open(path, "a") as f:
            for s in samples:
                f.write(json.dumps(s))
                f.write("\n")
        return len(samples)
    except OSError:
        return 0


def _read_disk(path):
    """Read NDJSON file into a list. Skips malformed lines silently."""
    samples = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    samples.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return samples


def _delete_disk(path):
    try:
        import os
        os.remove(path)
    except OSError:
        pass


def _upload_chunks(cfg, samples):
    """POST `samples` in chunks. Returns (ok, sent_count)."""
    sent = 0
    for i in range(0, len(samples), _UPLOAD_CHUNK):
        chunk = samples[i:i + _UPLOAD_CHUNK]
        ok, _err = _flush(cfg, chunk)
        if not ok:
            return False, sent
        sent += len(chunk)
        gc.collect()  # free TLS buffers between chunks
    return True, sent


def _flush(cfg, batch):
    """POST the batch as a JSON array (PostgREST bulk insert). Returns
    (ok:bool, err:str|None). Blocks for ~3 s on success (TLS handshake
    + request). Caller should drain UART after."""
    if not batch:
        return True, None
    if not network.WLAN(network.STA_IF).isconnected():
        return False, "no wifi"
    try:
        import urequests as requests
    except ImportError:
        import requests
    gc.collect()
    r = None
    try:
        r = requests.post(
            cfg["endpoint"],
            json=batch,
            headers={
                "apikey": cfg["key"],
                "Authorization": "Bearer " + cfg["key"],
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
        )
        ok = 200 <= r.status_code < 300
        return ok, None if ok else "HTTP{}".format(r.status_code)
    except Exception as e:
        return False, type(e).__name__
    finally:
        if r is not None:
            try:
                r.close()
            except Exception:
                pass


# ---- UI -----------------------------------------------------------------

def _fix_status(fix):
    return ("no fix", "GPS", "DGPS", "PPS", "RTK", "FRTK", "EST", "MAN", "SIM")[
        fix if 0 <= fix < 9 else 0
    ]


def _draw_header(state, up):
    """up: dict with keys 'enabled', 'busy', 'last_ok', 'pending',
    'sent'. Header shows fix status on left, an upload count + colored
    dot on the right."""
    Lcd.fillRect(0, 0, Lcd.width(), _HDR_H, _HDR_BG)
    Lcd.setFont(_LABEL_FONT)
    Lcd.setTextColor(_HDR_FG, _HDR_BG)
    Lcd.setCursor(_PAD, 6)
    Lcd.print("GPS")

    Lcd.setTextColor(_DIM, _HDR_BG)
    Lcd.setCursor(_PAD + 28, 6)
    Lcd.print(_fix_status(state.get("fix", 0)))

    # Upload counter + dot on the right side
    if up["enabled"]:
        if up["busy"]:
            txt = "..."
            udot = _WAIT
        else:
            txt = "{}+{}".format(up["sent"], up["pending"])
            if up["last_ok"] is None:
                udot = _OFF
            elif up["last_ok"]:
                udot = _OK if up["pending"] == 0 else _WAIT
            else:
                udot = _ERR
        Lcd.setTextColor(_DIM, _HDR_BG)
        tw = Lcd.textWidth(txt, _LABEL_FONT)
        Lcd.setCursor(Lcd.width() - tw - 22, 6)
        Lcd.print(txt)
        Lcd.fillCircle(Lcd.width() - 18, _HDR_H // 2, 3, udot)

    fix = state.get("fix", 0)
    if fix > 0:
        c = _OK
    elif state.get("rx"):
        c = _WAIT
    else:
        c = _ERR
    Lcd.fillCircle(Lcd.width() - 8, _HDR_H // 2, 4, c)


def _draw_row(idx, label, val, val_color):
    y = _ROW_TOP + idx * _ROW_H
    Lcd.fillRect(0, y, Lcd.width(), _ROW_H, _BG)
    Lcd.setFont(_LABEL_FONT)
    Lcd.setTextColor(_DIM, _BG)
    Lcd.setCursor(_PAD, y + 4)
    Lcd.print(label)
    Lcd.setFont(_VAL_FONT)
    Lcd.setTextColor(val_color, _BG)
    tw = Lcd.textWidth(val, _VAL_FONT)
    Lcd.setCursor(Lcd.width() - tw - _PAD, y)
    Lcd.print(val)


def _fmt_lat(v):
    if v is None:
        return "--"
    return "{:.5f} {}".format(abs(v), "N" if v >= 0 else "S")


def _fmt_lon(v):
    if v is None:
        return "--"
    return "{:.5f} {}".format(abs(v), "E" if v >= 0 else "W")


def _fmt_sat(state):
    sats = state.get("sats")
    if sats is None:
        return "--"
    in_view = state.get("in_view") or {}
    total_view = sum(in_view.values()) if in_view else sats
    hdop = state.get("hdop")
    if hdop is None:
        return "{}/{}".format(sats, total_view)
    return "{}/{}  H{:.1f}".format(sats, total_view, hdop)


def _fmt_utc(state):
    t = state.get("time") or ""
    d = state.get("date") or ""
    if len(t) < 6 or len(d) != 6:
        return "--"
    return "{}-{} {}:{}:{}".format(d[2:4], d[:2], t[:2], t[2:4], t[4:6])


def _fmt_spd(state):
    s = state.get("spd_kmh")
    c = state.get("course")
    if s is None:
        return "--"
    if c is None or s < 0.5:
        return "{:.1f} km/h".format(s)
    return "{:.1f} km/h  {:.0f}".format(s, c)


def _fmt_alt(state):
    a = state.get("alt")
    if a is None:
        return "--"
    return "{:.1f} m".format(a)


# ---- main ---------------------------------------------------------------

def run():
    Lcd.clear(_BG)
    state = {"fix": 0, "rx": False}
    cfg = _load_upload_cfg()
    up = {
        "enabled": cfg is not None,
        "busy": False,
        "last_ok": None,
        "pending": 0,
        "sent": 0,
    }
    # Carry over any rows that didn't make it last session.
    disk_count = _count_rows(_BUFFER_FILE) if cfg else 0
    up["pending"] = disk_count

    _draw_header(state, up)
    _draw_row(0, "LAT", "--", _LAT_C)
    _draw_row(1, "LON", "--", _LON_C)
    _draw_row(2, "SAT", "--", _SAT_C)
    _draw_row(3, "UTC", "--", _TIME_C)
    _draw_row(4, "SPD", "--", _FG)
    _draw_row(5, "ALT", "--", _ALT_C)

    kb = MatrixKeyboard()
    # rxbuf 4 KB: covers ~16 s of NMEA at the V1.1 module's data rate,
    # giving comfortable margin for the ~3 s blocking POST every 10 min.
    uart = UART(1, baudrate=_BAUD, rx=_RX, tx=_TX,
                timeout=20, rxbuf=4096)

    buf = bytearray()
    mem = []  # rows collected since last disk flush
    last_redraw = -10_000
    last_sample = -10_000
    last_disk_flush = time.ticks_ms()  # don't write empty file on startup
    last_upload = time.ticks_ms()      # delay first upload by full period
    last_gc = time.ticks_ms()
    try:
        while True:
            if kb.get_key() == KeyCode.KEYCODE_ESC:
                return

            n = uart.any()
            if n:
                data = uart.read(n)
                if data:
                    state["rx"] = True
                    buf.extend(data)
                    while True:
                        idx = buf.find(b"\n")
                        if idx < 0:
                            break
                        line = bytes(buf[:idx]).strip().decode("ascii", "ignore")
                        buf = buf[idx + 1:]
                        try:
                            if not _checksum_ok(line):
                                continue
                            body = line.split("*", 1)[0]
                            fields = body.split(",")
                            tag = fields[0][3:] if len(fields[0]) >= 6 else ""
                            if tag == "RMC":
                                _parse_rmc(fields, state)
                            elif tag == "GGA":
                                _parse_gga(fields, state)
                            elif tag == "GSV":
                                _parse_gsv(fields, state)
                        except Exception:
                            # One malformed line shouldn't take down the
                            # whole app — drop it and keep going.
                            pass

            now = time.ticks_ms()

            # Sample one point per 5 s when fix is valid.
            if cfg and time.ticks_diff(now, last_sample) >= _SAMPLE_PERIOD_MS:
                s = _build_sample(state, cfg["device"])
                if s is not None and disk_count + len(mem) < _HARD_CAP_ROWS:
                    mem.append(s)
                last_sample = now

            # Memory → flash file every 1 min.
            if cfg and time.ticks_diff(now, last_disk_flush) >= _DISK_FLUSH_PERIOD_MS:
                if mem:
                    n_written = _append_disk(_BUFFER_FILE, mem)
                    disk_count += n_written
                    mem = []
                last_disk_flush = now

            # Flash → Supabase every 10 min. This is the only step that
            # blocks on TLS. Includes a pre-flush of memory so nothing
            # gets left behind.
            if cfg and time.ticks_diff(now, last_upload) >= _UPLOAD_PERIOD_MS:
                if mem:
                    disk_count += _append_disk(_BUFFER_FILE, mem)
                    mem = []
                if disk_count > 0:
                    up["busy"] = True
                    _draw_header(state, up)
                    samples = _read_disk(_BUFFER_FILE)
                    ok, sent = _upload_chunks(cfg, samples)
                    up["busy"] = False
                    up["last_ok"] = ok
                    if ok:
                        up["sent"] += sent
                        _delete_disk(_BUFFER_FILE)
                        disk_count = 0
                    # On failure keep the file; next cycle retries.
                    gc.collect()
                last_upload = now
                last_redraw = -10_000  # force redraw with new counts

            up["pending"] = disk_count + len(mem)

            # Periodic GC keeps mbedtls fragmentation in check.
            if time.ticks_diff(now, last_gc) >= _GC_PERIOD_MS:
                gc.collect()
                last_gc = now

            if time.ticks_diff(now, last_redraw) >= 500:
                _draw_header(state, up)
                _draw_row(0, "LAT", _fmt_lat(state.get("lat")), _LAT_C)
                _draw_row(1, "LON", _fmt_lon(state.get("lon")), _LON_C)
                _draw_row(2, "SAT", _fmt_sat(state), _SAT_C)
                _draw_row(3, "UTC", _fmt_utc(state), _TIME_C)
                _draw_row(4, "SPD", _fmt_spd(state), _FG)
                _draw_row(5, "ALT", _fmt_alt(state), _ALT_C)
                last_redraw = now

            time.sleep_ms(20)
    finally:
        # Don't lose in-memory samples on exit — write them to flash
        # so the next session picks them up.
        if cfg and mem:
            try:
                _append_disk(_BUFFER_FILE, mem)
            except Exception:
                pass
        try:
            uart.deinit()
        except Exception:
            pass
