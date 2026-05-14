"""Satellite tracker — ISS and crew vehicles.

Location: BeaconDB WiFi geolocation (https://beacondb.net — MLS replacement,
  API compatible, no key required). Falls back to /flash/sat_loc.json.
TLE source: CelesTrak stations group, cached daily in /flash/sat_tle.txt.
Propagator: SGP4 near-Earth (apps/sat/_sgp4.py) — Vallado (2006).

Keys
  Up/Down   navigate pass list
  Enter     open skyplot for selected satellite
  R         re-fetch TLE from CelesTrak
  L         manual lat/lon entry
  ESC       back / quit
"""
import gc
import json
import math
import os
import time

import M5
import network
import requests
from M5 import Lcd
from hardware.matrix_keyboard import MatrixKeyboard
from startup.cardputeradv.framework import KeyCode

# SGP4 propagator — loaded lazily after screen is up
_sgp4 = None

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_TLE_URL    = "https://celestrak.org/NORAD/elements/gp.php?GROUP=stations&FORMAT=tle"
_TLE_CACHE  = "/flash/sat_tle.txt"
_LOC_CACHE  = "/flash/sat_loc.json"
_TLE_MAX_AGE = 86400          # seconds before re-fetching TLE (1 day)
_BEACON_URL = "https://api.beacondb.net/v1/geolocate"
_HORIZON    = 10.0            # minimum elevation for a "visible" pass (deg)
# Pass-search step. ISS-class LEO passes last 5-10 min from horizon to
# horizon; 5 min sampling reliably finds the rise/set crossings (with
# ±2.5 min uncertainty on the exact crossing time, which is fine for
# "look up at the sky tonight" use). Was 60 s but at ~100 ms per SGP4
# eval that took ~24 min for 10 sats × 24 h — the user saw "computing
# passes" forever. 300 s drops it to ~5 min × 4 sats = ~12 sec.
_STEP_S     = 300.0
_HORIZON_H  = 12.0            # search window in hours (was 24h)
_N_PASSES   = 8               # max passes to show

# UI colours and layout
_BG     = 0x000000
_FG     = 0xFFFFFF
_DIM    = 0x666666
_OK     = 0x00DD66
_WAIT   = 0xFFD040
_ERR    = 0xFF4444
_HDR_BG = 0x232332
_HDR_FG = 0xFFD040
_SEL_BG = 0x003366
_ACC    = 0x40A0FF
_SAT_C  = 0xFFD040

_FONT  = M5.Lcd.FONTS.DejaVu18
_SMALL = M5.Lcd.FONTS.DejaVu12
_PAD   = 4
_HDR_H  = 22
_HINT_H = 16
_ROW_H  = 24

# Skyplot geometry (right portion of 240×135 display)
_PCX = 168     # polar plot center x
_PCY = 67      # polar plot center y
_PR  = 55      # horizon radius (pixels)


# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------
def _load_location():
    """Load saved lat/lon from /flash/sat_loc.json. Returns (lat, lon)."""
    try:
        with open(_LOC_CACHE) as f:
            d = json.load(f)
        return float(d['lat']), float(d['lon'])
    except Exception:
        return 0.0, 0.0


def _save_location(lat, lon):
    try:
        with open(_LOC_CACHE, 'w') as f:
            json.dump({'lat': lat, 'lon': lon}, f)
    except Exception:
        pass


