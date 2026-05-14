import gc
import random
import struct
import time

import M5
from M5 import Lcd
from machine import I2C, Pin
from hardware.matrix_keyboard import MatrixKeyboard
from startup.cardputeradv.framework import KeyCode

# ---------------------------------------------------------------------------
# Screen geometry
# ---------------------------------------------------------------------------
_W = 240
_H = 135
_HUD_H = 14          # top strip: lives | specials | score
_GAME_TOP = _HUD_H + 1
_GAME_H = _H - _GAME_TOP

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------
_BG       = 0x000000
_HUD_BG   = 0x0A0A18
_STAR     = 0x303050
_PLAYER   = 0x40C8FF
_BULLET   = 0xFFFF40
_SPREAD   = 0xFF8020
_ENEMY    = 0xFF4040
_EBULLET  = 0xFF6060
_EXPLO    = 0xFF8800
_TEXT     = 0xFFFFFF
_DIM      = 0x666688
_LIVES_C  = 0xFF6464
_SPEC_C   = 0x80FF80
_LOSE     = 0xFF3333
_HIT_C    = 0xFFFFFF

_FONT_S = M5.Lcd.FONTS.DejaVu12
_FONT_M = M5.Lcd.FONTS.DejaVu18
_FONT_L = M5.Lcd.FONTS.DejaVu24

# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------
_VOL         = 70
_SND_SHOOT   = (1400, 6)    # (freq_hz, ms) — very short click
_SND_EXPLODE = (300,  20)
_SND_HIT     = (150,  80)
_SND_GAMEOVER= [(400, 200), (300, 200), (200, 300)]

# ---------------------------------------------------------------------------
# Game constants
# ---------------------------------------------------------------------------
_LIVES_START   = 3
_SPECIALS_START = 3
_FIRE_PERIOD   = 8
_SPREAD_FRAMES = 180
_ENEMY_MAX     = 8
_PBULLET_MAX   = 6
_EBULLET_MAX   = 6
_ENEMY_SPEED   = 1.2
_PBULLET_SPEED = 7
_EBULLET_SPEED = 2.2
_FRAME_MS      = 33
_INVINCIBLE_F  = 60
_GC_EVERY      = 90

_SHP_W  = 11
_SHP_H  = 9
_SHP_HW = _SHP_W // 2

# ---------------------------------------------------------------------------
# BMI270 raw I/O
# ---------------------------------------------------------------------------
_BMI270_ADDR       = 0x69
_REG_CHIP_ID       = 0x00
_REG_INTERNAL_STATUS = 0x21
_REG_ACC_X_LSB     = 0x0C
_REG_ACC_RANGE     = 0x41
_G                 = 9.80665
_LSB_PER_G         = (16384, 8192, 4096, 2048)
_acc_scale         = _G / 16384.0


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
    raw = i2c.readfrom_mem(_BMI270_ADDR, _REG_ACC_X_LSB, 2)
    return struct.unpack("<h", raw)[0] * _acc_scale


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------
def _snd(freq, ms):
    try:
        M5.Speaker.tone(freq, ms)
    except Exception:
        pass


def _silence_codec(i2c):
    try:
        M5.Speaker.stop()
    except Exception:
        pass
    try:
        M5.Speaker.end()
    except Exception:
        pass
    try:
        for reg, val in (
            (0x32, 0x00), (0x12, 0x02), (0x13, 0x10), (0x0E, 0xFF),
            (0x14, 0x00), (0x0D, 0xFA), (0x37, 0x08), (0x00, 0x00),
        ):
            i2c.writeto_mem(0x18, reg, bytes([val]))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------
