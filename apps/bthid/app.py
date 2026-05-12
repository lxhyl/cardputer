"""BLE HID — composite keyboard + mouse over a single GATT service.

One BLE peripheral, one bond on the host, three modes selectable from
the device:

  - **Macros**: pick from a list, hit Enter to send a key chord
    (e.g. Lock screen = Cmd+Ctrl+Q).
  - **Live keyboard**: every Cardputer keystroke forwards as a real
    key event (passthrough mode).
  - **Mouse**: tilt the device to move the cursor (BMI270
    accelerometer); Space/Enter/M for L/R/M click; W/S for scroll;
    C to recalibrate the level pose.

GATT structure:
  - HID Info, Report Map (composite — both keyboard + mouse), Control
    Point, Protocol Mode.
  - Keyboard input report (Report ID 1) — 8-byte notify, encrypted.
  - Keyboard output report (Report ID 1) — 1-byte LED state from host.
  - Mouse input report (Report ID 2) — 4-byte notify, encrypted.

Pairing/bonding/encryption identical to the old btmacro app — see the
BLE HID section of CLAUDE.md for the why behind every config flag.
Bond persists in /flash/ble_bonds.json (compatible with the previous
btmacro file so existing pairings carry over).
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

# ---------------- BLE flag shorthand ----------------
F_READ = bluetooth.FLAG_READ
F_WRITE = bluetooth.FLAG_WRITE
F_READ_NOTIFY = bluetooth.FLAG_READ | bluetooth.FLAG_NOTIFY
F_READ_WRITE_NORESPONSE = (bluetooth.FLAG_READ | bluetooth.FLAG_WRITE
                           | bluetooth.FLAG_WRITE_NO_RESPONSE)
F_READ_WRITE_NOTIFY_NORESPONSE = (bluetooth.FLAG_READ | bluetooth.FLAG_WRITE
                                  | bluetooth.FLAG_NOTIFY
                                  | bluetooth.FLAG_WRITE_NO_RESPONSE)
_FLAG_READ_ENCRYPTED  = 0x0200
_FLAG_WRITE_ENCRYPTED = 0x1000
F_READ_NOTIFY_ENCRYPTED = F_READ_NOTIFY | _FLAG_READ_ENCRYPTED
F_READ_WRITE_NOTIFY_NORESPONSE_ENCRYPTED = (F_READ_WRITE_NOTIFY_NORESPONSE
                                            | _FLAG_READ_ENCRYPTED
                                            | _FLAG_WRITE_ENCRYPTED)
DSC_F_READ = 0x02

# IRQ events
_IRQ_CENTRAL_CONNECT     = 1
_IRQ_CENTRAL_DISCONNECT  = 2
_IRQ_GATTS_WRITE         = 3
_IRQ_MTU_EXCHANGED       = 21
_IRQ_CONNECTION_UPDATE   = 27
_IRQ_ENCRYPTION_UPDATE   = 28
_IRQ_GET_SECRET          = 29
_IRQ_SET_SECRET          = 30
_IRQ_PASSKEY_ACTION      = 31

# Bond file path — same as old btmacro so an existing pairing carries over.
_SECRETS_FILE = "/flash/ble_bonds.json"

# UUIDs
_UUID_HID_SVC      = UUID(0x1812)
_UUID_DEVINFO_SVC  = UUID(0x180A)
_UUID_BAT_SVC      = UUID(0x180F)
_UUID_HID_INFO     = UUID(0x2A4A)
_UUID_REPORT_MAP   = UUID(0x2A4B)
_UUID_HID_CTRL_PT  = UUID(0x2A4C)
_UUID_REPORT       = UUID(0x2A4D)
_UUID_PROTOCOL_MD  = UUID(0x2A4E)
_UUID_REPORT_REF   = UUID(0x2908)
_UUID_PNP_ID       = UUID(0x2A50)
_UUID_MFR          = UUID(0x2A29)
_UUID_MODEL        = UUID(0x2A24)
_UUID_BAT_LEVEL    = UUID(0x2A19)


# ---------------- Composite HID Report Descriptor ----------------
# Two top-level Application collections in one descriptor:
#   - Report ID 1 = Keyboard (8-byte input, 1-byte LED output)
#   - Report ID 2 = Mouse    (4-byte input)
_HID_REPORT_MAP = bytes([
    # ===== Keyboard =====
    0x05, 0x01,  # Usage Page (Generic Desktop)
    0x09, 0x06,  # Usage (Keyboard)
    0xA1, 0x01,  # Collection (Application)
    0x85, 0x01,  #   Report ID (1)
    0x75, 0x01,  #   Report Size (1)
    0x95, 0x08,  #   Report Count (8)
    0x05, 0x07,  #   Usage Page (Key Codes)
    0x19, 0xE0,  #   Usage Min (224)
    0x29, 0xE7,  #   Usage Max (231)
    0x15, 0x00,  #   Logical Min (0)
    0x25, 0x01,  #   Logical Max (1)
    0x81, 0x02,  #   Input (Data, Var, Abs) — modifier byte
    0x95, 0x01,  #   Report Count (1)
    0x75, 0x08,  #   Report Size (8)
    0x81, 0x01,  #   Input (Const) — reserved
    0x95, 0x05,  #   Report Count (5)
    0x75, 0x01,  #   Report Size (1)
    0x05, 0x08,  #   Usage Page (LEDs)
    0x19, 0x01,  #   Usage Min (Num Lock)
    0x29, 0x05,  #   Usage Max (Kana)
    0x91, 0x02,  #   Output (Data, Var, Abs) — LED report
    0x95, 0x01,  #   Report Count (1)
    0x75, 0x03,  #   Report Size (3)
    0x91, 0x01,  #   Output (Const) — LED padding
    0x95, 0x06,  #   Report Count (6)
    0x75, 0x08,  #   Report Size (8)
    0x15, 0x00,  #   Logical Min (0)
    0x25, 0x65,  #   Logical Max (101)
    0x05, 0x07,  #   Usage Page (Key Codes)
    0x19, 0x00,  #   Usage Min (0)
    0x29, 0x65,  #   Usage Max (101)
    0x81, 0x00,  #   Input (Data, Array) — 6-byte key array
    0xC0,        # End Collection (Keyboard)

    # ===== Mouse =====
    0x05, 0x01,  # Usage Page (Generic Desktop)
    0x09, 0x02,  # Usage (Mouse)
    0xA1, 0x01,  # Collection (Application)
    0x85, 0x02,  #   Report ID (2)
    0x09, 0x01,  #   Usage (Pointer)
    0xA1, 0x00,  #   Collection (Physical)
    0x05, 0x09,  #     Usage Page (Buttons)
    0x19, 0x01, 0x29, 0x03,  # Usage Min/Max (1..3)
    0x15, 0x00, 0x25, 0x01,
    0x95, 0x03, 0x75, 0x01,
    0x81, 0x02,  # 3 buttons (left/right/middle)
    0x95, 0x01, 0x75, 0x05,
    0x81, 0x03,  # 5-bit padding to round byte
    0x05, 0x01,  # Usage Page (Generic Desktop)
    0x09, 0x30, 0x09, 0x31, 0x09, 0x38,  # X, Y, Wheel
    0x15, 0x81, 0x25, 0x7F,  # signed -127..127
    0x75, 0x08, 0x95, 0x03,
    0x81, 0x06,  # dx, dy, wheel (relative)
    0xC0,        # End Collection (Physical)
    0xC0,        # End Collection (Mouse)
])


# ---------------- Service tree ----------------
_HID_INFO_VAL = b"\x01\x01\x00\x00"
# PnP: vendor src=USB(2), VID=0x05AC (Apple), PID=0x820A, ver=0x0123 — big-endian
_PNP_ID_VAL = struct.pack(">BHHH", 0x02, 0x05AC, 0x820A, 0x0123)

_DIS = (
    _UUID_DEVINFO_SVC,
    (
        (_UUID_MFR,    F_READ),
        (_UUID_MODEL,  F_READ),
        (_UUID_PNP_ID, F_READ),
    ),
)

_BAS = (
    _UUID_BAT_SVC,
    ((_UUID_BAT_LEVEL, F_READ_NOTIFY),),
)

_HIDS = (
    _UUID_HID_SVC,
    (
        (_UUID_HID_INFO,    F_READ),                                       # 0
        (_UUID_REPORT_MAP,  F_READ),                                       # 1
        (_UUID_HID_CTRL_PT, F_READ_WRITE_NORESPONSE),                      # 2
        # Keyboard input (Report ID 1)
        (_UUID_REPORT,      F_READ_NOTIFY_ENCRYPTED, (                     # 3 val
            (_UUID_REPORT_REF, DSC_F_READ),                                # 4 desc
        )),
        # Keyboard output (LED, Report ID 1)
        (_UUID_REPORT,      F_READ_WRITE_NOTIFY_NORESPONSE_ENCRYPTED, (    # 5 val
            (_UUID_REPORT_REF, DSC_F_READ),                                # 6 desc
        )),
        # Mouse input (Report ID 2)
        (_UUID_REPORT,      F_READ_NOTIFY_ENCRYPTED, (                     # 7 val
            (_UUID_REPORT_REF, DSC_F_READ),                                # 8 desc
        )),
        (_UUID_PROTOCOL_MD, F_READ_WRITE_NORESPONSE),                      # 9
    ),
)

# Advertise as a Keyboard (961). Mac's icon will be a keyboard but the
# composite descriptor still exposes mouse to the host.
_APPEARANCE = 961


def _adv_payload(name):
    p = bytearray()
    p += b"\x02\x01\x06"
    p += b"\x03\x19" + struct.pack("<H", _APPEARANCE)
    p += b"\x03\x03" + struct.pack("<H", 0x1812)
    n = name.encode()
    p += struct.pack("BB", 1 + len(n), 0x09) + n
    return bytes(p)


# ---------------- BLE HID server ----------------

class BLEHID:
    def __init__(self, name="Cardputer-Adv"):
        self._name = name
        self._conn = None
        self._secrets = self._load_secrets()

        self._ble = bluetooth.BLE()
        # CRITICAL: BLE is a process-wide singleton. If a previous run /
        # app session already activated it, calling `config(bond=True)`
        # below reaches into NimBLE on a live stack and triggers a hard
        # fault (full ESP32 reset, no Python traceback). Force a clean
        # deactivate-reactivate cycle before reconfiguring security.
        try: self._ble.gap_advertise(None)
        except Exception: pass
        try:
            if self._ble.active():
                self._ble.active(False)
                time.sleep_ms(100)
        except Exception:
            pass

        self._ble.irq(self._irq)
        try:
            self._ble.config(bond=True)
            self._ble.config(le_secure=True)
            self._ble.config(mitm=False)
            self._ble.config(io=3)
        except Exception:
            pass
        self._ble.active(True)
        self._ble.config(gap_name=name)

        handles = self._ble.gatts_register_services((_DIS, _BAS, _HIDS))
        (h_mfr, h_model, h_pnp) = handles[0]
        (self._h_bat,) = handles[1]
        (h_info, h_hid, h_ctrl,
         self._h_kb_in,  h_kb_in_ref,
         self._h_kb_out, h_kb_out_ref,
         self._h_mouse_in, h_mouse_in_ref,
         h_proto) = handles[2]

        self._ble.gatts_write(h_mfr,   b"M5Stack")
        self._ble.gatts_write(h_model, b"Cardputer-Adv")
        self._ble.gatts_write(h_pnp,   _PNP_ID_VAL)
        self._ble.gatts_write(self._h_bat, b"\x64")  # 100%
        self._ble.gatts_write(h_info,  _HID_INFO_VAL)
        self._ble.gatts_write(h_hid,   _HID_REPORT_MAP)
        self._ble.gatts_write(h_ctrl,  b"\x00")
        self._ble.gatts_write(h_proto, b"\x01")  # report mode
        # Report Reference descriptors: (report_id, report_type)
        # type 1=Input, 2=Output
        self._ble.gatts_write(h_kb_in_ref,    struct.pack("<BB", 1, 1))
        self._ble.gatts_write(h_kb_out_ref,   struct.pack("<BB", 1, 2))
        self._ble.gatts_write(h_mouse_in_ref, struct.pack("<BB", 2, 1))
        # Seed report values so reads before any input return zeros
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

    # ---- bond persistence (official ble_bonding_peripheral.py pattern) ----
    def _load_secrets(self):
        import binascii, json
        secrets = {}
        try:
            with open(_SECRETS_FILE) as f:
                for sec_type, key, value in json.load(f):
                    secrets[(sec_type, binascii.a2b_base64(key))] = \
                        binascii.a2b_base64(value)
        except Exception:
            pass
        return secrets

    def _save_secrets(self):
        import binascii, json
        try:
            entries = [
                (st, binascii.b2a_base64(k).decode().strip(),
                     binascii.b2a_base64(v).decode().strip())
                for (st, k), v in self._secrets.items()
            ]
            with open(_SECRETS_FILE, "w") as f:
                json.dump(entries, f)
        except Exception:
            pass

    def _irq(self, event, data):
        if event == _IRQ_CENTRAL_CONNECT:
            self._conn, _, _ = data
            try:
                self._ble.gap_pair(self._conn)
            except Exception:
                pass
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

    # ---- send helpers ----
    def send_kb_report(self, modifier, keys):
        """8-byte keyboard input report (modifier, reserved, k0..k5)."""
        if self._conn is None:
            return False
        rep = bytearray(8)
        rep[0] = modifier & 0xFF
        for i, k in enumerate(keys[:6]):
            rep[2 + i] = k & 0xFF
        try:
            self._ble.gatts_write(self._h_kb_in, bytes(rep))
            self._ble.gatts_notify(self._conn, self._h_kb_in)
            return True
        except OSError:
            return False

    def send_kb_release(self):
        return self.send_kb_report(0, [])

    def send_mouse_report(self, buttons, dx, dy, wheel):
        """4-byte mouse input report (buttons, dx, dy, wheel)."""
        if self._conn is None:
            return False
        def _s8(v):
            v = int(v)
            if v > 127: v = 127
            if v < -127: v = -127
            return v & 0xFF
        rep = bytes((buttons & 0x07, _s8(dx), _s8(dy), _s8(wheel)))
        try:
            self._ble.gatts_write(self._h_mouse_in, rep)
            self._ble.gatts_notify(self._conn, self._h_mouse_in)
            return True
        except OSError:
            return False


# ---------------- HID code tables (US ANSI) ----------------

def _char_to_hid(ch):
    if "a" <= ch <= "z":
        return False, 0x04 + ord(ch) - ord("a")
    if "A" <= ch <= "Z":
        return True, 0x04 + ord(ch) - ord("A")
    if "1" <= ch <= "9":
        return False, 0x1E + ord(ch) - ord("1")
    if ch == "0":
        return False, 0x27
    table = {
        " ":  (False, 0x2C),
        "-":  (False, 0x2D), "_":  (True,  0x2D),
        "=":  (False, 0x2E), "+":  (True,  0x2E),
        "[":  (False, 0x2F), "{":  (True,  0x2F),
        "]":  (False, 0x30), "}":  (True,  0x30),
        "\\": (False, 0x31), "|":  (True,  0x31),
        ";":  (False, 0x33), ":":  (True,  0x33),
        "'":  (False, 0x34), "\"": (True,  0x34),
        "`":  (False, 0x35), "~":  (True,  0x35),
        ",":  (False, 0x36), "<":  (True,  0x36),
        ".":  (False, 0x37), ">":  (True,  0x37),
        "/":  (False, 0x38), "?":  (True,  0x38),
        "!":  (True,  0x1E), "@":  (True,  0x1F), "#":  (True,  0x20),
        "$":  (True,  0x21), "%":  (True,  0x22), "^":  (True,  0x23),
        "&":  (True,  0x24), "*":  (True,  0x25), "(":  (True,  0x26),
        ")":  (True,  0x27),
    }
    return table.get(ch, (False, 0))


_SPECIAL_HID = {
    KeyCode.KEYCODE_ENTER:     0x28,
    KeyCode.KEYCODE_BACKSPACE: 0x2A,
    KeyCode.KEYCODE_DEL:       0x4C,
    KeyCode.KEYCODE_TAB:       0x2B,
    KeyCode.KEYCODE_SPACE:     0x2C,
    KeyCode.KEYCODE_UP:        0x52,
    KeyCode.KEYCODE_DOWN:      0x51,
    KeyCode.KEYCODE_LEFT:      0x50,
    KeyCode.KEYCODE_RIGHT:     0x4F,
}

_MOD_LCTRL  = 0x01
_MOD_LSHIFT = 0x02
_MOD_LGUI   = 0x08


# ---------------- BMI270 raw I/O (mirrors apps/sensor/imu) ----------------
_BMI270_ADDR = 0x69
_REG_CHIP_ID = 0x00
_REG_INTERNAL_STATUS = 0x21
_REG_ACC_X_LSB = 0x0C
_REG_GYR_X_LSB = 0x12
_REG_ACC_RANGE = 0x41
_REG_GYR_RANGE = 0x43
_REG_PWR_CTRL  = 0x7D
_G = 9.80665
_LSB_PER_G = (16384, 8192, 4096, 2048)
# GYR_RANGE bits[2:0]: 0=±2000 dps, 1=±1000, 2=±500, 3=±250, 4=±125
_GYR_DPS_PER_LSB = (2000.0/32768, 1000.0/32768, 500.0/32768,
                    250.0/32768, 125.0/32768)


def _bmi_init(i2c):
    """Bring up BMI270, return (accel_scale, gyro_scale_dps)."""
    chip = i2c.readfrom_mem(_BMI270_ADDR, _REG_CHIP_ID, 1)[0]
    if chip != 0x24:
        raise OSError("BMI270 chip_id 0x{:02x}, expected 0x24".format(chip))
    status = i2c.readfrom_mem(_BMI270_ADDR, _REG_INTERNAL_STATUS, 1)[0]
    if (status & 0x01) == 0:
        from micropython_bmi270 import bmi270
        bmi270.BMI270(i2c, address=_BMI270_ADDR)
    # PWR_CTRL: enable accel + gyro + temp (sensor app only enables accel).
    # Without the gyr_en bit, gyro registers stay at 0x8000 (data-not-ready).
    cur = i2c.readfrom_mem(_BMI270_ADDR, _REG_PWR_CTRL, 1)[0]
    i2c.writeto_mem(_BMI270_ADDR, _REG_PWR_CTRL, bytes([cur | 0x0E]))
    time.sleep_ms(10)
    a_rng = i2c.readfrom_mem(_BMI270_ADDR, _REG_ACC_RANGE, 1)[0] & 0x03
    g_rng = i2c.readfrom_mem(_BMI270_ADDR, _REG_GYR_RANGE, 1)[0] & 0x07
    g_rng = min(g_rng, len(_GYR_DPS_PER_LSB) - 1)
    return _G / _LSB_PER_G[a_rng], _GYR_DPS_PER_LSB[g_rng]


def _read_accel(i2c, scale):
    raw = i2c.readfrom_mem(_BMI270_ADDR, _REG_ACC_X_LSB, 6)
    ax, ay, az = struct.unpack("<hhh", raw)
    return ax * scale, ay * scale, az * scale


def _read_gyro(i2c, scale_dps):
    """Returns (gx, gy, gz) in degrees per second."""
    raw = i2c.readfrom_mem(_BMI270_ADDR, _REG_GYR_X_LSB, 6)
    gx, gy, gz = struct.unpack("<hhh", raw)
    return gx * scale_dps, gy * scale_dps, gz * scale_dps


# ---------------- Tilt → cursor (bounce-game style) ----------------
# Same idea as apps/games/bounce: read raw accelerometer, subtract a
# neutral pose, normalise to -1..+1 range, multiply by step. Tilt the
# device the way you want the cursor to go.
_TILT_NORM     = 6.0   # m/s²; ~37° tilt = full speed
_TILT_STEP     = 14.0  # px per frame at full tilt (raised: 27" monitors
                       # need real speed at full tilt — was 8 = ~270 px/s,
                       # now ~1400 px/s)
_TILT_DEADZONE = 0.18  # m/s²; ~1° — kills sensor noise / sub-pixel drift
_MAX_STEP      = 70    # per-frame cap; 70 @ 30Hz ≈ 2100 px/s peak

# Continuous still-pose re-calibration — auto-fixes the case where the
# device's actual rest pose differs from whatever was captured at app
# entry (uneven desk, user placed it down differently after pressing
# Enter, etc).
_STILL_WIN     = 30    # samples (~1 s @ 30 Hz)
_STILL_RANGE   = 0.20  # m/s² max-min over window to count as "still"
_STILL_HOLD_MS = 1000  # how long it must be still before re-cal kicks in


def _tilt_to_cursor(ax, ay, neutral_ax, neutral_ay, accum):
    """Bounce-style: cursor velocity ∝ tilt magnitude.

    `accum` is a 2-element list — sub-pixel accumulator so very small
    tilts (which produce <1 px/frame) eventually move the cursor instead
    of being silently truncated to 0. Without the deadzone the same
    accumulator silently turns ±0.1 m/s² accelerometer noise into a
    visible drift (~4 px/sec).
    """
    rx = ax - neutral_ax
    ry = ay - neutral_ay
    # Symmetric deadzone subtracted off the magnitude (so just past the
    # deadzone you get gentle motion, not a jump).
    if abs(rx) < _TILT_DEADZONE:
        rx = 0
    else:
        rx = rx - _TILT_DEADZONE if rx > 0 else rx + _TILT_DEADZONE
    if abs(ry) < _TILT_DEADZONE:
        ry = 0
    else:
        ry = ry - _TILT_DEADZONE if ry > 0 else ry + _TILT_DEADZONE

    nx = -rx / _TILT_NORM   # tilt right → cursor right
    ny =  ry / _TILT_NORM   # tilt forward (top down) → cursor down (Y inverted vs prev)

    accum[0] += nx * _TILT_STEP
    accum[1] += ny * _TILT_STEP

    dx = int(accum[0])
    dy = int(accum[1])
    accum[0] -= dx
    accum[1] -= dy

    if dx >  _MAX_STEP: dx =  _MAX_STEP
    if dx < -_MAX_STEP: dx = -_MAX_STEP
    if dy >  _MAX_STEP: dy =  _MAX_STEP
    if dy < -_MAX_STEP: dy = -_MAX_STEP
    return dx, dy


# ---------------- Macros ----------------

def _shortcut(hid, mods, keys):
    hid.send_kb_report(mods, keys)
    time.sleep_ms(20)
    hid.send_kb_release()


_LIVE_TYPING = "_LIVE_"
_MOUSE_MODE  = "_MOUSE_"
_COMBO_MODE  = "_COMBO_"

# Macro list. fn is either a sentinel (handled in run() with a sub-mode)
# or a callable taking the BLEHID instance.
_MACROS = (
    ("> Combo (type + tilt)",  _COMBO_MODE),
    ("> Live keyboard mode",   _LIVE_TYPING),
    ("> Mouse mode (tilt)",    _MOUSE_MODE),
    ("Lock screen (Cmd+Ctrl+Q)",
     lambda h: _shortcut(h, _MOD_LGUI | _MOD_LCTRL, [0x14])),  # Q = 0x14
)


# ---------------- UI ----------------
_BG = 0x000000
_FG = 0xFFFFFF
_DIM = 0x666666
_HDR_BG = 0x232332
_HDR_FG = 0xFFD040
_OK = 0x00DD66
_RED = 0xFF6060
_BLUE = 0x40A0FF
_SEL_BG = 0xFFFFFF
_SEL_FG = 0x000000

_FONT = M5.Lcd.FONTS.DejaVu18
_SMALL = M5.Lcd.FONTS.DejaVu12
_PAD = 6
_HDR_H = 22
_HINT_H = 16
_LIST_TOP = 28
_LINE_H = 20
_VISIBLE = 4


def _truncate(s, n):
    return s if len(s) <= n else s[:n - 1] + "."


def _draw_header(text, color=_HDR_FG):
    Lcd.fillRect(0, 0, Lcd.width(), _HDR_H, _HDR_BG)
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(color, _HDR_BG)
    Lcd.setCursor(_PAD, 6)
    Lcd.print(_truncate(text, 38))


def _draw_hint(text):
    y = Lcd.height() - _HINT_H
    Lcd.fillRect(0, y, Lcd.width(), _HINT_H, _BG)
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(_DIM, _BG)
    Lcd.setCursor(_PAD, y + 2)
    Lcd.print(text)


def _draw_macro_list(idx, scroll, header):
    Lcd.fillRect(0, _HDR_H + 1, Lcd.width(),
                 Lcd.height() - _HDR_H - 1 - _HINT_H, _BG)
    _draw_header(header)
    Lcd.setFont(_FONT)
    visible = _MACROS[scroll:scroll + _VISIBLE]
    for i, (name, _fn) in enumerate(visible):
        actual = scroll + i
        y = _LIST_TOP + i * _LINE_H
        is_sel = actual == idx
        if is_sel:
            Lcd.fillRect(0, y - 2, Lcd.width(), _LINE_H, _SEL_BG)
            Lcd.setTextColor(_SEL_FG, _SEL_BG)
        else:
            Lcd.setTextColor(_FG, _BG)
        Lcd.setCursor(_PAD, y)
        Lcd.print(_truncate(name, 26))


# ---------------- Submodes ----------------

def _live_typing_mode(kb_in, hid):
    Lcd.clear(_BG)
    _draw_header("LIVE keyboard → BLE host", color=_OK)
    _draw_hint("ESC = back to menu")
    Lcd.setFont(_FONT)
    Lcd.setTextColor(_DIM, _BG)
    Lcd.setCursor(_PAD, _HDR_H + 8)
    Lcd.print("type away...")

    tail = ""
    def repaint():
        Lcd.fillRect(0, _HDR_H + 30, Lcd.width(),
                     Lcd.height() - _HDR_H - 30 - _HINT_H, _BG)
        Lcd.setFont(_FONT)
        Lcd.setTextColor(_OK, _BG)
        show = tail[-16:]
        pretty = ""
        for c in show:
            if c == "\n": pretty += "_"
            elif c == "\b": pretty += "<"
            elif c == "\t": pretty += ">"
            else: pretty += c
        Lcd.setCursor(_PAD, _HDR_H + 32)
        Lcd.print(pretty)

    while True:
        k = kb_in.get_key()
        if k is None:
            time.sleep_ms(15)
            continue
        if k == KeyCode.KEYCODE_ESC:
            return
        if not hid.is_connected():
            Lcd.setFont(_SMALL)
            Lcd.setTextColor(_RED, _BG)
            Lcd.setCursor(_PAD, _HDR_H + 70)
            Lcd.print("not connected — pair host first")
            continue

        try:
            if k in _SPECIAL_HID:
                hid_code = _SPECIAL_HID[k]
                hid.send_kb_report(0, [hid_code])
                time.sleep_ms(8)
                hid.send_kb_release()
                if k == KeyCode.KEYCODE_ENTER:
                    tail = ""  # submit-and-clear
                elif k == KeyCode.KEYCODE_BACKSPACE or k == KeyCode.KEYCODE_DEL:
                    tail = tail[:-1] if tail else ""
                elif k == KeyCode.KEYCODE_SPACE:
                    tail += " "
                elif k == KeyCode.KEYCODE_TAB:
                    tail += "\t"
            elif isinstance(k, int) and 32 <= k <= 126:
                ch = chr(k)
                shift, hid_code = _char_to_hid(ch)
                if hid_code:
                    mod = _MOD_LSHIFT if shift else 0
                    hid.send_kb_report(mod, [hid_code])
                    time.sleep_ms(8)
                    hid.send_kb_release()
                    tail += ch
            else:
                continue
            repaint()
        except Exception as e:
            Lcd.setFont(_SMALL)
            Lcd.setTextColor(_RED, _BG)
            Lcd.setCursor(_PAD, _HDR_H + 70)
            Lcd.print(_truncate(repr(e), 40))


def _draw_mouse_body(neutral, ax, ay, dx, dy, buttons):
    Lcd.fillRect(0, _HDR_H + 1, Lcd.width(),
                 Lcd.height() - _HDR_H - 1 - _HINT_H, _BG)
    Lcd.setFont(_SMALL)
    Lcd.setTextColor(_BLUE, _BG)
    Lcd.setCursor(_PAD, _HDR_H + 4)
    Lcd.print("tilt  ax {:+.2f}  ay {:+.2f}".format(ax, ay))
    Lcd.setCursor(_PAD, _HDR_H + 18)
    Lcd.print("zero  ax {:+.2f}  ay {:+.2f}".format(*neutral))
    Lcd.setTextColor(_OK if (dx or dy) else _DIM, _BG)
    Lcd.setCursor(_PAD, _HDR_H + 32)
    Lcd.print("dx {:+3d}  dy {:+3d}".format(dx, dy))
    Lcd.setFont(_FONT)
    Lcd.setTextColor(_RED if buttons & 1 else _DIM, _BG)
    Lcd.setCursor(_PAD, _HDR_H + 50)
    Lcd.print("L")
    Lcd.setTextColor(_RED if buttons & 4 else _DIM, _BG)
    Lcd.setCursor(_PAD + 22, _HDR_H + 50)
    Lcd.print("M")
    Lcd.setTextColor(_RED if buttons & 2 else _DIM, _BG)
    Lcd.setCursor(_PAD + 44, _HDR_H + 50)
    Lcd.print("R")


def _capture_neutral(i2c, accel_scale, samples=20):
    """Average accel over a few reads → neutral pose to subtract from
    every subsequent reading."""
    sx = sy = 0.0
    for _ in range(samples):
        ax, ay, _az = _read_accel(i2c, accel_scale)
        sx += ax; sy += ay
        time.sleep_ms(10)
    return (sx / samples, sy / samples)


def _mouse_mode(kb_in, hid, i2c, accel_scale, gyro_scale):
    Lcd.clear(_BG)
    _draw_header("Mouse  calibrating...", color=_BLUE)
    _draw_hint("hold still for 1 second")
    neutral = _capture_neutral(i2c, accel_scale, samples=50)

    _draw_header("Mouse  tilt to move", color=_OK)
    _draw_hint("Spc=L  Ent=R  M=mid  D=drag  W/S=scroll")

    held = 0          # held button bits (for drag — toggle with `D`)
    last_draw = 0
    last_send = 0
    SEND_MS = 30
    accum = [0.0, 0.0]
    still_win = []
    still_since = -1

    while True:
        scroll = 0
        click_bits = 0    # one-shot click this iteration
        send_now = False
        k = kb_in.get_key()
        if k is not None:
            send_now = True
            if k == KeyCode.KEYCODE_ESC:
                if held:
                    hid.send_mouse_report(0, 0, 0, 0)
                return
            elif k == KeyCode.KEYCODE_SPACE:
                click_bits |= 0x01     # left click (press+release)
            elif k == KeyCode.KEYCODE_ENTER:
                click_bits |= 0x02     # right click (press+release)
            elif isinstance(k, int) and (k == ord("m") or k == ord("M")):
                click_bits |= 0x04     # middle click (press+release)
            elif isinstance(k, int) and (k == ord("d") or k == ord("D")):
                held ^= 0x01           # toggle drag-hold of left button
            elif isinstance(k, int) and (k == ord("c") or k == ord("C")):
                neutral = _capture_neutral(i2c, accel_scale, samples=20)
                accum[0] = accum[1] = 0.0
                still_win = []
                send_now = False
            elif isinstance(k, int) and (k == ord("w") or k == ord("W")):
                scroll = 1
            elif isinstance(k, int) and (k == ord("s") or k == ord("S")):
                scroll = -1
            else:
                send_now = False

        # Inline a full click cycle: press → 15ms → release. Cheap, always
        # works regardless of when the next motion-driven report fires.
        if click_bits and hid.is_connected():
            hid.send_mouse_report(held | click_bits, 0, 0, 0)
            time.sleep_ms(15)
            hid.send_mouse_report(held, 0, 0, 0)
            click_bits = 0

        try:
            ax, ay, _az = _read_accel(i2c, accel_scale)
        except Exception:
            ax = ay = 0.0

        # Continuous neutral re-cal: maintain a rolling stillness window
        now = time.ticks_ms()
        still_win.append((ax, ay))
        if len(still_win) > _STILL_WIN:
            still_win.pop(0)
        if len(still_win) == _STILL_WIN:
            xmin = ymin = 1e9
            xmax = ymax = -1e9
            for sx, sy in still_win:
                if sx < xmin: xmin = sx
                if sx > xmax: xmax = sx
                if sy < ymin: ymin = sy
                if sy > ymax: ymax = sy
            if (xmax - xmin) < _STILL_RANGE and (ymax - ymin) < _STILL_RANGE:
                if still_since < 0:
                    still_since = now
                elif now - still_since >= _STILL_HOLD_MS:
                    sx_sum = sy_sum = 0.0
                    for sx, sy in still_win:
                        sx_sum += sx; sy_sum += sy
                    neutral = (sx_sum / _STILL_WIN, sy_sum / _STILL_WIN)
                    accum[0] = accum[1] = 0.0
                    still_since = now  # don't recal again until next still period
            else:
                still_since = -1

        dx, dy = _tilt_to_cursor(ax, ay, neutral[0], neutral[1], accum)

        if hid.is_connected() and time.ticks_diff(now, last_send) >= SEND_MS:
            if dx or dy or scroll or send_now:
                hid.send_mouse_report(held, dx, dy, scroll)
                last_send = now

        if time.ticks_diff(now, last_draw) > 100:
            _draw_mouse_body(neutral, ax, ay, dx, dy, held)
            last_draw = now

        time.sleep_ms(10)


# ---------------- Top-level ----------------

_status_msg = None
_status_color = _OK
_status_until = 0


def _set_status(text, color):
    global _status_msg, _status_color, _status_until
    _status_msg = text
    _status_color = color
    _status_until = time.ticks_add(time.ticks_ms(), 1500)


def _refresh_status():
    global _status_msg
    if _status_msg is None:
        return False
    if time.ticks_diff(_status_until, time.ticks_ms()) > 0:
        Lcd.fillRect(0, _HDR_H + 1, Lcd.width(), 16, _BG)
        Lcd.setFont(_SMALL)
        Lcd.setTextColor(_status_color, _BG)
        Lcd.setCursor(_PAD, _HDR_H + 4)
        Lcd.print(_truncate(_status_msg, 40))
        return True
    Lcd.fillRect(0, _HDR_H + 1, Lcd.width(), 16, _BG)
    _status_msg = None
    return True


def _spk_off():
    """Same ES8311 power-down as launcher / morse — BLE init can leave the
    codec in a noisy state, so kill it when we boot the app."""
    try:
        i2c = I2C(1, sda=Pin(8), scl=Pin(9), freq=400000)
        for reg, val in (
            (0x32, 0x00), (0x12, 0x02), (0x13, 0x10),
            (0x0E, 0xFF), (0x14, 0x00), (0x0D, 0xFA),
            (0x37, 0x08), (0x00, 0x00),
        ):
            i2c.writeto_mem(0x18, reg, bytes([val]))
    except Exception:
        pass


def run():
    Lcd.clear(_BG)
    kb_in = MatrixKeyboard()
    _spk_off()

    # Init IMU once at app start so mouse mode is instant later
    _draw_header("BLE HID  init IMU...")
    try:
        i2c = I2C(1, sda=Pin(8), scl=Pin(9), freq=400_000)
        accel_scale, gyro_scale = _bmi_init(i2c)
    except Exception:
        # Mouse won't work but keyboard still can
        accel_scale = None
        gyro_scale = None
        i2c = None

    _draw_header("BLE HID  starting BLE...")
    _draw_hint("waiting for host to pair")
    try:
        hid = BLEHID("Cardputer-Adv")
        hid.start_advertising()
    except Exception as e:
        _draw_header("BLE init error", color=_RED)
        Lcd.setFont(_SMALL)
        Lcd.setTextColor(_RED, _BG)
        Lcd.setCursor(_PAD, _HDR_H + 6)
        Lcd.print(_truncate(repr(e), 40))
        while kb_in.get_key() != KeyCode.KEYCODE_ESC:
            time.sleep_ms(40)
        return

    idx = 0
    scroll = 0

    def header_text():
        return ("BLE: connected" if hid.is_connected()
                else "BLE: advertising 'Cardputer-Adv'")

    last_conn = hid.is_connected()
    _draw_macro_list(idx, scroll, header_text())
    _draw_hint("Enter=pick  ^v=move  ESC=quit")

    while True:
        cur_conn = hid.is_connected()
        if cur_conn != last_conn:
            last_conn = cur_conn
            _draw_header(header_text(),
                         color=_OK if cur_conn else _BLUE)

        if _refresh_status():
            pass

        k = kb_in.get_key()
        if k is None:
            time.sleep_ms(30)
            continue
        if k == KeyCode.KEYCODE_ESC:
            hid.shutdown()
            return
        if k == KeyCode.KEYCODE_UP:
            idx = (idx - 1) % len(_MACROS)
            if idx < scroll: scroll = idx
            if idx >= scroll + _VISIBLE: scroll = idx - _VISIBLE + 1
            _draw_macro_list(idx, scroll, header_text())
        elif k == KeyCode.KEYCODE_DOWN:
            idx = (idx + 1) % len(_MACROS)
            if idx < scroll: scroll = idx
            if idx >= scroll + _VISIBLE: scroll = idx - _VISIBLE + 1
            _draw_macro_list(idx, scroll, header_text())
        elif k == KeyCode.KEYCODE_ENTER:
            name, fn = _MACROS[idx]
            if not hid.is_connected():
                _set_status("not connected — pair first", _RED)
                continue
            if fn is _LIVE_TYPING:
                _live_typing_mode(kb_in, hid)
                Lcd.clear(_BG)
                _draw_macro_list(idx, scroll, header_text())
                _draw_hint("Enter=pick  ^v=move  ESC=quit")
            elif fn is _MOUSE_MODE:
                if i2c is None:
                    _set_status("IMU not available", _RED)
                    continue
                _mouse_mode(kb_in, hid, i2c, accel_scale, gyro_scale)
                Lcd.clear(_BG)
                _draw_macro_list(idx, scroll, header_text())
                _draw_hint("Enter=pick  ^v=move  ESC=quit")
            elif fn is _COMBO_MODE:
                if i2c is None:
                    _set_status("IMU not available", _RED)
                    continue
                # Lazy-import combo mode. Living in its own module keeps
                # app.py small enough that BLE init doesn't hard-fault on
                # this firmware (when the function lived inline, having
                # all its nested closures parsed at app-import time
                # corrupted enough state that `BLE().active(True)` later
                # crashed the chip — see CLAUDE.md note on bthid combo).
                import sys as _sys
                _combo = _sys.modules.get("_combo")
                if _combo is None:
                    import _combo as _combo
                _combo.run(kb_in, hid, i2c, accel_scale,
                           _sys.modules[__name__])
                Lcd.clear(_BG)
                _draw_macro_list(idx, scroll, header_text())
                _draw_hint("Enter=pick  ^v=move  ESC=quit")
            else:
                try:
                    fn(hid)
                    _set_status("sent: " + _truncate(name, 30), _OK)
                except Exception as e:
                    _set_status(_truncate(repr(e), 40), _RED)
