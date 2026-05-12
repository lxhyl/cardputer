import json
import time

import M5
import network
from M5 import Lcd
from hardware.matrix_keyboard import MatrixKeyboard
from startup.cardputeradv.framework import KeyCode

_WIFI_CFG = "/flash/wifi.json"


def _load_known():
    """Return list of {'ssid','password'} dicts. Tolerates the legacy
    single-network format `{ssid, password}`."""
    try:
        with open(_WIFI_CFG) as f:
            cfg = json.load(f)
    except Exception:
        return []
    if isinstance(cfg, dict) and "networks" in cfg:
        return [n for n in cfg["networks"]
                if isinstance(n, dict) and n.get("ssid")]
    if isinstance(cfg, dict) and cfg.get("ssid"):
        return [{"ssid": cfg["ssid"], "password": cfg.get("password", "")}]
    return []

_BG = 0x000000
_FG = 0xFFFFFF
_DIM = 0x666666
_OK = 0x00DD66
_WAIT = 0xFFD040
_ERR = 0xFF4444
_SEL_BG = 0xFFFFFF
_SEL_FG = 0x000000
_HDR_BG = 0x232332
_HDR_FG = 0xFFD040
_BAR_OFF = 0x303040

_FONT = M5.Lcd.FONTS.DejaVu18
_SMALL = M5.Lcd.FONTS.DejaVu12

_LINE_H = 22
_PAD = 6
_HEADER_H = 22
_LIST_TOP = 28
_HINT_H = 16
_VISIBLE_ROWS = 4   # 28 + 4*22 = 116, leaves 19px for hint


def _truncate(s, n):
    return s if len(s) <= n else s[:n - 1] + "."


def _draw_header(text, color=_HDR_FG):
    Lcd.fillRect(0, 0, Lcd.width(), _HEADER_H, _HDR_BG)
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(color, _HDR_BG)
    Lcd.setCursor(_PAD, 6)
    Lcd.print(text)


def _draw_hint(text):
    y = Lcd.height() - _HINT_H
    Lcd.fillRect(0, y, Lcd.width(), _HINT_H, _BG)
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(_DIM, _BG)
    Lcd.setCursor(_PAD, y + 2)
    Lcd.print(text)


def _draw_signal(x, y, level, color):
    bw = 3
    for i in range(4):
        bh = 2 + i * 2
        bx = x + i * (bw + 1)
        by = y + (10 - bh)
        Lcd.fillRect(bx, by, bw, bh, color if i < level else _BAR_OFF)


def _draw_lock(x, y, color):
    # 8w x 11h padlock: shackle (arc) + body (rect)
    Lcd.drawArc(x + 4, y + 4, 4, 3, 180, 360, color)
    Lcd.fillRect(x, y + 4, 8, 7, color)


def _bars(rssi):
    if rssi >= -55: return 4
    if rssi >= -65: return 3
    if rssi >= -75: return 2
    if rssi >= -85: return 1
    return 0


def _dedupe_scan(raw):
    seen = {}
    for net in raw:
        ssid_b, _bssid, _chan, rssi, sec, _hid = net
        try:
            ssid = ssid_b.decode("utf-8")
        except UnicodeError:
            continue
        if not ssid:
            continue
        cur = seen.get(ssid)
        if cur is None or rssi > cur[1]:
            seen[ssid] = (ssid, rssi, sec != 0)
    out = list(seen.values())
    out.sort(key=lambda r: -r[1])
    return out


def _scan(wlan):
    """Try a non-disruptive scan first (keeps the existing connection
    alive). Only fall back to the disconnect+radio-cycle path if that
    returns nothing — historically scan() on a busy reconnecting STA
    can yield an empty list."""
    try:
        raw = wlan.scan()
        out = _dedupe_scan(raw)
        if out:
            return out
    except OSError:
        pass

    # Fallback: force a clean scan window.
    try:
        wlan.disconnect()
    except OSError:
        pass
    try:
        wlan.active(False)
        time.sleep_ms(120)
        wlan.active(True)
        time.sleep_ms(120)
    except OSError:
        pass
    try:
        return _dedupe_scan(wlan.scan())
    except OSError:
        return []


