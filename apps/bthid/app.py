"""BLE HID combo — keyboard + mouse over a single GATT service.

One BLE peripheral, one bond on the host. Tilt the Cardputer to move
the cursor, type on the keyboard to send characters, use arrow keys
for clicks. No mode switching, no macro menu — open the app and start
using it.

GATT structure (Heerkog/MicroPythonBLEHID layout):
  - HID Info, Report Map (composite — both keyboard + mouse), Control
    Point, Protocol Mode.
  - Keyboard input report (Report ID 1) — 8-byte notify, encrypted.
  - Keyboard output report (Report ID 1) — 1-byte LED state from host.
  - Mouse input report (Report ID 2) — 4-byte notify, encrypted.

Pairing: JustWorks bond (FLAG_READ_ENCRYPTED on input report forces
SMP, proactive `gap_pair()` from CONNECT IRQ avoids macOS' silent
"don't-bother-pairing" behaviour). Bond persists in
/flash/ble_bonds.json. See CLAUDE.md "BLE HID 键盘连 macOS" section
for the full why.

Controls:
  • tilt the device       → cursor moves
  • any printable key     → typed as keyboard event
  • Enter / Backspace / Space / Tab → keyboard
  • ←  → mouse left click
  • →  → mouse right click
  • ↑  → scroll up
  • ↓  → scroll down
  • Fn + Space (sends '+') → middle click  (Cardputer Tab key sends
    KEYCODE_TAB; we use that as middle click)
  • ESC → quit (BLE radio fully off on exit)

Drift is handled automatically: when the device sits still for ~1 second
the neutral pose is re-captured to whatever the resting orientation is.
No manual "calibrate" key needed.
"""

import struct
import time

import M5
import bluetooth
from bluetooth import UUID
from M5 import Lcd
from machine import I2C, Pin
from hardware.matrix_keyboard import MatrixKeyboard
from startup.cardputeradv.framework import KeyCode

# ---------------- BLE constants ----------------
F_READ                 = bluetooth.FLAG_READ
F_WRITE                = bluetooth.FLAG_WRITE
F_READ_NOTIFY          = bluetooth.FLAG_READ | bluetooth.FLAG_NOTIFY
F_READ_WRITE_NORESPONSE = (bluetooth.FLAG_READ | bluetooth.FLAG_WRITE
                           | bluetooth.FLAG_WRITE_NO_RESPONSE)
F_RW_NOTIFY_NORESPONSE = (F_READ_WRITE_NORESPONSE | bluetooth.FLAG_NOTIFY)
_FLAG_READ_ENCRYPTED   = 0x0200
_FLAG_WRITE_ENCRYPTED  = 0x1000
F_READ_NOTIFY_ENC      = F_READ_NOTIFY | _FLAG_READ_ENCRYPTED
F_RW_NOTIFY_NORESP_ENC = (F_RW_NOTIFY_NORESPONSE
                          | _FLAG_READ_ENCRYPTED | _FLAG_WRITE_ENCRYPTED)
DSC_F_READ = 0x02

_IRQ_CENTRAL_CONNECT     = 1
_IRQ_CENTRAL_DISCONNECT  = 2
_IRQ_GET_SECRET          = 29
_IRQ_SET_SECRET          = 30
_IRQ_PASSKEY_ACTION      = 31

_SECRETS_FILE = "/flash/ble_bonds.json"

# Standard short UUIDs
_UUID_HID_SVC, _UUID_DEVINFO_SVC = UUID(0x1812), UUID(0x180A)
_UUID_HID_INFO, _UUID_REPORT_MAP, _UUID_HID_CTRL_PT = UUID(0x2A4A), UUID(0x2A4B), UUID(0x2A4C)
_UUID_REPORT, _UUID_PROTOCOL_MD = UUID(0x2A4D), UUID(0x2A4E)
_UUID_REPORT_REF, _UUID_PNP_ID = UUID(0x2908), UUID(0x2A50)
_UUID_MFR, _UUID_MODEL = UUID(0x2A29), UUID(0x2A24)