def _draw_hud(lives, specials, score):
    Lcd.fillRect(0, 0, _W, _HUD_H, _HUD_BG)
    Lcd.setFont(_FONT_S)
    Lcd.setTextColor(_LIVES_C, _HUD_BG)
    Lcd.setCursor(2, 1)
    Lcd.print("HP:" + str(lives))
    Lcd.setTextColor(_SPEC_C, _HUD_BG)
    sp = "B:" + str(specials)
    sw = Lcd.textWidth(sp, _FONT_S)
    Lcd.setCursor((_W - sw) // 2, 1)
    Lcd.print(sp)
    Lcd.setTextColor(_TEXT, _HUD_BG)
    sc = str(score)
    scw = Lcd.textWidth(sc, _FONT_S)
    Lcd.setCursor(_W - scw - 2, 1)
    Lcd.print(sc)
    Lcd.drawLine(0, _HUD_H, _W, _HUD_H, _DIM)


def _draw_player(px, py, color):
    cx = int(px); ty = int(py); by = ty + _SHP_H
    Lcd.fillTriangle(cx, ty, cx - _SHP_HW, by, cx + _SHP_HW, by, color)
    Lcd.fillRect(cx - 1, by, 3, 2, _SPREAD if color != _BG else _BG)


def _erase_player(px, py):
    cx = int(px); ty = int(py)
    Lcd.fillRect(cx - _SHP_HW - 1, ty - 1, _SHP_W + 2, _SHP_H + 4, _BG)


def _draw_pbullet(bx, by, color):
    Lcd.fillRect(int(bx) - 1, int(by) - 3, 2, 6, color)


def _draw_ebullet(bx, by, color):
    Lcd.fillRect(int(bx) - 1, int(by) - 3, 3, 6, color)


def _draw_enemy(ex, ey, color):
    cx = int(ex); cy = int(ey)
    Lcd.fillTriangle(cx, cy - 4, cx - 4, cy, cx + 4, cy, color)
    Lcd.fillTriangle(cx, cy + 4, cx - 4, cy, cx + 4, cy, color)


def _draw_explosion(ex, ey, frame):
    r = frame + 2
    c = _EXPLO if frame < 3 else _DIM
    cx = int(ex); cy = int(ey)
    Lcd.fillCircle(cx, cy, r, c)


def _erase_explosion(ex, ey):
    cx = int(ex); cy = int(ey)
    Lcd.fillRect(cx - 8, cy - 8, 17, 17, _BG)


def _draw_stars(stars):
    for sx, sy in stars:
        Lcd.drawPixel(sx, sy, _STAR)


def _erase_stars(stars):
    for sx, sy in stars:
        Lcd.drawPixel(sx, sy, _BG)


# ---------------------------------------------------------------------------
# Object pools
# ---------------------------------------------------------------------------
def _make_pbullet_pool():
    return [{"alive": False, "x": 0.0, "y": 0.0, "dx": 0.0} for _ in range(_PBULLET_MAX)]


def _make_ebullet_pool():
    # vx/vy = velocity components (px per frame). Aimed at player at
    # spawn time so the player can't sit directly below an enemy and
    # camp — they have to strafe.
    return [{"alive": False, "x": 0.0, "y": 0.0,
             "vx": 0.0, "vy": _EBULLET_SPEED}
            for _ in range(_EBULLET_MAX)]


def _make_enemy_pool():
    return [
        {"alive": False, "x": 0.0, "y": 0.0, "hp": 1,
         "shoot_cd": 0, "shoot_every": 80}
        for _ in range(_ENEMY_MAX)
    ]


def _make_explosion_pool():
    return [{"alive": False, "x": 0.0, "y": 0.0, "frame": 0} for _ in range(8)]


def _new_game():
    stars = [(random.randint(0, _W - 1), random.randint(_GAME_TOP, _H - 1)) for _ in range(20)]
    return {
        "px":       float(_W // 2),
        "py":       float(_H - _SHP_H - 4),
        "lives":    _LIVES_START,
        "specials": _SPECIALS_START,
        "score":    0,
        "lost":     False,
        "fire_cd":  0,
        "spread_f": 0,
        "invincible": 0,
        "frame":    0,
        "spawn_cd": 30,
        "wave":     0,
        "pbullets": _make_pbullet_pool(),
        "ebullets": _make_ebullet_pool(),
        "enemies":  _make_enemy_pool(),
        "explosions": _make_explosion_pool(),
        "stars":    stars,
        "star_timer": 0,
    }


def _spawn_pbullet(pool, x, y, dx):
    for b in pool:
        if not b["alive"]:
            b["alive"] = True
            b["x"] = x; b["y"] = float(y); b["dx"] = dx
            return True
    return False


def _spawn_ebullet(pool, x, y, target_x=None, target_y=None):
    """Spawn an enemy bullet. If target is given, the bullet is aimed at
    (target_x, target_y) at spawn time (not predictive — just current
    player position). Otherwise it falls straight down (legacy fallback)."""
    for b in pool:
        if not b["alive"]:
            b["alive"] = True
            b["x"] = float(x); b["y"] = float(y)
            if target_x is not None:
                dx = target_x - x
                dy = target_y - y
                # Normalize to fixed speed magnitude
                dist = (dx * dx + dy * dy) ** 0.5
                if dist < 1.0:
                    dist = 1.0
                b["vx"] = dx / dist * _EBULLET_SPEED
                b["vy"] = dy / dist * _EBULLET_SPEED
                # Clamp downward — never let a bullet shoot UP at the player
                # from below (looks weird and the player can't see it coming)
                if b["vy"] < 0.5:
                    b["vy"] = 0.5
            else:
                b["vx"] = 0.0
                b["vy"] = _EBULLET_SPEED
            return True
    return False


def _spawn_enemy(pool, x, y, hp=1, shoot_every=80):
    for e in pool:
        if not e["alive"]:
            e["alive"] = True
            e["x"] = float(x); e["y"] = float(y)
            e["hp"] = hp
            e["shoot_cd"] = random.randint(20, shoot_every)
            e["shoot_every"] = shoot_every
            return True
    return False


def _spawn_explosion(pool, x, y):
    for ex in pool:
        if not ex["alive"]:
            ex["alive"] = True
            ex["x"] = float(x); ex["y"] = float(y)
            ex["frame"] = 0
            return


def _spawn_wave(state):
    w = state["wave"]
    enemies = state["enemies"]
    if w > 0 and w % 5 == 0:
        mx = random.randint(20, _W - 20)
        _spawn_enemy(enemies, mx, _GAME_TOP + 4, hp=2, shoot_every=50)
    count = 1 if w < 5 else 2
    for _ in range(count):
        ex = random.randint(10, _W - 10)
        _spawn_enemy(enemies, ex, _GAME_TOP + 4,
                     hp=1, shoot_every=max(30, 80 - w * 3))
    state["wave"] += 1
    state["spawn_cd"] = max(40, 90 - w * 2)


def _overlap(ax, ay, aw, ah, bx, by, bw, bh):
    return (ax < bx + bw and ax + aw > bx and
            ay < by + bh and ay + ah > by)


# ---------------------------------------------------------------------------
# Per-frame tick
# ---------------------------------------------------------------------------
def _tick(state, ax, spread_key):
    s = state
    frame = s["frame"]

    norm = -ax / 6.0
    if norm > 1.0: norm = 1.0
    if norm < -1.0: norm = -1.0
    step = norm * 6.0
    new_px = s["px"] + step
    if new_px < _SHP_HW + 1: new_px = float(_SHP_HW + 1)
    if new_px > _W - _SHP_HW - 1: new_px = float(_W - _SHP_HW - 1)
    old_px = s["px"]; old_py = s["py"]
    s["px"] = new_px

    if spread_key and s["specials"] > 0 and s["spread_f"] == 0:
        s["specials"] -= 1
        s["spread_f"] = _SPREAD_FRAMES
        return_hud = True
    else:
        return_hud = False
    if s["spread_f"] > 0:
        s["spread_f"] -= 1

    s["fire_cd"] -= 1
    if s["fire_cd"] <= 0:
        s["fire_cd"] = _FIRE_PERIOD
        bx = s["px"]; by = s["py"] - 1
        if s["spread_f"] > 0:
            for dx in (-3.0, -1.5, 0.0, 1.5, 3.0):
                _spawn_pbullet(s["pbullets"], bx, by, dx)
        else:
            _spawn_pbullet(s["pbullets"], bx, by, 0.0)

    for b in s["pbullets"]:
        if not b["alive"]: continue
        _draw_pbullet(b["x"], b["y"], _BG)
        b["y"] -= _PBULLET_SPEED
        b["x"] += b["dx"]
        if b["y"] < _GAME_TOP or b["x"] < 0 or b["x"] >= _W:
            b["alive"] = False

    for b in s["ebullets"]:
        if not b["alive"]: continue
        _draw_ebullet(b["x"], b["y"], _BG)
        b["x"] += b["vx"]
        b["y"] += b["vy"]
        if b["y"] > _H or b["x"] < -4 or b["x"] > _W + 4:
            b["alive"] = False

    for e in s["enemies"]:
        if not e["alive"]: continue
        _draw_enemy(e["x"], e["y"], _BG)
        e["y"] += _ENEMY_SPEED
        if e["y"] > _H + 8:
            e["alive"] = False
            continue
        e["shoot_cd"] -= 1
        if e["shoot_cd"] <= 0:
            e["shoot_cd"] = e["shoot_every"] + random.randint(0, 20)
            # Aim at player's current position so the player has to dodge
            # by strafing — Raiden classic-arcade behavior. HP-2 boss
            # enemies fire a 3-way spread (centre + ±15° offset).
            tx, ty = s["px"], s["py"]
            if e["hp"] >= 2:
                # Spread shot
                _spawn_ebullet(s["ebullets"], e["x"], e["y"] + 5, tx, ty)
                _spawn_ebullet(s["ebullets"], e["x"], e["y"] + 5,
                               tx - 30, ty)
                _spawn_ebullet(s["ebullets"], e["x"], e["y"] + 5,
                               tx + 30, ty)
            else:
                _spawn_ebullet(s["ebullets"], e["x"], e["y"] + 5, tx, ty)

    for ex in s["explosions"]:
        if not ex["alive"]: continue
        _erase_explosion(ex["x"], ex["y"])
        ex["frame"] += 1
        if ex["frame"] > 5:
            ex["alive"] = False

    score_delta = 0
    for b in s["pbullets"]:
        if not b["alive"]: continue
        bx = int(b["x"]); by2 = int(b["y"])
        for e in s["enemies"]:
            if not e["alive"]: continue
            if _overlap(bx - 1, by2 - 3, 2, 6, int(e["x"]) - 4, int(e["y"]) - 4, 8, 8):
                b["alive"] = False
                e["hp"] -= 1
                if e["hp"] <= 0:
                    e["alive"] = False
                    _spawn_explosion(s["explosions"], e["x"], e["y"])
                    score_delta += 10
                    _snd(*_SND_EXPLODE)
                break

    hit_player = False
    if s["invincible"] == 0:
        px = int(s["px"]); py = int(s["py"])
        for b in s["ebullets"]:
            if not b["alive"]: continue
            if _overlap(int(b["x"]) - 1, int(b["y"]) - 3, 3, 6,
                        px - _SHP_HW, py, _SHP_W, _SHP_H):
                b["alive"] = False
                hit_player = True
                break
        if not hit_player:
            for e in s["enemies"]:
                if not e["alive"]: continue
                if _overlap(int(e["x"]) - 4, int(e["y"]) - 4, 8, 8,
                            px - _SHP_HW, py, _SHP_W, _SHP_H):
                    e["alive"] = False
                    _spawn_explosion(s["explosions"], e["x"], e["y"])
                    score_delta += 5
                    hit_player = True
                    break

    if hit_player:
        s["lives"] -= 1
        s["invincible"] = _INVINCIBLE_F
        _snd(*_SND_HIT)
        if s["lives"] <= 0:
            s["lost"] = True
        return_hud = True
    if s["invincible"] > 0:
        s["invincible"] -= 1

    if score_delta:
        s["score"] += score_delta
        return_hud = True

    s["spawn_cd"] -= 1
    if s["spawn_cd"] <= 0:
        _spawn_wave(s)

    s["star_timer"] += 1
    if s["star_timer"] >= 3:
        s["star_timer"] = 0
        _erase_stars(s["stars"])
        new_stars = []
        for sx, sy in s["stars"]:
            sy += 1
            if sy >= _H:
                sy = _GAME_TOP
                sx = random.randint(0, _W - 1)
            new_stars.append((sx, sy))
        s["stars"] = new_stars
        _draw_stars(s["stars"])

    for ex in s["explosions"]:
        if ex["alive"]:
            _draw_explosion(ex["x"], ex["y"], ex["frame"])

    for e in s["enemies"]:
        if e["alive"]:
            _draw_enemy(e["x"], e["y"], _ENEMY)

    bcolor = _SPREAD if s["spread_f"] > 0 else _BULLET
    for b in s["pbullets"]:
        if b["alive"]:
            _draw_pbullet(b["x"], b["y"], bcolor)

    for b in s["ebullets"]:
        if b["alive"]:
            _draw_ebullet(b["x"], b["y"], _EBULLET)

    old_pi = int(old_px); new_pi = int(new_px)
    if old_pi != new_pi:
        _erase_player(old_px, old_py)
    if s["invincible"] == 0 or (s["invincible"] % 6 < 3):
        _draw_player(s["px"], s["py"], _PLAYER)

    if return_hud:
        _draw_hud(s["lives"], s["specials"], s["score"])

    s["frame"] = frame + 1


def _show_game_over(score):
    bx = 30; bw = _W - 60
    by = 28; bh = 80
    Lcd.fillRoundRect(bx, by, bw, bh, 5, 0x080818)
    Lcd.drawRoundRect(bx, by, bw, bh, 5, _LOSE)
    Lcd.setFont(_FONT_L)
    Lcd.setTextColor(_LOSE, 0x080818)
    msg = "GAME OVER"
    mw = Lcd.textWidth(msg, _FONT_L)
    Lcd.setCursor((_W - mw) // 2, by + 6)
    Lcd.print(msg)
    Lcd.setFont(_FONT_M)
    Lcd.setTextColor(_TEXT, 0x080818)
    sc = str(score)
    sw = Lcd.textWidth(sc, _FONT_M)
    Lcd.setCursor((_W - sw) // 2, by + 38)
    Lcd.print(sc)
    Lcd.setFont(_FONT_S)
    Lcd.setTextColor(_DIM, 0x080818)
    hint = "ENTER restart  ESC quit"
    hw = Lcd.textWidth(hint, _FONT_S)
    Lcd.setCursor((_W - hw) // 2, by + 64)
    Lcd.print(hint)


def run():
    try:
        M5.Speaker.begin()
        M5.Speaker.setVolume(_VOL)
    except Exception:
        pass

    Lcd.clear(_BG)
    Lcd.setFont(_FONT_S)
    Lcd.setTextColor(_DIM, _BG)
    Lcd.setCursor(4, _GAME_TOP + 4)
    Lcd.print("Init IMU...")

    gc.collect()
    i2c = I2C(1, sda=Pin(8), scl=Pin(9), freq=400_000)
    try:
        _bmi_init(i2c)
    except Exception as e:
        Lcd.fillRect(0, _GAME_TOP, _W, _GAME_H, _BG)
        Lcd.setTextColor(_LOSE, _BG)
        Lcd.setCursor(4, _GAME_TOP + 4)
        Lcd.print("IMU err: " + repr(e)[:28])
        Lcd.setTextColor(_DIM, _BG)
        Lcd.setCursor(4, _H - 14)
        Lcd.print("ESC = back")
        kb = MatrixKeyboard()
        while True:
            if kb.get_key() == KeyCode.KEYCODE_ESC:
                _silence_codec(i2c)
                return
            time.sleep_ms(50)

    kb = MatrixKeyboard()
    state = _new_game()
    Lcd.clear(_BG)
    _draw_hud(state["lives"], state["specials"], state["score"])
    _draw_stars(state["stars"])
    _draw_player(state["px"], state["py"], _PLAYER)
    gc.collect()

    last_gc = 0

    while True:
        frame_start = time.ticks_ms()
        try: M5.update()
        except Exception: pass

        spread_key = False
        restart = False
        do_exit = False
        while True:
            k = kb.get_key()
            if k is None: break
            if k == KeyCode.KEYCODE_ESC:
                do_exit = True; break
            if state["lost"]:
                if k == KeyCode.KEYCODE_ENTER:
                    restart = True
            else:
                if k == ord(" ") or k == KeyCode.KEYCODE_ENTER:
                    spread_key = True

        if do_exit:
            _silence_codec(i2c)
            return

        if restart:
            state = _new_game()
            Lcd.clear(_BG)
            _draw_hud(state["lives"], state["specials"], state["score"])
            _draw_stars(state["stars"])
            _draw_player(state["px"], state["py"], _PLAYER)
            gc.collect()
            continue

        if state["lost"]:
            time.sleep_ms(_FRAME_MS)
            continue

        try: ax = _read_ax(i2c)
        except Exception: ax = 0.0

        _tick(state, ax, spread_key)

        if state["lost"]:
            for freq, ms in _SND_GAMEOVER:
                _snd(freq, ms)
                time.sleep_ms(ms + 30)
            _erase_player(state["px"], state["py"])
            _spawn_explosion(state["explosions"], state["px"], state["py"])
            _show_game_over(state["score"])

        last_gc += 1
        if last_gc >= _GC_EVERY:
            last_gc = 0
            gc.collect()

        elapsed = time.ticks_diff(time.ticks_ms(), frame_start)
        if elapsed < _FRAME_MS:
            time.sleep_ms(_FRAME_MS - elapsed)
