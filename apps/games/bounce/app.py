import gc
import struct
import time

import M5
from M5 import Lcd
from machine import I2C, Pin
from hardware.matrix_keyboard import MatrixKeyboard
from startup.cardputeradv.framework import KeyCode

# --- Geometry ---------------------------------------------------------------
_W = 240
_H = 135
_TITLE_H = 14
_AREA_TOP = _TITLE_H + 1
_BALL_R = 5
_PADDLE_W = 60
_PADDLE_H = 6
_PADDLE_Y = _H - _PADDLE_H - 2

# Audio
_HIT_FREQ = 880
_HIT_MS = 35
_VOLUME = 80   # 0..255

# --- Colors -----------------------------------------------------------------
_BG = 0x000000
_FRAME = 0x404048
_BALL = 0xFFD040
_PADDLE = 0x64A0FF
_TEXT = 0xFFFFFF
_DIM = 0x666666
_LOSE = 0xFF4444
_WIN = 0x00DD66

_FONT_S = M5.Lcd.FONTS.DejaVu12
_FONT_M = M5.Lcd.FONTS.DejaVu18
_FONT_L = M5.Lcd.FONTS.DejaVu24

# --- BMI270 raw I/O (no driver in hot path) ---------------------------------
_BMI270_ADDR = 0x69
_REG_CHIP_ID = 0x00
_REG_INTERNAL_STATUS = 0x21
_REG_ACC_X_LSB = 0x0C
_REG_ACC_RANGE = 0x41
_G = 9.80665
_LSB_PER_G = (16384, 8192, 4096, 2048)
_acc_scale = _G / 16384.0


def _bmi_init(i2c):
    global _acc_scale
    chip = i2c.readfrom_mem(_BMI270_ADDR, _REG_CHIP_ID, 1)[0]
    if chip != 0x24:
        raise OSError("BMI270 chip_id 0x{:02x}".format(chip))
    status = i2c.readfrom_mem(_BMI270_ADDR, _REG_INTERNAL_STATUS, 1)[0]
    if (status & 0x01) == 0:
        from micropython_bmi270 import bmi270
        bmi270.BMI270(i2c, address=_BMI270_ADDR)
    rng = i2c.readfrom_mem(_BMI270_ADDR, _REG_ACC_RANGE, 1)[0] & 0x03
    _acc_scale = _G / _LSB_PER_G[rng]


def _read_ax(i2c):
    """Just X-axis: 2-byte burst, minimal I2C traffic."""
    raw = i2c.readfrom_mem(_BMI270_ADDR, _REG_ACC_X_LSB, 2)
    return struct.unpack("<h", raw)[0] * _acc_scale


# --- Drawing helpers --------------------------------------------------------

def _draw_frame_and_title(score):
    Lcd.fillRect(0, 0, _W, _TITLE_H, _BG)
    Lcd.setFont(_FONT_S)
    Lcd.setTextColor(_DIM, _BG)
    Lcd.setCursor(4, 1)
    Lcd.print("Bounce")
    Lcd.setTextColor(_TEXT, _BG)
    s = "Score {}".format(score)
    sw = Lcd.textWidth(s, _FONT_S)
    Lcd.setCursor(_W - sw - 4, 1)
    Lcd.print(s)
    Lcd.drawLine(0, _TITLE_H, _W, _TITLE_H, _FRAME)


def _erase_ball(x, y):
    Lcd.fillRect(int(x) - _BALL_R - 1, int(y) - _BALL_R - 1,
                 2 * _BALL_R + 2, 2 * _BALL_R + 2, _BG)


def _draw_ball(x, y):
    Lcd.fillCircle(int(x), int(y), _BALL_R, _BALL)


def _erase_paddle(x):
    px = int(x) - _PADDLE_W // 2 - 1
    Lcd.fillRect(px, _PADDLE_Y - 1, _PADDLE_W + 2, _PADDLE_H + 2, _BG)


def _draw_paddle(x):
    px = int(x) - _PADDLE_W // 2
    Lcd.fillRoundRect(px, _PADDLE_Y, _PADDLE_W, _PADDLE_H, 2, _PADDLE)