# Composite HID descriptor: keyboard (Report ID 1) + mouse (Report ID 2).
_HID_REPORT_MAP = bytes([
    # Keyboard
    0x05,0x01, 0x09,0x06, 0xA1,0x01, 0x85,0x01,
    0x75,0x01, 0x95,0x08, 0x05,0x07, 0x19,0xE0, 0x29,0xE7,
    0x15,0x00, 0x25,0x01, 0x81,0x02,                       # modifier byte
    0x95,0x01, 0x75,0x08, 0x81,0x01,                       # reserved
    0x95,0x05, 0x75,0x01, 0x05,0x08, 0x19,0x01, 0x29,0x05,
    0x91,0x02, 0x95,0x01, 0x75,0x03, 0x91,0x01,            # LED out
    0x95,0x06, 0x75,0x08, 0x15,0x00, 0x25,0x65,
    0x05,0x07, 0x19,0x00, 0x29,0x65, 0x81,0x00,            # 6-key array
    0xC0,
    # Mouse
    0x05,0x01, 0x09,0x02, 0xA1,0x01, 0x85,0x02,
    0x09,0x01, 0xA1,0x00,
    0x05,0x09, 0x19,0x01, 0x29,0x03, 0x15,0x00, 0x25,0x01,
    0x95,0x03, 0x75,0x01, 0x81,0x02,                       # 3 buttons
    0x95,0x01, 0x75,0x05, 0x81,0x03,                       # padding
    0x05,0x01, 0x09,0x30, 0x09,0x31, 0x09,0x38,
    0x15,0x81, 0x25,0x7F, 0x75,0x08, 0x95,0x03,
    0x81,0x06,                                             # dx, dy, wheel
    0xC0, 0xC0,
])

_HID_INFO_VAL = b"\x01\x01\x00\x00"
_PNP_ID_VAL   = struct.pack(">BHHH", 0x02, 0x05AC, 0x820A, 0x0123)

_DIS = (_UUID_DEVINFO_SVC, (
    (_UUID_MFR,    F_READ),
    (_UUID_MODEL,  F_READ),
    (_UUID_PNP_ID, F_READ),
))

_HIDS = (_UUID_HID_SVC, (
    (_UUID_HID_INFO,    F_READ),
    (_UUID_REPORT_MAP,  F_READ),
    (_UUID_HID_CTRL_PT, F_READ_WRITE_NORESPONSE),
    (_UUID_REPORT,      F_READ_NOTIFY_ENC,        ((_UUID_REPORT_REF, DSC_F_READ),)),  # KB in
    (_UUID_REPORT,      F_RW_NOTIFY_NORESP_ENC,   ((_UUID_REPORT_REF, DSC_F_READ),)),  # KB LED out
    (_UUID_REPORT,      F_READ_NOTIFY_ENC,        ((_UUID_REPORT_REF, DSC_F_READ),)),  # mouse in
    (_UUID_PROTOCOL_MD, F_READ_WRITE_NORESPONSE),
))


def _adv_payload(name):
    p = bytearray()
    p += b"\x02\x01\x06"                              # flags
    p += b"\x03\x19" + struct.pack("<H", 961)         # appearance: HID keyboard
    p += b"\x03\x03" + struct.pack("<H", 0x1812)      # service UUID
    n = name.encode()
    p += struct.pack("BB", 1 + len(n), 0x09) + n      # full local name
    return bytes(p)


# ---------------- BLE HID server ----------------

