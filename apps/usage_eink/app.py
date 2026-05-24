"""Push Claude usage % to a flashed Xiaomi Mijia Thermometer 3 (MJWSD05MMC).

Requires that the thermometer has been re-flashed with pvvx's
ATC_MiThermometer custom firmware (https://pvvx.github.io/ATC_MiThermometer)
and configured with screen_type = "External number & symbols". See the
project README for the one-time flashing procedure.

Wire protocol — write to GATT char 0x1F1F (service 0x1F10):
  byte 0:        0x22                   CMD_ID_EXTDATA opcode (pvvx firmware)
  bytes 1..4:    s32 little-endian      number * 100  (e.g. 31% -> 3100)
  bytes 5..6:    u16 little-endian      vtime_sec — display this value for
                                         N seconds before reverting to the
                                         default screen. 0xFFFF = forever.
  byte 7 (bitfield):
     bits 0..2:  smiley   0=off, 1..7 see firmware; 1=happy, 6=sad, 4=neutral
     bit 3:      battery icon
     bits 4..7:  unit symbol — 0=none, 3=°C, 7=°F, 8="%"

Default no-pin firmware = no pairing required. We connect, write, and
disconnect on each refresh so the e-ink is free to refresh between updates.

Device-local config at /flash/usage_eink.json:
  {"thermometer_mac": "aa:bb:cc:dd:ee:ff",
   "endpoint": "http://192.168.x.x:8765/usage",
   "auth": ""}
"""

import bluetooth
import gc
import json
import struct
import time

import M5
from M5 import Lcd
from hardware.matrix_keyboard import MatrixKeyboard
from startup.cardputeradv.framework import KeyCode

try:
    import requests
except ImportError:
    import urequests as requests

_CFG_PATH = "/flash/usage_eink.json"

# Refresh cadence — keep e-ink updates infrequent (it has limited write
# cycles and the refresh is visible). Every 2 minutes is plenty.
_PUSH_INTERVAL_MS = 120_000

# pvvx custom service / characteristic
_PVVX_SVC = bluetooth.UUID(0x1F10)
_PVVX_CHR = bluetooth.UUID(0x1F1F)

# pvvx external-display command
_CMD_EXTDATA = 0x22

# Bit-packing for byte 7 (smiley + battery + unit)
def _pack_byte7(smiley, battery, unit):
    return (smiley & 0x07) | ((1 if battery else 0) << 3) | ((unit & 0x0F) << 4)

_UNIT_PCT = 8

# IRQ event ids (from MicroPython bluetooth docs)
_IRQ_PERIPHERAL_CONNECT = 7
_IRQ_PERIPHERAL_DISCONNECT = 8
_IRQ_GATTC_SERVICE_RESULT = 9
_IRQ_GATTC_SERVICE_DONE = 10
_IRQ_GATTC_CHARACTERISTIC_RESULT = 11
_IRQ_GATTC_CHARACTERISTIC_DONE = 12
_IRQ_GATTC_WRITE_DONE = 17

_W = 240
_H = 135
_BG = 0x000000
_FG = 0xFFFFFF
_DIM = 0x888888
_GREEN = 0x7BB662
_AMBER = 0xE5A642
_RED = 0xE5483B
_ACCENT = 0x40C8FF
_ERR = 0xFF5544
_SMALL = M5.Lcd.FONTS.DejaVu12
_MID = M5.Lcd.FONTS.DejaVu18
_BIG = M5.Lcd.FONTS.DejaVu24


# ---- Helpers ------------------------------------------------------------

def _load_cfg():
    try:
        with open(_CFG_PATH) as f:
            cfg = json.load(f)
    except Exception:
        return None
    if not isinstance(cfg, dict):
        return None
    mac = cfg.get("thermometer_mac") or ""
    ep = cfg.get("endpoint") or ""
    if not mac or not ep:
        return None
    return cfg


def _parse_mac(mac_str):
    """Convert 'aa:bb:cc:dd:ee:ff' to 6-byte big-endian bytes."""
    parts = mac_str.replace("-", ":").split(":")
    if len(parts) != 6:
        return None
    try:
        return bytes(int(p, 16) for p in parts)
    except ValueError:
        return None


def _smiley_for(pct):
    # 1=happy, 4=neutral parens, 6=sad
    if pct >= 80:
        return 6
    if pct >= 50:
        return 4
    return 1


def _color_for(pct):
    if pct >= 80:
        return _RED
    if pct >= 50:
        return _AMBER
    return _GREEN