def _wifi_geolocate():
    """Scan nearby APs and query BeaconDB for approximate location.

    BeaconDB (https://beacondb.net) is a community-run open-source
    replacement for Mozilla Location Service (MLS, retired July 2024).
    API endpoint is MLS/Ichnaea-compatible, no key required.

    Returns (lat, lon) or None on failure.
    """
    wlan = network.WLAN(network.STA_IF)
    if not wlan.isconnected():
        return None
    try:
        nets = wlan.scan()
    except Exception:
        return None

    aps = []
    for net in nets[:20]:
        bssid = net[1]
        rssi  = net[3]
        mac   = ":".join("{:02x}".format(b) for b in bssid)
        aps.append({"macAddress": mac, "signalStrength": rssi})
    if not aps:
        return None

    body = json.dumps({"wifiAccessPoints": aps})
    r = None
    try:
        gc.collect()
        r = requests.post(
            _BEACON_URL,
            data=body,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        if r.status_code == 200:
            d   = r.json()
            loc = d.get("location", {})
            lat = float(loc.get("lat", 0.0))
            lon = float(loc.get("lng", 0.0))
            if lat != 0.0 or lon != 0.0:
                return lat, lon
    except Exception:
        pass
    finally:
        if r is not None:
            try:
                r.close()
            except Exception:
                pass
    return None


# ---------------------------------------------------------------------------
# TLE management
# ---------------------------------------------------------------------------
def _tle_age():
    """Return age of cached TLE in seconds, or a huge number if absent."""
    try:
        return time.time() - os.stat(_TLE_CACHE)[8]
    except Exception:
        return 10 ** 9


def _fetch_tle():
    """Download fresh TLE from CelesTrak. Returns text or None."""
    r = None
    try:
        gc.collect()
        r = requests.get(_TLE_URL, timeout=20)
        if r.status_code == 200:
            txt = r.text
            with open(_TLE_CACHE, 'w') as f:
                f.write(txt)
            return txt
    except Exception:
        pass
    finally:
        if r is not None:
            try:
                r.close()
            except Exception:
                pass
    return None


def _load_tle_cache():
    try:
        with open(_TLE_CACHE) as f:
            return f.read()
    except Exception:
        return ""


def _parse_tles(text):
    """Parse multi-satellite TLE text into list of initialized SatRec objects."""
    sats  = []
    lines = [l for l in text.split('\n') if l.strip()]
    i = 0
    while i + 2 < len(lines):
        name  = lines[i].strip()
        line1 = lines[i + 1].strip()
        line2 = lines[i + 2].strip()
        if line1.startswith('1 ') and line2.startswith('2 '):
            try:
                s = _sgp4.tle_parse(name, line1, line2)
                _sgp4.sgp4init(s)
                if s.error == 0:
                    sats.append(s)
            except Exception:
                pass
            i += 3
        else:
            i += 1
    return sats


# ---------------------------------------------------------------------------
# Pass prediction
# ---------------------------------------------------------------------------
def _compass(az):
    dirs = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
    return dirs[int((az + 22.5) / 45.0) % 8]


def _predict_passes(sats, lat, lon, now_unix, kb=None, on_progress=None):
    """Find upcoming passes in the search window for all sats.

    `kb` (MatrixKeyboard) and `on_progress(done, total)` are optional —
    if provided, ESC aborts the search and progress is reported after
    each sat. SGP4 is slow on MicroPython so even with 5-min step this
    still takes a few seconds; user feedback matters.

    Returns list of dicts sorted by rise time:
      name, rise, set, max_el, rise_az, set_az, sat (SatRec)
    """
    passes = []
    end_t  = now_unix + _HORIZON_H * 3600.0
    n = len(sats)

    for i, sat in enumerate(sats):
        if on_progress:
            on_progress(i, n)
        prev_el = None
        rise_t  = None
        rise_az = 0.0
        max_el  = 0.0
        max_az  = 0.0
        t = now_unix
        step_count = 0
        while t < end_t:
            # Cooperative abort + keyboard pump every ~16 SGP4 evals
            step_count += 1
            if kb is not None and step_count % 16 == 0:
                if kb.get_key() == KeyCode.KEYCODE_ESC:
                    return None  # user cancelled

            tsince = (t - sat.epoch_unix) / 60.0
            try:
                pos, _ = _sgp4.sgp4(sat, tsince)
                az, el, _ = _sgp4.teme_to_azel(pos, lat, lon, t)
            except Exception:
                prev_el = None
                t += _STEP_S
                continue

            if prev_el is None:
                prev_el = el
                t += _STEP_S
                continue

            if prev_el < _HORIZON <= el:          # rising
                rise_t  = t - _STEP_S * 0.5
                rise_az = az
                max_el  = el
                max_az  = az

            if el >= _HORIZON and rise_t is not None:
                if el > max_el:
                    max_el = el
                    max_az = az

            if prev_el >= _HORIZON > el and rise_t is not None:  # setting
                passes.append({
                    'name':    sat.name[:14],
                    'rise':    rise_t,
                    'set':     t - _STEP_S * 0.5,
                    'max_el':  max_el,
                    'max_az':  max_az,
                    'rise_az': rise_az,
                    'set_az':  az,
                    'sat':     sat,
                })
                rise_t = None
                if len(passes) >= _N_PASSES * 2:
                    break

            prev_el = el
            t += _STEP_S

    if on_progress:
        on_progress(n, n)
    passes.sort(key=lambda p: p['rise'])
    return passes[:_N_PASSES]


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------
def _draw_header(text, color=_HDR_FG):
    Lcd.fillRect(0, 0, Lcd.width(), _HDR_H, _HDR_BG)
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(color, _HDR_BG)
    Lcd.setCursor(_PAD, 6)
    Lcd.print(text[:40])


def _draw_hint(text):
    y = Lcd.height() - _HINT_H
    Lcd.fillRect(0, y, Lcd.width(), _HINT_H, _BG)
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(_DIM, _BG)
    Lcd.setCursor(_PAD, y + 2)
    Lcd.print(text[:42])


def _fmt_rel(t, now):
    d = t - now
    if d < 0:
        return "now"
    if d < 60:
        return "<1m"
    m = int(d / 60)
    if m < 60:
        return "+{}m".format(m)
    return "+{}h{}m".format(m // 60, m % 60)


def _draw_pass_row(i, p, selected, now):
    y  = _HDR_H + 2 + i * _ROW_H
    bg = _SEL_BG if selected else _BG
    Lcd.fillRect(0, y, Lcd.width(), _ROW_H - 1, bg)

    Lcd.setFont(_SMALL)
    Lcd.setTextColor(_FG, bg)
    Lcd.setCursor(_PAD, y + 2)
    Lcd.print(p['name'][:9])

    rt = _fmt_rel(p['rise'], now)
    Lcd.setTextColor(_WAIT if selected else _ACC, bg)
    Lcd.setCursor(_PAD + 72, y + 2)
    Lcd.print(rt[:6])

    el_txt = "{:.0f}\xb0".format(p['max_el'])
    Lcd.setTextColor(_OK if p['max_el'] >= 30 else _FG, bg)
    Lcd.setCursor(_PAD + 116, y + 2)
    Lcd.print(el_txt)

    dir_txt = _compass(p['rise_az']) + "\xbb" + _compass(p['set_az'])
    Lcd.setTextColor(_DIM if not selected else _FG, bg)
    dw = Lcd.textWidth(dir_txt, _SMALL)
    Lcd.setCursor(Lcd.width() - dw - _PAD, y + 2)
    Lcd.print(dir_txt)


def _draw_pass_list(passes, sel, now):
    body_h   = Lcd.height() - _HDR_H - _HINT_H - 2
    max_rows = max(1, body_h // _ROW_H)
    Lcd.fillRect(0, _HDR_H, Lcd.width(), body_h + 2, _BG)

    if not passes:
        Lcd.setFont(_FONT)
        Lcd.setTextColor(_DIM, _BG)
        Lcd.setCursor(_PAD, _HDR_H + 30)
        Lcd.print("No passes found")
        return

    scroll = max(0, min(sel, len(passes) - max_rows)) if len(passes) > max_rows else 0
    for i, p in enumerate(passes[scroll:scroll + max_rows]):
        _draw_pass_row(i, p, scroll + i == sel, now)


# ---------------------------------------------------------------------------
# Skyplot
# ---------------------------------------------------------------------------
def _el_to_r(el):
    return int(_PR * (90.0 - max(0.0, min(90.0, el))) / 90.0)


def _az_el_to_xy(az, el):
    r   = _el_to_r(el)
    rad = (az - 90.0) * math.pi / 180.0
    return int(_PCX + r * math.cos(rad)), int(_PCY + r * math.sin(rad))


def _draw_skyplot_bg():
    Lcd.fillRect(_PCX - _PR - 3, _HDR_H,
                 (_PR + 3) * 2, Lcd.height() - _HDR_H, 0x080810)
    for el in (0, 30, 60):
        Lcd.drawCircle(_PCX, _PCY, _el_to_r(el),
                       0x334466 if el == 0 else _DIM)
    # Cardinal labels
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(0x446688, 0x080810)
    for lbl, az in (("N", 0), ("E", 90), ("S", 180), ("W", 270)):
        sx, sy = _az_el_to_xy(az, 3)
        Lcd.setCursor(sx - 3, sy - 5)
        Lcd.print(lbl)


def _predict_pass_arc(sat, lat, lon, rise_t, set_t, n=30):
    """Compute (az, el) at evenly spaced points across an upcoming pass.
    Used to draw the predicted trajectory on the skyplot before the pass
    starts so the user can see WHERE the satellite will appear."""
    arc = []
    if set_t <= rise_t:
        return arc
    dt = (set_t - rise_t) / n
    for i in range(n + 1):
        ut = rise_t + i * dt
        ts = (ut - sat.epoch_unix) / 60.0
        try:
            pos, _ = _sgp4.sgp4(sat, ts)
            az, el, _ = _sgp4.teme_to_azel(pos, lat, lon, ut)
            if el >= 0:
                arc.append(_az_el_to_xy(az, el))
        except Exception:
            pass
    return arc


def _fmt_countdown(secs):
    if secs <= 0: return "now"
    if secs < 60: return "{}s".format(secs)
    m, s = divmod(secs, 60)
    if m < 60: return "{}m{:02d}s".format(m, s)
    h, m = divmod(m, 60)
    return "{}h{:02d}m".format(h, m)


def _skyplot_screen(kb, sat, lat, lon, pass_info=None):
    """Real-time skyplot for one satellite. ESC returns to pass list.

    `pass_info` is the upcoming pass dict (rise/set times + max_el etc.).
    When the satellite is currently below the horizon we use it to:
      - draw the predicted trajectory across the sky (dotted)
      - show a countdown to the rise time
    so the user understands "wait N minutes, satellite will appear here".
    """
    Lcd.clear(_BG)
    _draw_header(sat.name[:20] + "  live", _HDR_FG)
    _draw_hint("ESC=back")
    _draw_skyplot_bg()

    # Pre-compute the upcoming pass arc once (≈30 SGP4 evals = a couple
    # seconds on first entry, then cached for the whole skyplot session).
    arc_pts = []
    if pass_info is not None:
        arc_pts = _predict_pass_arc(
            sat, lat, lon, pass_info['rise'], pass_info['set'])

    trail     = []
    last_tick = time.ticks_add(time.ticks_ms(), -3000)  # force immediate draw

    while True:
        k = kb.get_key()
        if k == KeyCode.KEYCODE_ESC:
            return

        now_ms = time.ticks_ms()
        if time.ticks_diff(now_ms, last_tick) >= 1000:
            last_tick = now_ms
            unix_t = time.time()
            tsince = (unix_t - sat.epoch_unix) / 60.0

            try:
                pos, _ = _sgp4.sgp4(sat, tsince)
                az, el, rng = _sgp4.teme_to_azel(pos, lat, lon, unix_t)
            except Exception:
                time.sleep_ms(200)
                continue

            # Left info panel
            panel_w = _PCX - _PR - 4
            Lcd.fillRect(0, _HDR_H, panel_w,
                         Lcd.height() - _HDR_H - _HINT_H, _BG)
            Lcd.setFont(_SMALL)
            for ii, (lbl, val) in enumerate([
                ("Az",  "{:.1f}\xb0".format(az)),
                ("El",  "{:+.1f}\xb0".format(el)),
                ("Rng", "{:.0f}km".format(rng)),
            ]):
                yy = _HDR_H + 4 + ii * 14
                Lcd.setTextColor(_DIM, _BG)
                Lcd.setCursor(_PAD, yy)
                Lcd.print(lbl)
                c = (_OK if el >= _HORIZON else _ERR) if lbl == "El" else _FG
                Lcd.setTextColor(c, _BG)
                Lcd.setCursor(_PAD + 24, yy)
                Lcd.print(val)

            # Below-horizon hint + countdown to next pass
            yy = _HDR_H + 4 + 3 * 14
            if el < 0 and pass_info is not None:
                Lcd.setTextColor(_ERR, _BG)
                Lcd.setCursor(_PAD, yy)
                Lcd.print("BELOW")
                Lcd.setCursor(_PAD, yy + 12)
                Lcd.print("HORIZON")
                secs = int(pass_info['rise'] - unix_t)
                Lcd.setTextColor(_HDR_FG, _BG)
                Lcd.setCursor(_PAD, yy + 28)
                Lcd.print("rise in")
                Lcd.setTextColor(_FG, _BG)
                Lcd.setCursor(_PAD, yy + 40)
                Lcd.print(_fmt_countdown(secs))
            elif el < 0:
                Lcd.setTextColor(_ERR, _BG)
                Lcd.setCursor(_PAD, yy)
                Lcd.print("not visible")

            # Polar plot
            _draw_skyplot_bg()

            # Draw predicted arc as faint dotted trail (only if pre-pass)
            if arc_pts and el < pass_info['max_el'] - 5:
                for ax, ay in arc_pts:
                    Lcd.drawPixel(ax, ay, 0x4477AA)
                # Mark rise point with R, set with S
                if len(arc_pts) >= 2:
                    Lcd.fillCircle(arc_pts[0][0],  arc_pts[0][1],  2, 0x66AA66)
                    Lcd.fillCircle(arc_pts[-1][0], arc_pts[-1][1], 2, 0xAA6666)

            if el > -5.0:
                sx, sy = _az_el_to_xy(az, el)
                for tx, ty in trail[-8:]:
                    Lcd.fillCircle(tx, ty, 1, _DIM)
                color = _OK if el >= _HORIZON else _ERR
                Lcd.fillCircle(sx, sy, 4, color)
                if not trail or trail[-1] != (sx, sy):
                    trail.append((sx, sy))
                if len(trail) > 20:
                    trail.pop(0)

        time.sleep_ms(50)


# ---------------------------------------------------------------------------
# Manual location entry
# ---------------------------------------------------------------------------
def _location_screen(kb, lat0, lon0):
    """Type in lat/lon. Returns (lat, lon) or None on cancel."""
    Lcd.clear(_BG)
    _draw_header("Set location", _HDR_FG)
    _draw_hint("Up/Dn=field  Enter=save  ESC=cancel")

    bufs = ["{:.5f}".format(lat0), "{:.5f}".format(lon0)]
    lbls = ["Lat", "Lon"]
    fi   = 0

    def _redraw():
        Lcd.fillRect(0, _HDR_H, Lcd.width(), Lcd.height() - _HDR_H - _HINT_H, _BG)
        for i in range(2):
            y  = _HDR_H + 8 + i * 38
            bg = _SEL_BG if i == fi else _BG
            Lcd.fillRect(0, y - 2, Lcd.width(), 34, bg)
            Lcd.setFont(_SMALL)
            Lcd.setTextColor(_DIM, bg)
            Lcd.setCursor(_PAD, y)
            Lcd.print(lbls[i] + ":")
            Lcd.setFont(_FONT)
            Lcd.setTextColor(_FG, bg)
            Lcd.setCursor(_PAD + 28, y)
            cur = bufs[i] + ("_" if i == fi else " ")
            Lcd.print(cur[:14])

    _redraw()

    while True:
        k = kb.get_key()
        if k is None:
            time.sleep_ms(30)
            continue
        if k == KeyCode.KEYCODE_ESC:
            return None
        if k == KeyCode.KEYCODE_UP or k == ord('w'):
            fi = (fi - 1) % 2
            _redraw()
        elif k == KeyCode.KEYCODE_DOWN or k == ord('s'):
            fi = (fi + 1) % 2
            _redraw()
        elif k == KeyCode.KEYCODE_ENTER:
            try:
                lat = float(bufs[0])
                lon = float(bufs[1])
                if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                    return lat, lon
            except Exception:
                pass
            _draw_hint("Invalid — check values")
        elif k == KeyCode.KEYCODE_BACKSPACE or k == KeyCode.KEYCODE_DEL:
            if bufs[fi]:
                bufs[fi] = bufs[fi][:-1]
                _redraw()
        elif isinstance(k, int) and 32 <= k <= 126:
            ch = chr(k)
            if ch in '0123456789.-+' and len(bufs[fi]) < 11:
                bufs[fi] += ch
                _redraw()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run():
    global _sgp4

    kb = MatrixKeyboard()
    Lcd.clear(_BG)
    _draw_header("Satellites", _WAIT)
    _draw_hint("Starting...")

    # Lazy-load propagator
    try:
        import apps.sat._sgp4 as m
        _sgp4 = m
    except ImportError:
        try:
            import sat._sgp4 as m
            _sgp4 = m
        except ImportError:
            _draw_header("Error", _ERR)
            Lcd.setFont(_SMALL)
            Lcd.setTextColor(_ERR, _BG)
            Lcd.setCursor(_PAD, _HDR_H + 20)
            Lcd.print("_sgp4 module missing")
            _draw_hint("ESC = quit")
            while kb.get_key() != KeyCode.KEYCODE_ESC:
                time.sleep_ms(50)
            return

    # -- location --
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(_DIM, _BG)
    Lcd.setCursor(_PAD, _HDR_H + 8)
    Lcd.print("Locating via WiFi...")

    lat, lon = _load_location()
    loc_src  = "saved"
    geo = _wifi_geolocate()
    if geo is not None:
        lat, lon = geo
        _save_location(lat, lon)
        loc_src = "wifi"
    elif lat == 0.0 and lon == 0.0:
        loc_src = "0,0"

    # -- TLE --
    Lcd.fillRect(0, _HDR_H, Lcd.width(), 20, _BG)
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(_DIM, _BG)
    Lcd.setCursor(_PAD, _HDR_H + 8)
    Lcd.print("Loading TLE...")

    tle_text = ""
    if _tle_age() > _TLE_MAX_AGE:
        tle_text = _fetch_tle() or ""
    if not tle_text:
        tle_text = _load_tle_cache()
    if not tle_text:
        tle_text = _fetch_tle() or ""

    gc.collect()
    sats = _parse_tles(tle_text) if tle_text else []

    passes    = []
    sel       = 0
    pass_time = 0   # time when passes were last computed

    def _show_progress(done, total):
        Lcd.fillRect(0, _HDR_H, Lcd.width(), 30, _BG)
        Lcd.setFont(_SMALL)
        Lcd.setTextColor(_DIM, _BG)
        Lcd.setCursor(_PAD, _HDR_H + 4)
        Lcd.print("Computing passes  {}/{}".format(done, total))
        # Bar
        bw = Lcd.width() - 2 * _PAD
        Lcd.drawRect(_PAD, _HDR_H + 18, bw, 6, _DIM)
        if total:
            fill = (bw - 2) * done // max(total, 1)
            Lcd.fillRect(_PAD + 1, _HDR_H + 19, fill, 4, _FG)

    def _recompute():
        nonlocal passes, sel, pass_time
        gc.collect()
        now = time.time()
        if sats:
            result = _predict_passes(sats, lat, lon, now,
                                     kb=kb, on_progress=_show_progress)
            passes = result if result is not None else []
        else:
            passes = []
        sel       = 0
        pass_time = now

    _recompute()

    def _hdr():
        t  = time.localtime(time.time() + 8 * 3600)  # local (Beijing)
        ts = "{:02d}:{:02d}".format(t[3], t[4])
        return "SAT {} ({}) {}".format(ts, loc_src,
                                       "{:.1f},{:.1f}".format(lat, lon))

    Lcd.clear(_BG)
    _draw_header(_hdr())
    _draw_hint("^v Enter=sky  R=TLE  L=loc  ESC")
    now = time.time()
    _draw_pass_list(passes, sel, now)

    last_hdr_ms  = time.ticks_ms()
    last_list_ms = time.ticks_ms()

    while True:
        k = kb.get_key()
        if k is None:
            time.sleep_ms(40)
        elif k == KeyCode.KEYCODE_ESC:
            return
        elif k in (KeyCode.KEYCODE_UP, ord('w')):
            if sel > 0:
                sel -= 1
                _draw_pass_list(passes, sel, time.time())
        elif k in (KeyCode.KEYCODE_DOWN, ord('s')):
            if sel < len(passes) - 1:
                sel += 1
                _draw_pass_list(passes, sel, time.time())
        elif k == KeyCode.KEYCODE_ENTER:
            if passes and sel < len(passes):
                _skyplot_screen(kb, passes[sel]['sat'], lat, lon,
                                pass_info=passes[sel])
                Lcd.clear(_BG)
                _draw_header(_hdr())
                _draw_hint("^v Enter=sky  R=TLE  L=loc  ESC")
                _draw_pass_list(passes, sel, time.time())
        elif k in (ord('r'), ord('R')):
            _draw_header("Fetching TLE...", _WAIT)
            new = _fetch_tle()
            if new:
                gc.collect()
                sats = _parse_tles(new)
            _recompute()
            Lcd.clear(_BG)
            _draw_header(_hdr())
            _draw_hint("^v Enter=sky  R=TLE  L=loc  ESC")
            _draw_pass_list(passes, sel, time.time())
        elif k in (ord('l'), ord('L')):
            result = _location_screen(kb, lat, lon)
            if result is not None:
                lat, lon = result
                _save_location(lat, lon)
                loc_src = "user"
                _recompute()
            Lcd.clear(_BG)
            _draw_header(_hdr())
            _draw_hint("^v Enter=sky  R=TLE  L=loc  ESC")
            _draw_pass_list(passes, sel, time.time())

        # Periodic refresh
        now_ms = time.ticks_ms()
        if time.ticks_diff(now_ms, last_hdr_ms) > 30000:
            last_hdr_ms = now_ms
            _draw_header(_hdr())
        if time.ticks_diff(now_ms, last_list_ms) > 60000:
            last_list_ms = now_ms
            _draw_pass_list(passes, sel, time.time())