class BLEHID:
    def __init__(self, name="Cardputer-Adv"):
        self._conn = None
        self._secrets = self._load_secrets()
        self._ble = bluetooth.BLE()
        self._ble.irq(self._irq)
        try:
            self._ble.config(bond=True)
            self._ble.config(le_secure=True)
            self._ble.config(mitm=False)
            self._ble.config(io=3)             # NoInputNoOutput → JustWorks
        except Exception:
            pass
        self._ble.active(True)
        self._ble.config(gap_name=name)

        h = self._ble.gatts_register_services((_DIS, _HIDS))
        (h_mfr, h_model, h_pnp) = h[0]
        (h_info, h_hid, h_ctrl,
         self._h_kb_in,  h_kb_in_ref,
         self._h_kb_out, h_kb_out_ref,
         self._h_mouse_in, h_mouse_in_ref,
         h_proto) = h[1]

        self._ble.gatts_write(h_mfr,   b"M5Stack")
        self._ble.gatts_write(h_model, b"Cardputer-Adv")
        self._ble.gatts_write(h_pnp,   _PNP_ID_VAL)
        self._ble.gatts_write(h_info,  _HID_INFO_VAL)
        self._ble.gatts_write(h_hid,   _HID_REPORT_MAP)
        self._ble.gatts_write(h_ctrl,  b"\x00")
        self._ble.gatts_write(h_proto, b"\x01")
        self._ble.gatts_write(h_kb_in_ref,    struct.pack("<BB", 1, 1))
        self._ble.gatts_write(h_kb_out_ref,   struct.pack("<BB", 1, 2))
        self._ble.gatts_write(h_mouse_in_ref, struct.pack("<BB", 2, 1))
        self._ble.gatts_write(self._h_kb_in,    b"\x00" * 8)
        self._ble.gatts_write(self._h_kb_out,   b"\x00")
        self._ble.gatts_write(self._h_mouse_in, b"\x00" * 4)

        self._adv_data = _adv_payload(name)

    def start_advertising(self):
        self._ble.gap_advertise(100_000, adv_data=self._adv_data)

    def shutdown(self):
        try: self._ble.gap_advertise(None)
        except Exception: pass
        try: self._ble.active(False)
        except Exception: pass

    def is_connected(self):
        return self._conn is not None

    # --- bond key persistence (official ble_bonding_peripheral.py format) ---
    def _load_secrets(self):
        import binascii, json
        out = {}
        try:
            with open(_SECRETS_FILE) as f:
                for st, k, v in json.load(f):
                    out[(st, binascii.a2b_base64(k))] = binascii.a2b_base64(v)
        except Exception:
            pass
        return out

    def _save_secrets(self):
        import binascii, json
        try:
            with open(_SECRETS_FILE, "w") as f:
                json.dump([(st,
                            binascii.b2a_base64(k).decode().strip(),
                            binascii.b2a_base64(v).decode().strip())
                           for (st, k), v in self._secrets.items()], f)
        except Exception:
            pass

    def _irq(self, event, data):
        if event == _IRQ_CENTRAL_CONNECT:
            self._conn, _, _ = data
            try: self._ble.gap_pair(self._conn)
            except Exception: pass
        elif event == _IRQ_CENTRAL_DISCONNECT:
            self._conn = None
            self._save_secrets()
            try: self.start_advertising()
            except Exception: pass
        elif event == _IRQ_SET_SECRET:
            sec_type, key, value = data
            key = (sec_type, bytes(key))
            if value is None:
                if key in self._secrets:
                    del self._secrets[key]
                    return True
                return False
            self._secrets[key] = bytes(value)
            return True
        elif event == _IRQ_GET_SECRET:
            sec_type, index, key = data
            if key is None:
                i = 0
                for (t, _k), v in self._secrets.items():
                    if t == sec_type:
                        if i == index:
                            return v
                        i += 1
                return None
            return self._secrets.get((sec_type, bytes(key)), None)

    def send_kb(self, modifier, keys):
        """8-byte HID keyboard input: modifier, reserved, k0..k5."""
        if self._conn is None: return
        rep = bytearray(8)
        rep[0] = modifier & 0xFF
        for i, k in enumerate(keys[:6]):
            rep[2 + i] = k & 0xFF
        try:
            self._ble.gatts_write(self._h_kb_in, bytes(rep))
            self._ble.gatts_notify(self._conn, self._h_kb_in)
        except OSError:
            pass

    def send_mouse(self, buttons, dx, dy, wheel):
        """4-byte HID mouse input: buttons, dx, dy, wheel (signed int8)."""
        if self._conn is None: return
        def s8(v):
            v = int(v)
            if v >  127: v =  127
            if v < -127: v = -127
            return v & 0xFF
        rep = bytes((buttons & 0x07, s8(dx), s8(dy), s8(wheel)))
        try:
            self._ble.gatts_write(self._h_mouse_in, rep)
            self._ble.gatts_notify(self._conn, self._h_mouse_in)
        except OSError:
            pass


# ---------------- Keyboard char → HID code ----------------