def _draw_list(items, idx, scroll, known, current):
    Lcd.fillRect(0, _HEADER_H + 1, Lcd.width(),
                 Lcd.height() - _HEADER_H - 1 - _HINT_H, _BG)
    if not items:
        Lcd.setFont(_FONT)
        Lcd.setTextColor(_DIM, _BG)
        Lcd.setCursor(_PAD, _LIST_TOP)
        Lcd.print("(no networks)")
        return

    Lcd.setFont(_FONT)
    visible = items[scroll:scroll + _VISIBLE_ROWS]
    for i, (ssid, _rssi, secured) in enumerate(visible):
        actual = scroll + i
        rssi_val = items[actual][1]
        y = _LIST_TOP + i * _LINE_H
        is_sel = actual == idx
        if is_sel:
            Lcd.fillRect(0, y - 2, Lcd.width(), _LINE_H, _SEL_BG)
            txt_color = _SEL_FG
            bg = _SEL_BG
        else:
            txt_color = _FG
            bg = _BG
        _draw_signal(_PAD, y + 4, _bars(rssi_val), txt_color)
        Lcd.setTextColor(txt_color, bg)
        Lcd.setCursor(_PAD + 22, y)
        is_known = ssid in known
        is_cur = ssid == current
        # Prefix: ✓ for the active connection, * for any other saved net.
        prefix = ">" if is_cur else ("*" if is_known else "")
        budget = 16 - len(prefix)
        label = prefix + _truncate(ssid, budget)
        Lcd.print(label)
        if secured:
            _draw_lock(Lcd.width() - 14, y + 4, txt_color)


def _current_ssid(wlan):
    """SSID currently associated with, or empty if not connected."""
    if not wlan.isconnected():
        return ""
    try:
        return wlan.config("essid") or ""
    except Exception:
        return ""


def _draw_top_status(wlan):
    """Header replacement: shows the active connection — `> ssid` if joined,
    or 'WiFi · not connected' otherwise. The list below uses `>` to mark the
    same SSID, so users can spot it visually."""
    Lcd.fillRect(0, 0, Lcd.width(), _HEADER_H, _HDR_BG)
    Lcd.setFont(_SMALL)
    cur = _current_ssid(wlan)
    if cur:
        ip = ""
        try:
            ip = wlan.ifconfig()[0]
        except Exception:
            pass
        # SSID on the left, IP right-aligned.
        Lcd.setTextColor(_OK, _HDR_BG)
        Lcd.setCursor(_PAD, 6)
        max_w = Lcd.width() - _PAD * 2 - Lcd.textWidth(ip, _SMALL) - 8
        s = "> " + cur
        while s and Lcd.textWidth(s, _SMALL) > max_w:
            s = s[:-1]
        Lcd.print(s)
        if ip:
            Lcd.setTextColor(_DIM, _HDR_BG)
            Lcd.setCursor(Lcd.width() - _PAD - Lcd.textWidth(ip, _SMALL), 6)
            Lcd.print(ip)
    else:
        Lcd.setTextColor(_DIM, _HDR_BG)
        Lcd.setCursor(_PAD, 6)
        Lcd.print("WiFi  not connected")


