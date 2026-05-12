# Cardputer launcher: scans /flash/apps recursively. Folders containing an
# app.py are launchable apps; folders that don't but contain app subfolders
# are *categories* (one level deep). Categories appear as folder icons in the
# root list; press Enter to descend, ESC/Backspace to return.
#
# Examples:
#   apps/clock/app.py            → root-level "clock" app
#   apps/games/snake/app.py      → "games" category, "snake" app inside
#   apps/games/bounce/app.py     → same category, second app
#
# App contract: <appdir>/app.py exports `def run()` (no args).
#
# Top of screen is a Mac-style status bar drawn in run():
#   left  : WiFi icon (3 arcs, color = connection state)
#   right : battery icon + percent

import json
import os
import sys
import time

import M5
import network
from M5 import Lcd
from hardware.matrix_keyboard import MatrixKeyboard
from startup.cardputeradv.framework import KeyCode

_APPS_DIR = "/flash/apps"
_WIFI_CFG = "/flash/wifi.json"

# Last-resort fallback if /flash/wifi.json is missing or empty.
# Hard-coded fallback used only when /flash/wifi.json is empty / missing.
# Leave blank in the source tree — set via the WiFi app at runtime, which
# persists creds to /flash/wifi.json.
_DEFAULT_SSID = ""
_DEFAULT_PWD = ""

_WIFI_RETRY_MS = 10000
_STATUS_REFRESH_MS = 333

_BG = 0x000000
_FG = 0xFFFFFF
_SEL_BG = 0xFFFFFF
_SEL_FG = 0x000000
_DIM = 0x666666
_OK = 0x00DD66
_WAIT = 0xFFD040
_ERR = 0xFF4444
_BAR_LINE = 0x303040

_FONT = M5.Lcd.FONTS.DejaVu18
_SMALL = M5.Lcd.FONTS.DejaVu12
_LINE_H = 24
_ICON_SIZE = 18
_ICON_X = 6
_TEXT_X = _ICON_X + _ICON_SIZE + 6
_PAD = 6
_STATUS_H = 22
_LIST_TOP = 26   # right below status bar + 2px gap
_BREADCRUMB_H = 14   # extra header row shown only inside a category
_SCROLLBAR_W = 3
_MAX_VISIBLE = 4  # at root: (135 - 26 - small bottom margin) // 24
_MAX_VISIBLE_CAT = 3  # in category view, breadcrumb steals one row

_kb = None
_wlan = None
_last_retry = 0
_last_status_drawn = None   # cache so we only repaint on change
_icon_cache = {}            # path -> draw fn or None (no icon)

_BJ_OFFSET = 8 * 3600
_NTP_HOST = "ntp.aliyun.com"
_NTP_RETRY_MS = 15000
_ntp_synced = False
_ntp_last_try = 0

# Charging detection — Cardputer-Adv has NO PMIC. Per M5Unified source
# (src/utility/Power_Class.cpp), this board uses pmic_t::pmic_adc, meaning
# battery voltage is read straight from an ADC on G10 with a 2:1 divider.
# isCharging() falls through to the enum default (always "True") and
# getVBUSVoltage() returns -1 — both unusable.
#
# We infer charging from cell voltage with HYSTERESIS so we don't flicker
# at the threshold:
#   - Was discharging, voltage rises above HIGH_MV → flip to charging.
#   - Was charging,    voltage drops below LOW_MV  → flip to discharging.
#   - Anywhere between → keep current state.
# Hi/Lo gap of ~120 mV is wide enough that ADC noise can't toggle it.
_BATT_HIGH_MV = 4220   # only reachable while a charger sources current
_BATT_LOW_MV  = 4100   # under load with no charger, drops here within sec
_battery_ema = None
_battery_v_ema = None
_BATTERY_EMA_A = 0.2
_charging_state = False