def _char_to_hid(ch):
    if "a" <= ch <= "z": return False, 0x04 + ord(ch) - ord("a")
    if "A" <= ch <= "Z": return True,  0x04 + ord(ch) - ord("A")
    if "1" <= ch <= "9": return False, 0x1E + ord(ch) - ord("1")
    if ch == "0":        return False, 0x27
    table = {
        " ":(False,0x2C), "-":(False,0x2D), "_":(True,0x2D),
        "=":(False,0x2E), "+":(True,0x2E),  "[":(False,0x2F), "{":(True,0x2F),
        "]":(False,0x30), "}":(True,0x30),  "\\":(False,0x31),"|":(True,0x31),
        ";":(False,0x33), ":":(True,0x33),  "'":(False,0x34), "\"":(True,0x34),
        "`":(False,0x35), "~":(True,0x35),  ",":(False,0x36), "<":(True,0x36),
        ".":(False,0x37), ">":(True,0x37),  "/":(False,0x38), "?":(True,0x38),
        "!":(True,0x1E),  "@":(True,0x1F),  "#":(True,0x20),  "$":(True,0x21),
        "%":(True,0x22),  "^":(True,0x23),  "&":(True,0x24),  "*":(True,0x25),
        "(":(True,0x26),  ")":(True,0x27),
    }
    return table.get(ch, (False, 0))


_SPECIAL_HID = {
    KeyCode.KEYCODE_ENTER:     0x28,
    KeyCode.KEYCODE_BACKSPACE: 0x2A,
    KeyCode.KEYCODE_DEL:       0x4C,
    KeyCode.KEYCODE_SPACE:     0x2C,
}

_MOD_LSHIFT = 0x02


# ---------------- BMI270 accel + tilt → cursor ----------------
_BMI270_ADDR     = 0x69
_REG_CHIP_ID     = 0x00
_REG_INT_STATUS  = 0x21
_REG_ACC_X_LSB   = 0x0C
_REG_ACC_RANGE   = 0x41
_LSB_PER_G       = (16384, 8192, 4096, 2048)


def _bmi_init(i2c):
    if i2c.readfrom_mem(_BMI270_ADDR, _REG_CHIP_ID, 1)[0] != 0x24:
        raise OSError("BMI270 not found")
    if (i2c.readfrom_mem(_BMI270_ADDR, _REG_INT_STATUS, 1)[0] & 0x01) == 0:
        from micropython_bmi270 import bmi270
        bmi270.BMI270(i2c, address=_BMI270_ADDR)
    rng = i2c.readfrom_mem(_BMI270_ADDR, _REG_ACC_RANGE, 1)[0] & 0x03
    return 9.80665 / _LSB_PER_G[rng]


def _read_accel(i2c, scale):
    raw = i2c.readfrom_mem(_BMI270_ADDR, _REG_ACC_X_LSB, 6)
    ax, ay, _ = struct.unpack("<hhh", raw)
    return ax * scale, ay * scale


# Tilt response: bounce-game style, plus sub-pixel accumulator + small
# deadzone + continuous still-pose recalibration so it doesn't drift.
_TILT_NORM     = 6.0
_TILT_STEP     = 14.0
_TILT_DEADZONE = 0.18
_MAX_STEP      = 70
_STILL_WIN     = 30
_STILL_RANGE   = 0.20
_STILL_HOLD_MS = 1000


def _tilt_to_cursor(ax, ay, nx0, ny0, accum):
    rx = ax - nx0
    ry = ay - ny0
    if abs(rx) < _TILT_DEADZONE: rx = 0
    else: rx = rx - _TILT_DEADZONE if rx > 0 else rx + _TILT_DEADZONE
    if abs(ry) < _TILT_DEADZONE: ry = 0
    else: ry = ry - _TILT_DEADZONE if ry > 0 else ry + _TILT_DEADZONE

    accum[0] += -rx / _TILT_NORM * _TILT_STEP   # tilt right → cursor right
    accum[1] +=  ry / _TILT_NORM * _TILT_STEP   # tilt forward → cursor down

    dx = int(accum[0]); dy = int(accum[1])
    accum[0] -= dx; accum[1] -= dy
    if dx >  _MAX_STEP: dx =  _MAX_STEP
    if dx < -_MAX_STEP: dx = -_MAX_STEP
    if dy >  _MAX_STEP: dy =  _MAX_STEP
    if dy < -_MAX_STEP: dy = -_MAX_STEP
    return dx, dy


def _capture_neutral(i2c, scale, samples=50):
    sx = sy = 0.0
    for _ in range(samples):
        ax, ay = _read_accel(i2c, scale)
        sx += ax; sy += ay
        time.sleep_ms(10)
    return (sx / samples, sy / samples)