def _show_game_over(score, won):
    Lcd.fillRect(0, _AREA_TOP, _W, _H - _AREA_TOP, _BG)
    Lcd.setFont(_FONT_L)
    msg = "WIN!" if won else "GAME OVER"
    color = _WIN if won else _LOSE
    Lcd.setTextColor(color, _BG)
    mw = Lcd.textWidth(msg, _FONT_L)
    Lcd.setCursor((_W - mw) // 2, _AREA_TOP + 14)
    Lcd.print(msg)
    Lcd.setFont(_FONT_M)
    s = "Score {}".format(score)
    Lcd.setTextColor(_TEXT, _BG)
    sw = Lcd.textWidth(s, _FONT_M)
    Lcd.setCursor((_W - sw) // 2, _AREA_TOP + 50)
    Lcd.print(s)
    Lcd.setFont(_FONT_S)
    Lcd.setTextColor(_DIM, _BG)
    h = "ENTER restart   ESC back"
    hw = Lcd.textWidth(h, _FONT_S)
    Lcd.setCursor((_W - hw) // 2, _H - 14)
    Lcd.print(h)


# --- Game -------------------------------------------------------------------

def _new_game():
    return {
        "bx": _W / 2.0,
        "by": _AREA_TOP + 8,
        "vx": 2.8,         # px / frame at ~20 Hz
        "vy": 2.4,
        "px": _W / 2.0,    # paddle center x
        "score": 0,
        "lost": False,
    }


def _tick(state, ax):
    # --- paddle: tilt-driven velocity. ax in m/s²; ~6 m/s² (~37°) = full speed.
    # Sign flipped: tilting LEFT should move the paddle LEFT (chip's +X axis
    # points right when held normally, so positive ax = tilt-right; we want the
    # paddle to track the tilt direction).
    norm = -ax / 6.0
    if norm > 1.0: norm = 1.0
    if norm < -1.0: norm = -1.0
    paddle_step = norm * 7.0          # max 7 px/frame at full tilt
    new_px = state["px"] + paddle_step
    half = _PADDLE_W / 2.0
    if new_px < half: new_px = half
    if new_px > _W - half: new_px = _W - half

    # --- ball
    new_bx = state["bx"] + state["vx"]
    new_by = state["by"] + state["vy"]
    vx = state["vx"]
    vy = state["vy"]

    # walls (left, right, top)
    if new_bx < _BALL_R:
        new_bx = _BALL_R; vx = -vx
    elif new_bx > _W - _BALL_R:
        new_bx = _W - _BALL_R; vx = -vx
    if new_by < _AREA_TOP + _BALL_R:
        new_by = _AREA_TOP + _BALL_R; vy = -vy

    # paddle hit: ball arriving from above onto paddle band
    hit = False
    if vy > 0 and (_PADDLE_Y - _BALL_R) <= new_by <= (_PADDLE_Y + _PADDLE_H):
        if abs(new_bx - new_px) < (_PADDLE_W / 2.0 + _BALL_R):
            new_by = _PADDLE_Y - _BALL_R
            vy = -abs(vy)
            # Add english based on hit position so you can steer the ball.
            offset = (new_bx - new_px) / (_PADDLE_W / 2.0)
            vx += offset * 0.8
            # Cap vx so ball isn't impossibly fast horizontally.
            if vx > 4.0: vx = 4.0
            if vx < -4.0: vx = -4.0
            state["score"] += 1
            hit = True
            # Aggressive ramp: every 3 hits speed *= 1.15. At score=15 the
            # ball is 1.15^5 ≈ 2× initial; at 30 it's ≈ 4×.
            if state["score"] % 3 == 0:
                vy *= 1.15
                vx *= 1.15
            # Cap |vy| so the ball can't tunnel through the paddle in one
            # frame (paddle band is ~6 px tall + 5 px radius = 11 px window).
            if vy > 9.0: vy = 9.0
            if vy < -9.0: vy = -9.0

    state["hit_this_tick"] = hit

    # ball off bottom: lose
    if new_by > _H + _BALL_R:
        state["lost"] = True

    state["bx"] = new_bx
    state["by"] = new_by
    state["vx"] = vx
    state["vy"] = vy
    state["px"] = new_px


def run():
    Lcd.clear(_BG)
    _draw_frame_and_title(0)

    # Speaker init (the launcher's M5.begin() inits hardware; we just enable
    # output and set a comfortable volume).
    try:
        M5.Speaker.begin()
        M5.Speaker.setVolume(_VOLUME)
    except Exception:
        pass

    # IMU init
    Lcd.setFont(_FONT_S)
    Lcd.setTextColor(_DIM, _BG)
    Lcd.setCursor(4, _AREA_TOP + 4)
    Lcd.print("Init IMU...")
    gc.collect()
    # Use I2C peripheral 1 — the same one the keyboard driver uses (see
    # M5 firmware m5stack/libs/hardware/matrix_keyboard.py). Sharing the
    # peripheral keeps the IO mux pointed at it; using I2C(0) on the same
    # pins reroutes the mux and breaks the keyboard until reboot.
    i2c = I2C(1, sda=Pin(8), scl=Pin(9), freq=400_000)
    try:
        _bmi_init(i2c)
    except Exception as e:
        Lcd.fillRect(0, _AREA_TOP, _W, _H - _AREA_TOP, _BG)
        Lcd.setTextColor(_LOSE, _BG)
        Lcd.setCursor(4, _AREA_TOP + 4)
        Lcd.print("IMU: " + repr(e)[:32])
        Lcd.setTextColor(_DIM, _BG)
        Lcd.setCursor(4, _H - 14)
        Lcd.print("ESC = back")
        kb = MatrixKeyboard()
        while True:
            if kb.get_key() == KeyCode.KEYCODE_ESC:
                return
            time.sleep_ms(50)

    Lcd.fillRect(0, _AREA_TOP, _W, _H - _AREA_TOP, _BG)

    kb = MatrixKeyboard()
    state = _new_game()
    _draw_frame_and_title(state["score"])

    last_bx = state["bx"]; last_by = state["by"]
    last_px = state["px"]
    _draw_ball(state["bx"], state["by"])
    _draw_paddle(state["px"])

    last_score = 0
    FRAME_MS = 50

    while True:
        frame_start = time.ticks_ms()

        # Pump the M5 framework once per frame — drives audio playback queue,
        # button state, etc. Keeps the speaker DMA from stalling other IRQs.
        try:
            M5.update()
        except Exception:
            pass

        # Drain ALL queued keys; ESC priority.
        restart = False
        while True:
            k = kb.get_key()
            if k is None:
                break
            if k == KeyCode.KEYCODE_ESC:
                return
            if state["lost"] and k == KeyCode.KEYCODE_ENTER:
                restart = True

        if restart:
            state = _new_game()
            Lcd.fillRect(0, _AREA_TOP, _W, _H - _AREA_TOP, _BG)
            _draw_frame_and_title(0)
            last_bx = state["bx"]; last_by = state["by"]; last_px = state["px"]
            _draw_ball(state["bx"], state["by"])
            _draw_paddle(state["px"])
            last_score = 0

        if not state["lost"]:
            try:
                ax = _read_ax(i2c)
            except Exception:
                ax = 0.0

            _tick(state, ax)

            # Sound on paddle hit (non-blocking).
            if state.get("hit_this_tick"):
                try:
                    M5.Speaker.tone(_HIT_FREQ, _HIT_MS)
                except Exception:
                    pass

            # Erase + redraw moved sprites only (cheaper + flicker-free).
            if int(last_bx) != int(state["bx"]) or int(last_by) != int(state["by"]):
                _erase_ball(last_bx, last_by)
                _draw_ball(state["bx"], state["by"])
                last_bx = state["bx"]; last_by = state["by"]

            if int(last_px) != int(state["px"]):
                _erase_paddle(last_px)
                _draw_paddle(state["px"])
                last_px = state["px"]

            if state["score"] != last_score:
                _draw_frame_and_title(state["score"])
                last_score = state["score"]

            if state["lost"]:
                try:
                    M5.Speaker.stop()
                except Exception:
                    pass
                _show_game_over(state["score"], False)

        # Frame pacing: fixed FRAME_MS so ball physics stays consistent.
        elapsed = time.ticks_diff(time.ticks_ms(), frame_start)
        if elapsed < FRAME_MS:
            time.sleep_ms(FRAME_MS - elapsed)