def _load_known_networks():
    """Return a list of saved {'ssid', 'password'} dicts plus the hard-coded
    default. Tolerates the legacy single-network format (`{ssid, password}`)."""
    nets = []
    try:
        with open(_WIFI_CFG) as f:
            cfg = json.load(f)
        if isinstance(cfg, dict) and "networks" in cfg:
            nets = [n for n in cfg["networks"]
                    if isinstance(n, dict) and n.get("ssid")]
        elif isinstance(cfg, dict) and cfg.get("ssid"):
            nets = [{"ssid": cfg["ssid"], "password": cfg.get("password", "")}]
    except Exception:
        pass
    if _DEFAULT_SSID and not any(n["ssid"] == _DEFAULT_SSID for n in nets):
        nets.append({"ssid": _DEFAULT_SSID, "password": _DEFAULT_PWD})
    return nets


def _load_creds():
    """Pick the first saved network — for the boot path we have to commit
    before scanning. Fast retry path that covers most cases (single home AP
    or last-used AP)."""
    nets = _load_known_networks()
    if nets:
        n = nets[0]
        return n["ssid"], n.get("password", "")
    return _DEFAULT_SSID, _DEFAULT_PWD


def _pick_known_visible(wlan):
    """Scan and return (ssid, password) of the strongest visible saved
    network, or None if scan fails / no match. Used by the retry path so
    that when the first saved AP isn't around, we hop to whichever known
    one *is*."""
    nets = _load_known_networks()
    if not nets:
        return None
    by_ssid = {n["ssid"]: n.get("password", "") for n in nets}
    try:
        raw = wlan.scan()
    except OSError:
        return None
    best_ssid, best_rssi = None, -999
    for net in raw:
        try:
            ssid = net[0].decode("utf-8")
        except Exception:
            continue
        if ssid in by_ssid and net[3] > best_rssi:
            best_ssid, best_rssi = ssid, net[3]
    if best_ssid is None:
        return None
    return best_ssid, by_ssid[best_ssid]


def _load_icon(path):
    """Import apps/<path>/icon.py exposing draw(lcd, x, y, size, on_dark).
    `path` is the dotted module suffix, e.g. "snake" or "games.snake".
    Returns the draw fn or None. Cached after first call."""
    if path in _icon_cache:
        return _icon_cache[path]
    fn = None
    try:
        mod = __import__("apps." + path + ".icon", None, None, ["draw"])
        fn = getattr(mod, "draw", None)
    except Exception:
        fn = None
    _icon_cache[path] = fn
    return fn