# ---------------- ES8311 codec power-down (M5Unified leaves it hot) ----------------
def _spk_off():
    try:
        i2c = I2C(1, sda=Pin(8), scl=Pin(9), freq=400000)
        for r, v in ((0x32,0x00),(0x12,0x02),(0x13,0x10),(0x0E,0xFF),
                     (0x14,0x00),(0x0D,0xFA),(0x37,0x08),(0x00,0x00)):
            i2c.writeto_mem(0x18, r, bytes([v]))
    except Exception:
        pass


# ---------------- UI ----------------
_BG = 0x000000; _FG = 0xFFFFFF; _DIM = 0x666666
_HDR_BG = 0x232332; _HDR_FG = 0xFFD040
_OK = 0x00DD66; _BLUE = 0x40A0FF; _RED = 0xFF6060

_FONT  = M5.Lcd.FONTS.DejaVu18
_SMALL = M5.Lcd.FONTS.DejaVu12
_PAD   = 6
_HDR_H = 22
_HINT_H = 16


def _hdr(text, color=_HDR_FG):
    Lcd.fillRect(0, 0, Lcd.width(), _HDR_H, _HDR_BG)
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(color, _HDR_BG)
    Lcd.setCursor(_PAD, 6)
    Lcd.print(text[:38])


def _hint(text):
    y = Lcd.height() - _HINT_H
    Lcd.fillRect(0, y, Lcd.width(), _HINT_H, _BG)
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(_DIM, _BG)
    Lcd.setCursor(_PAD, y + 2)
    Lcd.print(text)