def _scan_screen(kb, wlan):
    """Returns (ssid, secured) or None if cancelled."""
    known = {n["ssid"]: n.get("password", "") for n in _load_known()}

    def rescan():
        # Cache the active SSID before scanning — `_scan()` may have to
        # cycle the radio (which drops us), and we want to silently
        # reconnect afterwards so the header keeps showing reality.
        was = _current_ssid(wlan)
        Lcd.clear(_BG)
        _draw_header("Scanning...", color=_WAIT)
        _draw_hint("ESC=cancel")
        Lcd.setFont(_FONT)
        Lcd.setTextColor(_DIM, _BG)
        Lcd.setCursor(_PAD, _LIST_TOP)
        Lcd.print("Please wait...")
        try:
            out = _scan(wlan)
        except Exception:
            out = []
        if was and not wlan.isconnected():
            pwd = next((n.get("password", "") for n in _load_known()
                        if n.get("ssid") == was), "")
            try:
                wlan.connect(was, pwd)
            except OSError:
                pass
        return out

    items = rescan()
    idx = 0
    scroll = 0
    cur = _current_ssid(wlan)
    Lcd.clear(_BG)
    _draw_top_status(wlan)
    _draw_hint("Enter=join D=forget R=rescan ESC")
    _draw_list(items, idx, scroll, known, cur)

    while True:
        k = kb.get_key()
        if k is None:
            time.sleep_ms(30)
            continue
        if k == KeyCode.KEYCODE_ESC:
            return None
        if k == KeyCode.KEYCODE_UP or k == ord("w"):
            if items:
                idx = (idx - 1) % len(items)
                if idx < scroll:
                    scroll = idx
                if idx >= scroll + _VISIBLE_ROWS:
                    scroll = idx - _VISIBLE_ROWS + 1
                _draw_list(items, idx, scroll, known, cur)
        elif k == KeyCode.KEYCODE_DOWN or k == ord("s"):
            if items:
                idx = (idx + 1) % len(items)
                if idx < scroll:
                    scroll = idx
                if idx >= scroll + _VISIBLE_ROWS:
                    scroll = idx - _VISIBLE_ROWS + 1
                _draw_list(items, idx, scroll, known, cur)
        elif k == KeyCode.KEYCODE_ENTER:
            if items:
                ssid, _r, secured = items[idx]
                return ssid, secured
        elif k == ord("d") or k == ord("D"):
            # Forget a saved network so the user can re-enter the password
            # (e.g. AP password changed).
            if items:
                ssid = items[idx][0]
                if ssid in known:
                    _forget(ssid)
                    known = {n["ssid"]: n.get("password", "")
                             for n in _load_known()}
                    _draw_list(items, idx, scroll, known, cur)
        elif k == ord("r") or k == ord("R"):
            known = {n["ssid"]: n.get("password", "")
                     for n in _load_known()}
            items = rescan()
            idx = 0
            scroll = 0
            cur = _current_ssid(wlan)
            Lcd.clear(_BG)
            _draw_top_status(wlan)
            _draw_hint("Enter=join D=forget R=rescan ESC")
            _draw_list(items, idx, scroll, known, cur)


def _forget(ssid):
    nets = [n for n in _load_known() if n.get("ssid") != ssid]
    try:
        with open(_WIFI_CFG, "w") as f:
            json.dump({"networks": nets}, f)
    except Exception:
        pass


def _password_screen(kb, ssid):
    """Returns password string, or None if cancelled."""
    buf = ""
    Lcd.clear(_BG)
    _draw_header("Password")
    _draw_hint("Enter=join  Bksp=del  ESC=cancel")

    Lcd.setFont(_SMALL)
    Lcd.setTextColor(_DIM, _BG)
    Lcd.setCursor(_PAD, 28)
    Lcd.print("Network:")

    Lcd.setFont(_FONT)
    Lcd.setTextColor(_FG, _BG)
    Lcd.setCursor(_PAD, 42)
    Lcd.print(_truncate(ssid, 18))

    def redraw_buf():
        Lcd.fillRect(0, 76, Lcd.width(), 30, _BG)
        Lcd.setFont(_FONT)
        Lcd.setTextColor(_OK, _BG)
        Lcd.setCursor(_PAD, 80)
        Lcd.print(_truncate(buf if buf else "_", 18))

    redraw_buf()

    while True:
        k = kb.get_key()
        if k is None:
            time.sleep_ms(30)
            continue
        if k == KeyCode.KEYCODE_ESC:
            return None
        if k == KeyCode.KEYCODE_ENTER:
            return buf
        if k == KeyCode.KEYCODE_BACKSPACE or k == KeyCode.KEYCODE_DEL:
            if buf:
                buf = buf[:-1]
                redraw_buf()
        elif isinstance(k, int) and 32 <= k <= 126:
            buf += chr(k)
            redraw_buf()