def _draw_default_icon(name, x, y, size, on_dark):
    """Fallback: colored square with the app's first letter inside."""
    color = 0x4488FF
    Lcd.fillRect(x, y, size, size, color)
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(_FG, color)
    ch = name[:1].upper() if name else "?"
    cw = Lcd.textWidth(ch, _SMALL)
    Lcd.setCursor(x + (size - cw) // 2, y + 3)
    Lcd.print(ch)


def _draw_folder_icon(x, y, size, on_dark):
    """Manila folder: small tab on top-left, body below. Yellow on any bg."""
    body = 0xFFD040
    edge = 0xAA8020
    tab_w = max(4, size // 2)
    tab_h = max(2, size // 5)
    body_y = y + tab_h
    body_h = size - tab_h - 1
    Lcd.fillRect(x, y + 1, tab_w, tab_h + 1, body)
    Lcd.fillRect(x, body_y, size, body_h, body)
    Lcd.drawLine(x, body_y, x + size - 1, body_y, edge)


def _draw_item_icon(item, x, y, size, on_dark):
    if item["kind"] == "category":
        _draw_folder_icon(x, y, size, on_dark)
        return
    fn = _load_icon(item["path"])
    if fn is None:
        _draw_default_icon(item["name"], x, y, size, on_dark)
        return
    try:
        fn(Lcd, x, y, size, on_dark)
    except Exception:
        _draw_default_icon(item["name"], x, y, size, on_dark)


def _is_dir(path):
    try:
        return bool(os.stat(path)[0] & 0x4000)
    except OSError:
        return False


def _has_app_py(dir_path):
    try:
        return "app.py" in os.listdir(dir_path)
    except OSError:
        return False


def _list_items(category=None):
    """List launcher entries for a view. At root (`category=None`) returns
    folder categories first, then root-level apps. Inside a category
    returns only that category's apps.

    Each item is a dict:
      {"kind": "app", "name": "snake", "path": "games.snake"}
      {"kind": "category", "name": "games", "count": 2}
    """
    base = _APPS_DIR if category is None else _APPS_DIR + "/" + category
    try:
        names = os.listdir(base)
    except OSError:
        return []
    apps = []
    cats = []
    for name in names:
        if name.startswith("."):
            continue
        full = base + "/" + name
        if not _is_dir(full):
            continue
        if _has_app_py(full):
            path = (category + "." + name) if category else name
            apps.append({"kind": "app", "name": name, "path": path})
        elif category is None:
            # Bare folder at root → potential category. Only count it if it
            # actually contains at least one app subfolder.
            count = 0
            try:
                for sub in os.listdir(full):
                    if _is_dir(full + "/" + sub) and _has_app_py(full + "/" + sub):
                        count += 1
            except OSError:
                pass
            if count > 0:
                cats.append({"kind": "category", "name": name, "count": count})
    apps.sort(key=lambda x: x["name"])
    cats.sort(key=lambda x: x["name"])
    return cats + apps


# --- WiFi -----------------------------------------------------------------

def _wifi_init():
    global _wlan, _last_retry
    _wlan = network.WLAN(network.STA_IF)
    if not _wlan.active():
        _wlan.active(True)
    if not _wlan.isconnected():
        ssid, pwd = _load_creds()
        try:
            _wlan.connect(ssid, pwd)
        except OSError:
            pass
    _last_retry = time.ticks_ms()


def _wifi_state():
    """Returns 'ok' | 'wait' | 'err'."""
    if _wlan.isconnected():
        return "ok"
    s = _wlan.status()
    if s == network.STAT_CONNECTING or s == network.STAT_IDLE:
        return "wait"
    return "err"


def _wifi_retry_if_needed(state):
    global _last_retry
    if state == "ok":
        return
    if time.ticks_diff(time.ticks_ms(), _last_retry) < _WIFI_RETRY_MS:
        return
    # Prefer whatever known AP is visible right now (so we auto-hop home /
    # phone hotspot / office), fall back to the first saved one blindly.
    pick = _pick_known_visible(_wlan)
    if pick is None:
        ssid, pwd = _load_creds()
    else:
        ssid, pwd = pick
    try:
        _wlan.disconnect()
    except OSError:
        pass
    try:
        _wlan.connect(ssid, pwd)
    except OSError:
        pass
    _last_retry = time.ticks_ms()


def _maybe_ntp_sync():
    """Best-effort NTP sync. Retries every _NTP_RETRY_MS until successful."""
    global _ntp_synced, _ntp_last_try
    if _ntp_synced:
        return
    if not _wlan.isconnected():
        return
    if time.ticks_diff(time.ticks_ms(), _ntp_last_try) < _NTP_RETRY_MS:
        return
    _ntp_last_try = time.ticks_ms()
    try:
        import ntptime
        ntptime.host = _NTP_HOST
        ntptime.settime()
        _ntp_synced = True
    except Exception:
        pass


def _bj_clock_text():
    """Returns 'HH:MM' Beijing time, or '' if RTC isn't synced yet."""
    if not _ntp_synced:
        return ""
    t = time.localtime(time.time() + _BJ_OFFSET)
    return "{:02d}:{:02d}".format(t[3], t[4])


# --- Icon drawing ---------------------------------------------------------

def _draw_wifi_icon(cx, cy, color):
    # Three stacked arcs over a center dot, all 90° wide centered at the top.
    # M5GFX angles: 0 = right (East), increasing clockwise; so 225..315 spans
    # the top quadrant. Visual extent: cy-11 (top of outer arc) to cy+1 (bottom of dot).
    Lcd.fillArc(cx, cy, 11, 9, 225, 315, color)
    Lcd.fillArc(cx, cy, 7, 5, 225, 315, color)
    Lcd.fillCircle(cx, cy - 1, 2, color)


def _draw_battery(x, y, level, charging):
    # Body: 22w x 10h rounded. Tip: 3w x 4h sticking out to the right.
    body_w, body_h = 22, 10
    Lcd.drawRoundRect(x, y, body_w, body_h, 2, _FG)
    Lcd.fillRect(x + body_w, y + (body_h // 2) - 2, 3, 4, _FG)
    # clear inner area first
    Lcd.fillRect(x + 2, y + 2, body_w - 4, body_h - 4, _BG)
    if charging:
        fill_color = _WAIT
    elif level >= 50:
        fill_color = _OK
    elif level >= 20:
        fill_color = _WAIT
    else:
        fill_color = _ERR
    inner_max = body_w - 4
    fill_w = max(1, (inner_max * level) // 100) if level > 0 else 0
    if fill_w > 0:
        Lcd.fillRect(x + 2, y + 2, fill_w, body_h - 4, fill_color)
    if charging:
        # Tiny lightning bolt = two filled triangles
        bx = x + body_w // 2
        by = y + 1
        Lcd.fillTriangle(bx + 1, by, bx - 2, by + 5, bx + 1, by + 5, _BG)
        Lcd.fillTriangle(bx - 1, by + 3, bx + 2, by + 3, bx - 1, by + 8, _BG)


def _read_battery():
    """Returns (smoothed_percent, is_charging). Both signals are EMA-smoothed
    voltage with hysteresis applied for charging state, since the Cardputer-Adv
    has no PMIC IC to query directly."""
    global _battery_ema, _battery_v_ema, _charging_state
    try:
        raw_pct = M5.Power.getBatteryLevel()
        if raw_pct is None:
            raw_pct = 0
        try:
            raw_mv = M5.Power.getBatteryVoltage() or 0
        except Exception:
            raw_mv = 0

        if _battery_ema is None:
            _battery_ema = float(raw_pct)
        else:
            _battery_ema = _BATTERY_EMA_A * raw_pct + (1 - _BATTERY_EMA_A) * _battery_ema
        if _battery_v_ema is None:
            _battery_v_ema = float(raw_mv)
        else:
            _battery_v_ema = _BATTERY_EMA_A * raw_mv + (1 - _BATTERY_EMA_A) * _battery_v_ema

        # Hysteresis — flip state only on a clear cross of the far threshold.
        if _charging_state:
            if _battery_v_ema < _BATT_LOW_MV:
                _charging_state = False
        else:
            if _battery_v_ema > _BATT_HIGH_MV:
                _charging_state = True

        return int(round(_battery_ema)), _charging_state
    except Exception:
        return 0, False


def _draw_status_bar(force=False):
    """Repaints WiFi + clock + battery indicators. Cached: only repaints when
    any displayed value changes, unless force=True."""
    global _last_status_drawn
    state = _wifi_state()
    level, charging = _read_battery()
    clock = _bj_clock_text()
    key = (state, level, charging, clock)
    if not force and key == _last_status_drawn:
        return
    _last_status_drawn = key

    Lcd.fillRect(0, 0, Lcd.width(), _STATUS_H, _BG)

    # All elements vertically centered around y = _STATUS_H // 2 = 11
    bar_mid = _STATUS_H // 2

    # WiFi icon (left): icon spans cy-11..cy+1, so cy=bar_mid+5 ≈ 16 centers it.
    wifi_color = _OK if state == "ok" else (_WAIT if state == "wait" else _ERR)
    _draw_wifi_icon(14, bar_mid + 5, wifi_color)

    # Center: clock (Beijing). Empty string before NTP syncs.
    Lcd.setFont(_SMALL)
    if clock:
        clock_w = Lcd.textWidth(clock, _SMALL)
        clock_h = Lcd.fontHeight(_SMALL)
        Lcd.setTextColor(_FG, _BG)
        Lcd.setCursor((Lcd.width() - clock_w) // 2,
                      (_STATUS_H - clock_h) // 2)
        Lcd.print(clock)

    # Right: percent text + battery icon, both vertically centered.
    bat_h = 10
    bat_y = (_STATUS_H - bat_h) // 2   # 6
    bat_x = Lcd.width() - 28
    pct = "{}%".format(level)
    pct_h = Lcd.fontHeight(_SMALL)
    pct_w = Lcd.textWidth(pct, _SMALL)
    Lcd.setTextColor(_FG, _BG)
    Lcd.setCursor(bat_x - pct_w - 4, (_STATUS_H - pct_h) // 2)
    Lcd.print(pct)
    _draw_battery(bat_x, bat_y, level, charging)

    Lcd.drawLine(0, _STATUS_H, Lcd.width(), _STATUS_H, _BAR_LINE)


# --- Menu drawing ---------------------------------------------------------

def _max_visible(in_category):
    return _MAX_VISIBLE_CAT if in_category else _MAX_VISIBLE


def _list_top(in_category):
    return _LIST_TOP + (_BREADCRUMB_H if in_category else 0)


def _adjust_scroll(idx, scroll, n, in_category):
    """Sliding-window scroll: keep `idx` visible, return adjusted offset."""
    visible = min(n, _max_visible(in_category))
    if idx < scroll:
        return idx
    if idx >= scroll + visible:
        return idx - visible + 1
    return scroll


def _move_idx(idx, delta, scroll, n, in_category):
    """Move cursor with wraparound; jump scroll to extreme on wrap."""
    if n == 0:
        return 0, 0
    mv = _max_visible(in_category)
    new_idx = (idx + delta) % n
    if delta > 0 and new_idx == 0:
        return 0, 0  # wrapped down→top
    if delta < 0 and new_idx == n - 1:
        return new_idx, max(0, n - mv)  # wrapped up→bottom
    return new_idx, _adjust_scroll(new_idx, scroll, n, in_category)


def _draw_scrollbar(scroll, visible, total, list_top):
    """Right-edge thumb track, only when content overflows."""
    track_x = Lcd.width() - _SCROLLBAR_W
    track_y = list_top - 2
    track_h = _LINE_H * visible
    Lcd.fillRect(track_x, track_y, _SCROLLBAR_W, track_h, _BAR_LINE)
    thumb_h = max(6, (track_h * visible) // total)
    max_thumb_y = track_h - thumb_h
    if total > visible:
        thumb_y = track_y + (max_thumb_y * scroll) // (total - visible)
    else:
        thumb_y = track_y
    Lcd.fillRect(track_x, thumb_y, _SCROLLBAR_W, thumb_h, _DIM)


def _draw_breadcrumb(category):
    y = _STATUS_H + 1
    Lcd.fillRect(0, y, Lcd.width(), _BREADCRUMB_H, _BG)
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(_WAIT, _BG)
    Lcd.setCursor(_PAD, y + 2)
    Lcd.print("/ " + category)
    hint = "ESC=back"
    hw = Lcd.textWidth(hint, _SMALL)
    Lcd.setTextColor(_DIM, _BG)
    Lcd.setCursor(Lcd.width() - hw - _PAD, y + 2)
    Lcd.print(hint)


def _draw_menu(items, idx, scroll, category=None):
    Lcd.clear(_BG)
    in_cat = category is not None
    list_top = _list_top(in_cat)
    mv = _max_visible(in_cat)

    if in_cat:
        _draw_breadcrumb(category)

    if not items:
        Lcd.setFont(_FONT)
        Lcd.setTextColor(_FG, _BG)
        Lcd.setCursor(_PAD, list_top)
        Lcd.print("(empty)")
    else:
        n = len(items)
        visible = min(n, mv)
        right_pad = _SCROLLBAR_W + 2 if n > visible else 0
        row_w = Lcd.width() - right_pad
        icon_y_offset = (_LINE_H - _ICON_SIZE) // 2 - 1

        for slot in range(visible):
            i = scroll + slot
            item = items[i]
            y = list_top + slot * _LINE_H
            selected = (i == idx)
            if selected:
                Lcd.fillRect(0, y - 2, row_w, _LINE_H, _SEL_BG)
                text_color = _SEL_FG
                bg = _SEL_BG
                on_dark = False
            else:
                text_color = _FG
                bg = _BG
                on_dark = True
            _draw_item_icon(item, _ICON_X, y - 2 + icon_y_offset,
                            _ICON_SIZE, on_dark)
            Lcd.setFont(_FONT)
            Lcd.setTextColor(text_color, bg)
            Lcd.setCursor(_TEXT_X, y)
            label = item["name"]
            if item["kind"] == "category":
                label = label + "  ({})".format(item["count"])
            Lcd.print(label)

        if n > visible:
            _draw_scrollbar(scroll, visible, n, list_top)
    _draw_status_bar(force=True)


def _drain_kb():
    for _ in range(32):
        if _kb.get_key() is None:
            return


def _read_nav_key():
    k = _kb.get_key()
    if k is None:
        return None
    # ; / . are the cardputer's arrow-key positions but require Fn to emit
    # KEYCODE_UP/DOWN — accept the bare characters so single-key nav works.
    if k == KeyCode.KEYCODE_UP or k == ord("w") or k == ord(";"):
        return "up"
    if k == KeyCode.KEYCODE_DOWN or k == ord("s") or k == ord("."):
        return "down"
    if k == KeyCode.KEYCODE_ENTER:
        return "enter"
    if k == KeyCode.KEYCODE_ESC or k == KeyCode.KEYCODE_BACKSPACE:
        return "back"
    return None


def _show_error(name, exc):
    Lcd.clear(_BG)
    Lcd.setFont(_FONT)
    Lcd.setTextColor(_ERR, _BG)
    Lcd.setCursor(_PAD, _PAD)
    Lcd.print("Crash: " + name)
    Lcd.setTextColor(_FG, _BG)
    Lcd.setCursor(_PAD, _PAD + _LINE_H)
    Lcd.print(repr(exc)[:80])
    Lcd.setCursor(_PAD, Lcd.height() - 18)
    Lcd.setTextColor(_DIM, _BG)
    Lcd.print("ESC = back")
    while True:
        if _kb.get_key() == KeyCode.KEYCODE_ESC:
            return
        time.sleep_ms(30)


def _run_app(path):
    """`path` is the dotted suffix under apps/, e.g. "snake" or "games.snake"."""
    global _kb
    mod_path = "apps." + path + ".app"
    if mod_path in sys.modules:
        del sys.modules[mod_path]
    try:
        mod = __import__(mod_path, None, None, ["run"])
        mod.run()
    except Exception as e:
        _show_error(path, e)
    # Apps that hammer I2C(0) (the keyboard's bus) sometimes wedge the
    # firmware's IRQ handler — kb.get_key() returns None forever even with
    # keys queued in the TCA8418's FIFO. Re-creating MatrixKeyboard re-runs
    # its init and re-attaches the GPIO IRQ, recovering the launcher's
    # navigation keys.
    try:
        _kb = MatrixKeyboard()
    except Exception:
        pass
    # Apps may have re-enabled the speaker DAC / NS4150B PA (or BLE / USB
    # init paths sometimes do too). Silence the codec on every return so
    # we never sit in the launcher with an audible idle hiss.
    _silence_codec()


def _silence_codec():
    """Cleanly power down ES8311 after something enabled it (boot beep,
    morse audio TX, etc).

    M5Unified's `_speaker_enabled_cb_cardputer_adv` has an EMPTY disable
    callback on this board, so `Speaker.end()` doesn't actually shut the
    DAC down — and NS4150B has no software-controllable SD pin we know
    of, so it amplifies whatever the codec output settles on.

    Sequence values come from Espressif's ESP-ADF ES8311 driver power-down
    path (NOT my own guesses — the previous version wrote 0x12=0xFF which
    leaves bias generators at MAX, making the output stage drift and the
    amp hiss like an antenna during WiFi scans).
    """
    try:
        M5.Speaker.stop()
    except Exception:
        pass
    try:
        M5.Speaker.end()
    except Exception:
        pass
    try:
        from machine import I2C, Pin
        i2c = I2C(1, sda=Pin(8), scl=Pin(9), freq=400000)
        for reg, val in (
            (0x32, 0x00),  # DAC_VOLUME = 0 (mute)
            (0x12, 0x02),  # SYSTEM12 = PDN_DAC (just bit 1)
            (0x13, 0x10),  # SYSTEM13 = ESP-ADF default (low-power output)
            (0x0E, 0xFF),  # SYSTEM0E = close ADC modulator
            (0x14, 0x00),  # ADC14 = disable PGA
            (0x0D, 0xFA),  # SYSTEM0D = power down analog
            (0x37, 0x08),  # DAC37 = bypass equalizer (idle state)
            (0x00, 0x00),  # CSM off (master power down)
        ):
            i2c.writeto_mem(0x18, reg, bytes([val]))
    except Exception:
        pass


def run():
    global _kb, _last_status_drawn
    M5.begin()
    _kb = MatrixKeyboard()
    # No codec touch at boot: M5.begin() doesn't play the cardputeradv
    # framework's startup beep (that's only in the framework's own start
    # path which we bypass), so the codec is at factory power-on default
    # = quiet. Writing it here would only put it into a non-default state.
    _wifi_init()

    category = None
    items = _list_items(None)
    idx = 0
    scroll = 0
    # When descending into a category we stash the root cursor so ESC restores it.
    saved_root = (0, 0)
    _draw_menu(items, idx, scroll, category)

    last_status_check = 0
    while True:
        now = time.ticks_ms()
        if time.ticks_diff(now, last_status_check) >= _STATUS_REFRESH_MS:
            _wifi_retry_if_needed(_wifi_state())
            _maybe_ntp_sync()
            _draw_status_bar()
            last_status_check = now

        key = _read_nav_key()
        if key is None:
            time.sleep_ms(30)
            continue
        in_cat = category is not None
        if not items:
            # Empty view (rare). Enter rescans, ESC pops out of an empty cat.
            if key == "enter":
                items = _list_items(category)
                _draw_menu(items, idx, scroll, category)
            elif key == "back" and in_cat:
                category = None
                items = _list_items(None)
                idx, scroll = saved_root
                _draw_menu(items, idx, scroll, category)
            continue
        if key == "up":
            idx, scroll = _move_idx(idx, -1, scroll, len(items), in_cat)
            _draw_menu(items, idx, scroll, category)
        elif key == "down":
            idx, scroll = _move_idx(idx, 1, scroll, len(items), in_cat)
            _draw_menu(items, idx, scroll, category)
        elif key == "back":
            if in_cat:
                category = None
                items = _list_items(None)
                idx, scroll = saved_root
                _draw_menu(items, idx, scroll, category)
            # else: at root — ESC is a no-op (launcher is the shell)
        elif key == "enter":
            item = items[idx]
            if item["kind"] == "category":
                saved_root = (idx, scroll)
                category = item["name"]
                items = _list_items(category)
                idx, scroll = 0, 0
                _draw_menu(items, idx, scroll, category)
            else:
                _run_app(item["path"])
                _drain_kb()
                items = _list_items(category)
                if not items and category is not None:
                    # Category went empty (apps removed at runtime?) — pop up.
                    category = None
                    items = _list_items(None)
                    idx, scroll = saved_root
                else:
                    if idx >= len(items):
                        idx = max(0, len(items) - 1)
                    scroll = _adjust_scroll(idx, scroll, len(items), in_cat)
                _last_status_drawn = None
                _draw_menu(items, idx, scroll, category)