def _build_payload(pct):
    """Pack the 8-byte EXTDATA write."""
    number = int(round(pct * 100))    # display X.YY → write X*100+YY
    vtime = 0xFFFF                    # display until next push
    smiley = _smiley_for(pct)
    b7 = _pack_byte7(smiley, False, _UNIT_PCT)
    # struct.pack: <BiHB = 1 + 4 + 2 + 1 = 8 bytes
    return struct.pack("<BiHB", _CMD_EXTDATA, number, vtime, b7)


def _fetch_usage(cfg):
    url = cfg["endpoint"]
    h = {}
    auth = cfg.get("auth")
    if auth:
        h["Authorization"] = "Bearer " + auth
    r = None
    try:
        gc.collect()
        r = requests.get(url, headers=h, timeout=8)
        if r.status_code != 200:
            return None, "HTTP %d" % r.status_code
        data = r.json()
        return data, None
    except Exception as e:
        return None, repr(e)[:32]
    finally:
        if r is not None:
            try:
                r.close()
            except Exception:
                pass


# ---- BLE writer (one-shot per push) ------------------------------------

class EinkWriter:
    """Connect → discover → write → disconnect, blocking up to N seconds.

    Async-style state machine driven by IRQs but exposed as a synchronous
    `write_value(addr_bytes, data)` -> ok:bool.
    """

    def __init__(self):
        self._ble = bluetooth.BLE()
        try:
            self._ble.active(False)
        except Exception:
            pass
        self._ble.active(True)
        self._ble.irq(self._on_irq)
        self._reset()

    def _reset(self):
        self._conn_handle = None
        self._svc_start = None
        self._svc_end = None
        self._chr_handle = None
        self._stage = "idle"      # idle | connecting | discovering_svc |
                                   # discovering_chr | writing | done | failed
        self._error = None

    def _on_irq(self, event, data):
        if event == _IRQ_PERIPHERAL_CONNECT:
            conn_handle, _addr_type, _addr = data
            self._conn_handle = conn_handle
            self._stage = "discovering_svc"
            try:
                self._ble.gattc_discover_services(conn_handle, _PVVX_SVC)
            except Exception as e:
                self._error = "disc_svc " + repr(e)[:20]
                self._stage = "failed"

        elif event == _IRQ_PERIPHERAL_DISCONNECT:
            if self._stage not in ("done", "failed"):
                self._error = "disc"
                self._stage = "failed"
            self._conn_handle = None

        elif event == _IRQ_GATTC_SERVICE_RESULT:
            _conn, start_handle, end_handle, _uuid = data
            self._svc_start = start_handle
            self._svc_end = end_handle

        elif event == _IRQ_GATTC_SERVICE_DONE:
            if self._svc_start is None:
                self._error = "no svc"
                self._stage = "failed"
                return
            self._stage = "discovering_chr"
            try:
                self._ble.gattc_discover_characteristics(
                    self._conn_handle, self._svc_start, self._svc_end, _PVVX_CHR)
            except Exception as e:
                self._error = "disc_chr " + repr(e)[:20]
                self._stage = "failed"

        elif event == _IRQ_GATTC_CHARACTERISTIC_RESULT:
            _conn, _def_handle, value_handle, _props, _uuid = data
            self._chr_handle = value_handle

        elif event == _IRQ_GATTC_CHARACTERISTIC_DONE:
            if self._chr_handle is None:
                self._error = "no chr"
                self._stage = "failed"
                return
            self._stage = "writing"
            try:
                # Write Without Response = mode 0 (faster, no ack);
                # Write With Response = mode 1 (firmware acks).
                self._ble.gattc_write(
                    self._conn_handle, self._chr_handle,
                    self._pending_data, 1)
            except Exception as e:
                self._error = "write " + repr(e)[:20]
                self._stage = "failed"

        elif event == _IRQ_GATTC_WRITE_DONE:
            self._stage = "done"

    def write_value(self, addr_bytes, data, timeout_ms=10_000):
        """Returns (ok, error_str)."""
        self._reset()
        self._pending_data = data
        self._stage = "connecting"
        try:
            # addr_type: 0 = public, 1 = random. The MJWSD05MMC uses
            # public address.
            self._ble.gap_connect(0, addr_bytes, 4000)
        except Exception as e:
            return False, "connect " + repr(e)[:20]

        deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
        while self._stage not in ("done", "failed"):
            if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                self._error = "timeout @ " + self._stage
                self._stage = "failed"
                break
            time.sleep_ms(50)

        ok = self._stage == "done"
        # Always disconnect so the e-ink can refresh.
        if self._conn_handle is not None:
            try:
                self._ble.gap_disconnect(self._conn_handle)
            except Exception:
                pass
            # Wait briefly for disconnect to settle.
            t0 = time.ticks_ms()
            while (self._conn_handle is not None and
                   time.ticks_diff(time.ticks_ms(), t0) < 1500):
                time.sleep_ms(50)

        return ok, self._error

    def shutdown(self):
        try:
            self._ble.irq(None)
        except Exception:
            pass
        try:
            self._ble.active(False)
        except Exception:
            pass