def run():
    Lcd.clear(_BG)
    kb_in = MatrixKeyboard()
    _spk_off()

    _hdr("BLE HID  init IMU...")
    try:
        i2c = I2C(1, sda=Pin(8), scl=Pin(9), freq=400_000)
        scale = _bmi_init(i2c)
    except Exception as e:
        _hdr("IMU init failed", color=_RED)
        Lcd.setFont(_SMALL); Lcd.setTextColor(_RED, _BG)
        Lcd.setCursor(_PAD, _HDR_H + 6); Lcd.print(repr(e)[:38])
        while kb_in.get_key() != KeyCode.KEYCODE_ESC:
            time.sleep_ms(40)
        return

    _hdr("BLE HID  starting BLE...")
    try:
        hid = BLEHID("Cardputer-Adv")
        hid.start_advertising()
    except Exception as e:
        _hdr("BLE init failed", color=_RED)
        Lcd.setFont(_SMALL); Lcd.setTextColor(_RED, _BG)
        Lcd.setCursor(_PAD, _HDR_H + 6); Lcd.print(repr(e)[:38])
        while kb_in.get_key() != KeyCode.KEYCODE_ESC:
            time.sleep_ms(40)
        return

    _hdr("Calibrating  hold flat 1s", color=_BLUE)
    neutral = _capture_neutral(i2c, scale, samples=50)

    _hdr("BLE HID  advertising 'Cardputer-Adv'", color=_BLUE)
    _hint("<-=L  ->=R  ^v=scroll  Tab=mid  ESC=quit")

    Lcd.setFont(_SMALL)
    Lcd.setTextColor(_DIM, _BG)
    Lcd.setCursor(_PAD, _HDR_H + 6); Lcd.print("type → keyboard")
    Lcd.setCursor(_PAD, _HDR_H + 22); Lcd.print("tilt → mouse")
    Lcd.setCursor(_PAD, _HDR_H + 38); Lcd.print("ESC → quit")

    accum = [0.0, 0.0]
    still_win = []
    still_since = -1
    last_send = 0
    last_conn = False
    SEND_MS = 30
    tail = ""
    last_paint = 0
    # Diagnostic counters — visible on screen so we can tell at a glance
    # whether reports are actually being sent to the host.
    n_mouse = 0
    n_kb = 0

    while True:
        # Connection-state header (cheap polling).
        cur_conn = hid.is_connected()
        if cur_conn != last_conn:
            last_conn = cur_conn
            if cur_conn:
                _hdr("BLE HID  CONNECTED", color=_OK)
            else:
                _hdr("BLE HID  advertising", color=_BLUE)

        # Mouse motion (every frame)
        try:
            ax, ay = _read_accel(i2c, scale)
        except Exception:
            ax = ay = 0.0
        now = time.ticks_ms()

        # Continuous still-pose re-cal (auto-fixes uneven desk / shifted pose)
        still_win.append((ax, ay))
        if len(still_win) > _STILL_WIN:
            still_win.pop(0)
        if len(still_win) == _STILL_WIN:
            xmn = ymn = 1e9; xmx = ymx = -1e9
            for sx, sy in still_win:
                if sx < xmn: xmn = sx
                if sx > xmx: xmx = sx
                if sy < ymn: ymn = sy
                if sy > ymx: ymx = sy
            if (xmx - xmn) < _STILL_RANGE and (ymx - ymn) < _STILL_RANGE:
                if still_since < 0:
                    still_since = now
                elif now - still_since >= _STILL_HOLD_MS:
                    sxs = sys_ = 0.0
                    for sx, sy in still_win:
                        sxs += sx; sys_ += sy
                    neutral = (sxs / _STILL_WIN, sys_ / _STILL_WIN)
                    accum[0] = accum[1] = 0.0
                    still_since = now
            else:
                still_since = -1

        dx, dy = _tilt_to_cursor(ax, ay, neutral[0], neutral[1], accum)
        if hid.is_connected() and (dx or dy) and \
           time.ticks_diff(now, last_send) >= SEND_MS:
            hid.send_mouse(0, dx, dy, 0)
            last_send = now
            n_mouse += 1

        # Keys: clicks via arrow cluster; everything else types
        k = kb_in.get_key()
        if k is not None:
            if k == KeyCode.KEYCODE_ESC:
                hid.shutdown()
                return
            elif not hid.is_connected():
                pass  # swallow keys while not paired
            elif k == KeyCode.KEYCODE_LEFT:
                hid.send_mouse(0x01, 0, 0, 0)
                time.sleep_ms(15)
                hid.send_mouse(0, 0, 0, 0)
                n_mouse += 2
            elif k == KeyCode.KEYCODE_RIGHT:
                hid.send_mouse(0x02, 0, 0, 0)
                time.sleep_ms(15)
                hid.send_mouse(0, 0, 0, 0)
            elif k == KeyCode.KEYCODE_TAB:
                hid.send_mouse(0x04, 0, 0, 0)
                time.sleep_ms(15)
                hid.send_mouse(0, 0, 0, 0)
            elif k == KeyCode.KEYCODE_UP:
                hid.send_mouse(0, 0, 0, 1)
                time.sleep_ms(8)
                hid.send_mouse(0, 0, 0, 0)
            elif k == KeyCode.KEYCODE_DOWN:
                hid.send_mouse(0, 0, 0, -1)
                time.sleep_ms(8)
                hid.send_mouse(0, 0, 0, 0)
            elif k in _SPECIAL_HID:
                hid.send_kb(0, [_SPECIAL_HID[k]])
                time.sleep_ms(8)
                hid.send_kb(0, [])
                n_kb += 2
                if k == KeyCode.KEYCODE_ENTER:
                    tail = ""
                elif k == KeyCode.KEYCODE_BACKSPACE or k == KeyCode.KEYCODE_DEL:
                    tail = tail[:-1] if tail else ""
                elif k == KeyCode.KEYCODE_SPACE:
                    tail = (tail + " ")[-16:]
            elif isinstance(k, int) and 32 <= k <= 126:
                ch = chr(k)
                shift, code = _char_to_hid(ch)
                if code:
                    hid.send_kb(_MOD_LSHIFT if shift else 0, [code])
                    time.sleep_ms(8)
                    hid.send_kb(0, [])
                    tail = (tail + ch)[-16:]
                    n_kb += 2

        # Periodic redraw of preview tail + diagnostic counters
        if time.ticks_diff(now, last_paint) > 200:
            Lcd.fillRect(0, _HDR_H + 54, Lcd.width(),
                         Lcd.height() - _HDR_H - 54 - _HINT_H, _BG)
            Lcd.setFont(_FONT)
            Lcd.setTextColor(_OK, _BG)
            Lcd.setCursor(_PAD, _HDR_H + 56)
            Lcd.print(tail)
            Lcd.setFont(_SMALL)
            Lcd.setTextColor(_DIM, _BG)
            Lcd.setCursor(_PAD, _HDR_H + 80)
            Lcd.print("sent  mouse:{} kb:{}".format(n_mouse, n_kb))
            last_paint = now

        time.sleep_ms(5)
