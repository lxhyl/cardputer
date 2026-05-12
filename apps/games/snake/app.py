import random
import time

import M5
from M5 import Lcd
from hardware.matrix_keyboard import MatrixKeyboard
from startup.cardputeradv.framework import KeyCode

# Geometry — 240x135 LCD. 14px header, 12px hint, ~109px game area.
_CELL = 7
_HDR_H = 14
_HINT_H = 12
_GAME_TOP = _HDR_H + 1
_GAME_H = 135 - _HDR_H - _HINT_H - 1     # ~108
_GRID_W = 240 // _CELL                    # 34
_GRID_H = _GAME_H // _CELL                # 15
_GAME_BOT = _GAME_TOP + _GRID_H * _CELL

# Colors
_BG = 0x000000
_BORDER = 0x303040
_HDR_BG = 0x232332
_HDR_FG = 0xFFD040
_BODY = 0x00DD66
_HEAD = 0xAAFF99
_FOOD = 0xFF6464
_DIM = 0x666666
_FG = 0xFFFFFF

_FONT_S = M5.Lcd.FONTS.DejaVu12
_FONT_M = M5.Lcd.FONTS.DejaVu18
_FONT_L = M5.Lcd.FONTS.DejaVu24

# Speed: starts gentle, ramps very slowly so the game stays playable.
# tick = max(_TICK_MIN, _TICK_MAX - score * _RAMP)
#   score 0  → 180 ms (~5.5 fps)
#   score 30 → 150 ms
#   score 60 → 120 ms
#   score 80 → 100 ms (floor)
_TICK_MAX = 180
_TICK_MIN = 100
_RAMP = 1   # ms shaved per food eaten


def _cell_xy(gx, gy):
    return gx * _CELL, _GAME_TOP + gy * _CELL


def _draw_cell(gx, gy, color):
    x, y = _cell_xy(gx, gy)
    Lcd.fillRect(x, y, _CELL - 1, _CELL - 1, color)


def _erase_cell(gx, gy):
    x, y = _cell_xy(gx, gy)
    Lcd.fillRect(x, y, _CELL - 1, _CELL - 1, _BG)


def _draw_header(score):
    Lcd.fillRect(0, 0, Lcd.width(), _HDR_H, _HDR_BG)
    Lcd.setFont(_FONT_S)
    Lcd.setTextColor(_HDR_FG, _HDR_BG)
    Lcd.setCursor(4, 1)
    Lcd.print("Snake")
    Lcd.setTextColor(_FG, _HDR_BG)
    s = "Score {}".format(score)
    sw = Lcd.textWidth(s, _FONT_S)
    Lcd.setCursor(Lcd.width() - sw - 4, 1)
    Lcd.print(s)


def _draw_hint(text):
    y = Lcd.height() - _HINT_H
    Lcd.fillRect(0, y, Lcd.width(), _HINT_H, _BG)
    Lcd.setFont(_FONT_S)
    Lcd.setTextColor(_DIM, _BG)
    Lcd.setCursor(4, y)
    Lcd.print(text)


def _spawn_food(snake_set):
    while True:
        f = (random.randint(0, _GRID_W - 1), random.randint(0, _GRID_H - 1))
        if f not in snake_set:
            return f