def _connect_attempt(wlan, ssid, pwd, timeout_ms):
    """One connect attempt + status poll. Returns None on success or an
    error string. Caller decides whether to retry with a radio cycle."""
    try:
        wlan.disconnect()
    except OSError:
        pass
    try:
        wlan.connect(ssid, pwd)
    except OSError as e:
        return repr(e)[:30]
    deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        if wlan.isconnected():
            return None
        s = wlan.status()
        if s == network.STAT_WRONG_PASSWORD:
            return "Wrong password"
        if s == network.STAT_NO_AP_FOUND:
            return "AP not found"
        if s == network.STAT_CONNECT_FAIL:
            return "Connect failed"
        time.sleep_ms(200)
    return "Timeout"


def _connect(wlan, ssid, pwd):
    """First-time connect with auto-retry on stale-radio failures.

    The launcher runs a background reconnect loop that may leave the
    radio in a half-joined state (status == CONNECTING but no progress).
    A fresh `connect()` from this state returns NO_AP_FOUND / CONNECT_FAIL
    / Timeout even though the AP is in range. Cycling the radio fully
    resets that state. We do it transparently on first failure rather
    than making the user press R, click again, etc."""
    err = _connect_attempt(wlan, ssid, pwd, 12_000)
    if err is None or err == "Wrong password":
        # Definitive outcomes — don't retry. Wrong password means the
        # creds are bad, not the radio.
        return err

    # Stale-radio symptoms — cycle the chip and try once more.
    try:
        wlan.disconnect()
    except OSError:
        pass
    try:
        wlan.active(False)
        time.sleep_ms(150)
        wlan.active(True)
        time.sleep_ms(150)
    except OSError:
        pass
    return _connect_attempt(wlan, ssid, pwd, 15_000)


def _save_creds(ssid, pwd):
    """Append to the saved-networks list. Most-recently-used wins on
    duplicates so that the boot path picks the AP you last successfully
    joined."""
    nets = _load_known()
    nets = [n for n in nets if n.get("ssid") != ssid]
    nets.insert(0, {"ssid": ssid, "password": pwd})
    try:
        with open(_WIFI_CFG, "w") as f:
            json.dump({"networks": nets}, f)
        return True
    except Exception:
        return False


def _show_centered(text, color):
    Lcd.fillRect(0, Lcd.height() // 2 - 16, Lcd.width(), 32, _BG)
    Lcd.setFont(_FONT)
    Lcd.setTextColor(color, _BG)
    tw = Lcd.textWidth(text, _FONT)
    Lcd.setCursor(max(0, (Lcd.width() - tw) // 2), Lcd.height() // 2 - 10)
    Lcd.print(text)


def run():
    Lcd.clear(_BG)
    kb = MatrixKeyboard()
    wlan = network.WLAN(network.STA_IF)
    if not wlan.active():
        wlan.active(True)

    while True:
        choice = _scan_screen(kb, wlan)
        if choice is None:
            return
        ssid, secured = choice

        # Saved SSID? reuse the stored password — that's the whole point of
        # this app remembering networks.
        known = {n["ssid"]: n.get("password", "") for n in _load_known()}
        if ssid in known:
            pwd = known[ssid]
        elif secured:
            pwd = _password_screen(kb, ssid)
            if pwd is None:
                continue
        else:
            pwd = ""

        Lcd.clear(_BG)
        _draw_header("Connecting...", color=_WAIT)
        _show_centered(_truncate(ssid, 18), _FG)
        _draw_hint("Please wait...")

        err = _connect(wlan, ssid, pwd)
        if err is None:
            _save_creds(ssid, pwd)
            Lcd.clear(_BG)
            _draw_header("Connected", color=_OK)
            ip = wlan.ifconfig()[0]
            _show_centered(ip, _OK)
            _draw_hint("ESC = back to launcher")
            while True:
                if kb.get_key() == KeyCode.KEYCODE_ESC:
                    return
                time.sleep_ms(30)
        else:
            # Stale stored password is the most painful failure mode — the
            # user picks a saved AP and silently gets a "wrong password"
            # loop. Drop the entry so the next attempt prompts again.
            if err == "Wrong password" and ssid in known:
                _forget(ssid)
            Lcd.clear(_BG)
            _draw_header("Failed", color=_ERR)
            _show_centered(err, _ERR)
            _draw_hint("Any key = back to list")
            # drain then wait for keypress
            time.sleep_ms(200)
            while True:
                if kb.get_key() is not None:
                    break
                time.sleep_ms(30)