# ---- UI -----------------------------------------------------------------

def _draw_screen(state, pct, err):
    Lcd.clear(_BG)
    Lcd.setFont(_MID)
    Lcd.setTextColor(_FG, _BG)
    title = "Usage → e-ink"
    tw = Lcd.textWidth(title, _MID)
    Lcd.setCursor((_W - tw) // 2, 0)
    Lcd.print(title)
    Lcd.drawLine(8, 22, _W - 8, 22, 0x202020)

    Lcd.setFont(_BIG)
    if pct is not None:
        msg = "%d%%" % pct
        col = _color_for(pct)
        Lcd.setTextColor(col, _BG)
    else:
        msg = "--%"
        Lcd.setTextColor(_DIM, _BG)
    mw = Lcd.textWidth(msg, _BIG)
    Lcd.setCursor((_W - mw) // 2, 36)
    Lcd.print(msg)

    Lcd.setFont(_SMALL)
    if state == "fetching":
        line = "fetching usage..."
        col = _DIM
    elif state == "pushing":
        line = "writing to e-ink..."
        col = _ACCENT
    elif state == "ok":
        line = "pushed → e-ink"
        col = _GREEN
    elif state == "no-config":
        line = "edit /flash/usage_eink.json"
        col = _AMBER
    elif state == "error":
        line = "error: " + (err or "?")
        col = _ERR
    else:
        line = "idle"
        col = _DIM
    lw = Lcd.textWidth(line, _SMALL)
    Lcd.setTextColor(col, _BG)
    Lcd.setCursor((_W - lw) // 2, 80)
    Lcd.print(line)

    Lcd.setFont(_SMALL)
    Lcd.setTextColor(_DIM, _BG)
    hint = "ESC quit  R refresh now"
    hw = Lcd.textWidth(hint, _SMALL)
    Lcd.setCursor((_W - hw) // 2, _H - 14)
    Lcd.print(hint)


# ---- Main loop ----------------------------------------------------------

def _push_once(cfg, writer):
    """Returns (pct, ok, err)."""
    data, err = _fetch_usage(cfg)
    if data is None:
        return None, False, err
    w = (data.get("weekly") or {})
    pct = int(w.get("pct", 0) or 0)
    payload = _build_payload(pct)
    addr = _parse_mac(cfg["thermometer_mac"])
    if addr is None:
        return pct, False, "bad MAC"
    ok, werr = writer.write_value(addr, payload)
    return pct, ok, werr


def run():
    kb = MatrixKeyboard()
    cfg = _load_cfg()
    if cfg is None:
        _draw_screen("no-config", None, None)
        while True:
            k = kb.get_key()
            if k == KeyCode.KEYCODE_ESC:
                return
            time.sleep_ms(60)

    _draw_screen("fetching", None, None)
    writer = EinkWriter()

    try:
        last_push = None
        last_pct = None
        last_err = None
        while True:
            now = time.ticks_ms()
            due = (last_push is None or
                   time.ticks_diff(now, last_push) >= _PUSH_INTERVAL_MS)
            if due:
                _draw_screen("fetching", last_pct, None)
                pct, ok, err = _push_once(cfg, writer)
                last_push = time.ticks_ms()
                if ok:
                    last_pct = pct
                    last_err = None
                    _draw_screen("ok", pct, None)
                else:
                    last_err = err
                    _draw_screen("error", pct if pct is not None else last_pct, err)

            k = kb.get_key()
            if k is None:
                time.sleep_ms(80)
                continue
            if k == KeyCode.KEYCODE_ESC:
                break
            if k == ord("r") or k == ord("R") or k == KeyCode.KEYCODE_ENTER:
                last_push = None
    finally:
        try:
            writer.shutdown()
        except Exception:
            pass