def _new_game():
    head = (_GRID_W // 2, _GRID_H // 2)
    snake = [head]
    food = _spawn_food(set(snake))
    return {"snake": snake, "dir": (1, 0), "food": food, "score": 0, "lost": False}


def _draw_initial(state):
    Lcd.clear(_BG)
    _draw_header(state["score"])
    Lcd.drawRect(0, _HDR_H, Lcd.width(), _GAME_H + 1, _BORDER)
    for gx, gy in state["snake"]:
        _draw_cell(gx, gy, _BODY)
    fx, fy = state["food"]
    _draw_cell(fx, fy, _FOOD)
    _draw_hint("WASD/arrows  ESC=back")


def _show_game_over(score):
    bx = 22
    bw = Lcd.width() - 44
    by = 38
    bh = 60
    Lcd.fillRoundRect(bx, by, bw, bh, 4, _BG)
    Lcd.drawRoundRect(bx, by, bw, bh, 4, _FOOD)
    Lcd.setFont(_FONT_L)
    Lcd.setTextColor(_FOOD, _BG)
    msg = "GAME OVER"
    mw = Lcd.textWidth(msg, _FONT_L)
    Lcd.setCursor((Lcd.width() - mw) // 2, by + 6)
    Lcd.print(msg)
    Lcd.setFont(_FONT_M)
    Lcd.setTextColor(_FG, _BG)
    s = "Score {}".format(score)
    sw = Lcd.textWidth(s, _FONT_M)
    Lcd.setCursor((Lcd.width() - sw) // 2, by + 36)
    Lcd.print(s)
    _draw_hint("ENTER restart  ESC back")


def run():
    kb = MatrixKeyboard()
    try:
        M5.Speaker.begin()
        M5.Speaker.setVolume(80)
    except Exception:
        pass

    state = _new_game()
    _draw_initial(state)

    pending_dir = state["dir"]
    last_tick = time.ticks_ms()

    while True:
        # Drain input
        while True:
            k = kb.get_key()
            if k is None:
                break
            if k == KeyCode.KEYCODE_ESC:
                try: M5.Speaker.stop()
                except: pass
                return
            if state["lost"]:
                if k == KeyCode.KEYCODE_ENTER:
                    state = _new_game()
                    pending_dir = state["dir"]
                    _draw_initial(state)
                    last_tick = time.ticks_ms()
                continue
            new_dir = None
            # Single-key arrows on the ;/,/./ key cluster (those are the
            # cardputer's arrow positions, but using them as arrows requires
            # the Fn modifier — too cumbersome for a real-time game).
            if k == KeyCode.KEYCODE_UP or k == ord("w") or k == ord(";"):
                new_dir = (0, -1)
            elif k == KeyCode.KEYCODE_DOWN or k == ord("s") or k == ord("."):
                new_dir = (0, 1)
            elif k == KeyCode.KEYCODE_LEFT or k == ord("a") or k == ord(","):
                new_dir = (-1, 0)
            elif k == KeyCode.KEYCODE_RIGHT or k == ord("d") or k == ord("/"):
                new_dir = (1, 0)
            if new_dir is not None:
                cur = state["dir"]
                # Block 180° reversal (would auto-collide with neck)
                if (new_dir[0] != -cur[0]) or (new_dir[1] != -cur[1]):
                    pending_dir = new_dir

        if state["lost"]:
            time.sleep_ms(40)
            continue

        # Pacing
        tick = max(_TICK_MIN, _TICK_MAX - state["score"] * _RAMP)
        if time.ticks_diff(time.ticks_ms(), last_tick) < tick:
            time.sleep_ms(8)
            continue
        last_tick = time.ticks_ms()

        state["dir"] = pending_dir
        body = state["snake"]
        hx, hy = body[-1]
        nx = hx + pending_dir[0]
        ny = hy + pending_dir[1]

        # Wall collision
        if not (0 <= nx < _GRID_W and 0 <= ny < _GRID_H):
            state["lost"] = True
            try: M5.Speaker.tone(220, 220)
            except: pass
            _show_game_over(state["score"])
            continue

        ate = (nx, ny) == state["food"]
        # Self collision: if eating, tail stays so check whole body;
        # if not eating, tail will move out so check body[1:]
        check_set = set(body) if ate else set(body[1:])
        if (nx, ny) in check_set:
            state["lost"] = True
            try: M5.Speaker.tone(220, 220)
            except: pass
            _show_game_over(state["score"])
            continue

        # Move: append head, repaint old head as body, drop tail unless ate.
        body.append((nx, ny))
        _draw_cell(nx, ny, _HEAD)
        if len(body) >= 2:
            _draw_cell(body[-2][0], body[-2][1], _BODY)

        if ate:
            state["score"] += 1
            _draw_header(state["score"])
            try: M5.Speaker.tone(880, 35)
            except: pass
            state["food"] = _spawn_food(set(body))
            _draw_cell(state["food"][0], state["food"][1], _FOOD)
        else:
            tail = body.pop(0)
            _erase_cell(tail[0], tail[1])

        time.sleep_ms(2)
