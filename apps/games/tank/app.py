"""Battle City (坦克大战) for Cardputer-Adv — full-screen 24x12 playfield.

This is a faithful-ish recreation of the NES/Famicom classic. Differences from
the original 13x13:

  * Playfield is 24 columns wide (TILE=10 → 240 px) to use the full Cardputer
    LCD width. Rows stay at 12 (close to the classic 13 — sacrificed one row
    for a 15-px top HUD with score/lives/stage/enemies).
  * Single player only.
  * One enemy type (basic) — 20 per stage. Power-ups omitted.

Why hold-to-move via raw TCA8418 events: the standard `MatrixKeyboard.get_key()`
drops release events (see m5stack/uiflow-micropython
`hardware/keyboard/__init__._ascii_handler`: `keyevent.state and append`).
That makes hold-to-move impossible through the public API. We hook the
`_tick_handler` slot on the underlying `KeyboardI2C` instance so both press AND
release reach us, then restore the original on exit.

Layout:
    HUD (15 px tall): [1P x N] [ST n] [SC nnnnn] [enemy dots]
    Field (240 x 120): 24 x 12 tiles, TILE = 10, TANK = 20.
"""

import gc
import random
import time

import M5
from M5 import Lcd
from machine import I2C, Pin
from hardware.matrix_keyboard import MatrixKeyboard
from startup.cardputeradv.framework import KeyCode

try:
    import apps.games.tank.render as R
except ImportError:
    import games.tank.render as R

# ---------------------------------------------------------------------------
# Tuning
# ---------------------------------------------------------------------------
_PLAYER_SPEED   = 1.4
_ENEMY_SPEED    = 0.9
_BULLET_SPEED   = 3.5
_FIRE_COOLDOWN_F = 8

_FRAME_MS = 33                 # ~30 fps
_GC_EVERY = 30

_LIVES_START   = 3
_ENEMIES_PER_STAGE     = 20
_MAX_ENEMIES_ON_SCREEN = 3

_RESPAWN_F        = 30
_INVINCIBLE_F     = 60
_EXPLOSION_FRAMES = 6

# Audio
_VOL = 65
_SND_FIRE  = (1500, 8)
_SND_BRICK = (600, 12)
_SND_STEEL = (900, 6)
_SND_BOOM  = (200, 60)
_SND_KILL  = (300, 30)
_SND_OVER  = ((500, 200), (380, 200), (260, 250), (180, 350))
_SND_STAGE = ((700, 80), (900, 80), (1100, 120))

_DX = (0, 1, 0, -1)
_DY = (-1, 0, 1, 0)


def _shuffle(lst):
    """Fisher-Yates in place. MicroPython's `random` has no `shuffle`."""
    for i in range(len(lst) - 1, 0, -1):
        j = random.randint(0, i)
        lst[i], lst[j] = lst[j], lst[i]


def _snd(freq, ms):
    try:
        M5.Speaker.tone(freq, ms)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Codec silence — must be called on every exit path (CLAUDE.md).
# ---------------------------------------------------------------------------
_codec_i2c = None


def _codec_bus():
    global _codec_i2c
    if _codec_i2c is None:
        _codec_i2c = I2C(1, sda=Pin(8), scl=Pin(9), freq=400_000)
    return _codec_i2c


def _silence_codec():
    try:
        M5.Speaker.stop()
    except Exception:
        pass
    try:
        M5.Speaker.end()
    except Exception:
        pass
    try:
        i2c = _codec_bus()
        for reg, val in (
            (0x32, 0x00), (0x12, 0x02), (0x13, 0x10), (0x0E, 0xFF),
            (0x14, 0x00), (0x0D, 0xFA), (0x37, 0x08), (0x00, 0x00),
        ):
            i2c.writeto_mem(0x18, reg, bytes([val]))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Stage maps — 24x12. Legend: . empty  B brick  S steel  W water  F forest
#                              E eagle  (player spawns at fixed offset)
# ---------------------------------------------------------------------------
_STAGE_1 = (
    "........................",
    "..BB..BB........BB..BB..",
    "..BB..BB........BB..BB..",
    "......BB........BB......",
    "S.....BB.WW..WW.BB.....S",
    ".........WW..WW.........",
    "S.....BB.WW..WW.BB.....S",
    "......BB........BB......",
    "FF.FF..............FF.FF",
    "..........BBBB..........",
    "..........BEEB..........",
    "..........BEEB..........",
)

_STAGE_2 = (
    "........................",
    ".BB.BB.BB.BB.BB.BB.BB.B.",
    ".BB.BB.BB.BB.BB.BB.BB.B.",
    "........................",
    "SSSS......WWWW......SSSS",
    "..........WWWW..........",
    "....BB.FF........FF.BB..",
    "....BB.FF........FF.BB..",
    "........................",
    "..........BBBB..........",
    "..........BEEB..........",
    "..........BEEB..........",
)

_STAGE_3 = (
    "........................",
    ".S.S.S.S.S.S.S.S.S.S.S.S",
    "...BB.....BBBB.....BB...",
    "...BB.....BBBB.....BB...",
    "......FF........FF......",
    "BB....FF...WW...FF....BB",
    "BB.........WW.........BB",
    "......FF........FF......",
    "...BB.....BBBB.....BB...",
    "..........BBBB..........",
    "..........BEEB..........",
    "..........BEEB..........",
)

_STAGES = (_STAGE_1, _STAGE_2, _STAGE_3)

_PLAYER_SPAWN_TX = 7      # 2x2 tank TL, leaves room left of eagle
_PLAYER_SPAWN_TY = 10
_EAGLE_TX = 11            # eagle TL — covers cols 11-12, rows 10-11
_EAGLE_TY = 10
_ENEMY_SPAWNS = ((0, 0), (11, 0), (22, 0))


def _stage_for(n):
    return _STAGES[(n - 1) % len(_STAGES)]


def _parse_stage(rows):
    tiles = [[0] * R.GRID_W for _ in range(R.GRID_H)]
    brick_hp = [[0] * R.GRID_W for _ in range(R.GRID_H)]
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch == 'B':
                tiles[y][x] = R.T_BRICK
                brick_hp[y][x] = 0xF
            elif ch == 'S':
                tiles[y][x] = R.T_STEEL
            elif ch == 'W':
                tiles[y][x] = R.T_WATER
            elif ch == 'F':
                tiles[y][x] = R.T_FOREST
            elif ch == 'E':
                tiles[y][x] = R.T_EAGLE
    return tiles, brick_hp


# ---------------------------------------------------------------------------
# Collision queries
# ---------------------------------------------------------------------------

def _tile_blocks_tank(t):
    return t in (R.T_BRICK, R.T_STEEL, R.T_WATER, R.T_EAGLE, R.T_EAGLE_DEAD)


def _can_tank_be_at(state, px, py, ignore_self=None):
    if px < 0 or py < 0 or px + R.TANK > R.FIELD_W or py + R.TANK > R.FIELD_H:
        return False
    tx0 = px // R.TILE
    ty0 = py // R.TILE
    tx1 = (px + R.TANK - 1) // R.TILE
    ty1 = (py + R.TANK - 1) // R.TILE
    tiles = state["tiles"]
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            t = tiles[ty][tx]
            if t == R.T_FOREST or t == R.T_EMPTY:
                continue
            if _tile_blocks_tank(t):
                return False
    pl = state["player"]
    if pl is not ignore_self and pl["alive"] and pl["respawn_t"] <= 0:
        if abs(px - pl["px"]) < R.TANK and abs(py - pl["py"]) < R.TANK:
            return False
    for e in state["enemies"]:
        if e is ignore_self:
            continue
        if not e["alive"]:
            continue
        if abs(px - e["px"]) < R.TANK and abs(py - e["py"]) < R.TANK:
            return False
    return True


# ---------------------------------------------------------------------------
# Brick damage — sub-cell layout bit0=TL bit1=TR bit2=BL bit3=BR
# ---------------------------------------------------------------------------

# Sub-cell damage is now applied inline by _bullet_hits_tile so that
# already-destroyed sub-cells are transparent to passing bullets.


# ---------------------------------------------------------------------------
# Object pools
# ---------------------------------------------------------------------------

def _make_player():
    return {"alive": False, "px": 0.0, "py": 0.0, "dir": R.D_UP,
            "fire_cd": 0, "respawn_t": 0, "invincible": 0}


def _make_enemy():
    return {"alive": False, "px": 0.0, "py": 0.0, "dir": R.D_DOWN,
            "fire_cd": 0, "turn_cd": 0, "stuck": 0}


def _make_bullet():
    return {"alive": False, "x": 0.0, "y": 0.0, "dir": 0, "owner": 0,
            "lx": -1, "ly": -1}


def _make_explosion():
    return {"alive": False, "cx": 0, "cy": 0, "frame": 0}


def _new_state(stage=1, lives=_LIVES_START, score=0):
    tiles, brick_hp = _parse_stage(_stage_for(stage))
    state = {
        "tiles": tiles,
        "brick_hp": brick_hp,
        "player": _make_player(),
        "player_bullet": _make_bullet(),
        "enemies":   [_make_enemy()     for _ in range(_MAX_ENEMIES_ON_SCREEN)],
        "ebullets":  [_make_bullet()    for _ in range(_MAX_ENEMIES_ON_SCREEN + 1)],
        "explosions":[_make_explosion() for _ in range(4)],
        "lives": lives,
        "stage": stage,
        "score": score,
        "enemies_left":     _ENEMIES_PER_STAGE,
        "enemies_to_spawn": _ENEMIES_PER_STAGE,
        "spawn_cd": 30,
        "spawn_idx": 0,
        "lost": False,
        "won": False,
        "frame": 0,
    }
    _spawn_player(state)
    return state


def _spawn_player(state):
    p = state["player"]
    p["px"] = float(_PLAYER_SPAWN_TX * R.TILE)
    p["py"] = float(_PLAYER_SPAWN_TY * R.TILE)
    p["dir"] = R.D_UP
    p["fire_cd"] = 0
    p["alive"] = True
    p["respawn_t"] = 0
    p["invincible"] = _INVINCIBLE_F


def _try_spawn_enemy(state):
    if state["enemies_to_spawn"] <= 0:
        return False
    slot = None
    for e in state["enemies"]:
        if not e["alive"]:
            slot = e
            break
    if slot is None:
        return False
    for attempt in range(3):
        si = (state["spawn_idx"] + attempt) % len(_ENEMY_SPAWNS)
        tx, ty = _ENEMY_SPAWNS[si]
        px = float(tx * R.TILE)
        py = float(ty * R.TILE)
        if _can_tank_be_at(state, int(px), int(py), ignore_self=slot):
            slot["alive"] = True
            slot["px"] = px
            slot["py"] = py
            slot["dir"] = R.D_DOWN
            slot["fire_cd"] = random.randint(20, 60)
            slot["turn_cd"] = random.randint(40, 80)
            slot["stuck"] = 0
            state["spawn_idx"] = (si + 1) % len(_ENEMY_SPAWNS)
            state["enemies_to_spawn"] -= 1
            return True
    return False


def _spawn_bullet_for(state, tank, owner):
    if owner == 0:
        b = state["player_bullet"]
        if b["alive"]:
            return False
    else:
        b = None
        for cand in state["ebullets"]:
            if not cand["alive"]:
                b = cand
                break
        if b is None:
            return False
    direction = tank["dir"]
    cx = tank["px"] + R.TANK / 2.0
    cy = tank["py"] + R.TANK / 2.0
    if direction == R.D_UP:
        x = cx - R.BULLET_W / 2.0
        y = tank["py"] - R.BULLET_H
    elif direction == R.D_DOWN:
        x = cx - R.BULLET_W / 2.0
        y = tank["py"] + R.TANK
    elif direction == R.D_LEFT:
        x = tank["px"] - R.BULLET_H
        y = cy - R.BULLET_W / 2.0
    else:
        x = tank["px"] + R.TANK
        y = cy - R.BULLET_W / 2.0
    b["alive"] = True
    b["x"] = x
    b["y"] = y
    b["dir"] = direction
    b["owner"] = owner
    b["lx"] = -1
    b["ly"] = -1
    return True


def _spawn_explosion(state, cx, cy):
    for ex in state["explosions"]:
        if not ex["alive"]:
            ex["alive"] = True
            ex["cx"] = int(cx)
            ex["cy"] = int(cy)
            ex["frame"] = 0
            return


# ---------------------------------------------------------------------------
# Map drawing helpers
# ---------------------------------------------------------------------------

def _draw_full_map(state):
    R.draw_field_bg()
    tiles = state["tiles"]
    brick_hp = state["brick_hp"]
    for ty in range(R.GRID_H):
        for tx in range(R.GRID_W):
            t = tiles[ty][tx]
            if t == R.T_EMPTY:
                continue
            if t == R.T_EAGLE:
                if (tx, ty) == (_EAGLE_TX, _EAGLE_TY):
                    R.draw_eagle(_EAGLE_TX, _EAGLE_TY, True)
                continue
            if t == R.T_EAGLE_DEAD:
                if (tx, ty) == (_EAGLE_TX, _EAGLE_TY):
                    R.draw_eagle(_EAGLE_TX, _EAGLE_TY, False)
                continue
            R.draw_tile(tx, ty, t, brick_hp[ty][tx] if t == R.T_BRICK else 0)


def _redraw_tiles_in_field_area(state, fx, fy, w, h):
    tx0 = max(0, fx // R.TILE)
    ty0 = max(0, fy // R.TILE)
    tx1 = min(R.GRID_W - 1, (fx + w - 1) // R.TILE)
    ty1 = min(R.GRID_H - 1, (fy + h - 1) // R.TILE)
    if tx0 > tx1 or ty0 > ty1:
        return
    tiles = state["tiles"]
    brick_hp = state["brick_hp"]
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            t = tiles[ty][tx]
            if t == R.T_EAGLE:
                if (tx, ty) == (_EAGLE_TX, _EAGLE_TY):
                    R.draw_eagle(_EAGLE_TX, _EAGLE_TY, True)
                continue
            if t == R.T_EAGLE_DEAD:
                if (tx, ty) == (_EAGLE_TX, _EAGLE_TY):
                    R.draw_eagle(_EAGLE_TX, _EAGLE_TY, False)
                continue
            if t == R.T_EMPTY:
                continue
            R.draw_tile(tx, ty, t, brick_hp[ty][tx] if t == R.T_BRICK else 0)


def _redraw_forest_overlap(state, px, py):
    tx0 = max(0, int(px) // R.TILE)
    ty0 = max(0, int(py) // R.TILE)
    tx1 = min(R.GRID_W - 1, (int(px) + R.TANK - 1) // R.TILE)
    ty1 = min(R.GRID_H - 1, (int(py) + R.TANK - 1) // R.TILE)
    tiles = state["tiles"]
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            if tiles[ty][tx] == R.T_FOREST:
                R.draw_tile(tx, ty, R.T_FOREST)


# ---------------------------------------------------------------------------
# Tank movement
# ---------------------------------------------------------------------------

def _try_move_tank(state, tank, direction, speed, ignore_self):
    """Move tank; turn is instant (Battle-City convention). Snap perpendicular
    axis to tile track so the tank fits into 1-tile-wide corridors. Returns
    True iff the tank actually moved."""
    tank["dir"] = direction
    px = tank["px"]
    py = tank["py"]
    if direction == R.D_UP or direction == R.D_DOWN:
        target_x = round(px / R.TILE) * R.TILE
        if abs(target_x - px) <= speed + 0.01:
            new_px = target_x
        elif target_x > px:
            new_px = px + speed
        else:
            new_px = px - speed
        new_py = py + _DY[direction] * speed
        if _can_tank_be_at(state, int(round(new_px)), int(round(py)), ignore_self):
            px = new_px
        if _can_tank_be_at(state, int(round(px)), int(round(new_py)), ignore_self):
            py = new_py
    else:
        target_y = round(py / R.TILE) * R.TILE
        if abs(target_y - py) <= speed + 0.01:
            new_py = target_y
        elif target_y > py:
            new_py = py + speed
        else:
            new_py = py - speed
        new_px = px + _DX[direction] * speed
        if _can_tank_be_at(state, int(round(px)), int(round(new_py)), ignore_self):
            py = new_py
        if _can_tank_be_at(state, int(round(new_px)), int(round(py)), ignore_self):
            px = new_px

    moved = (abs(px - tank["px"]) > 0.01) or (abs(py - tank["py"]) > 0.01)
    tank["px"] = px
    tank["py"] = py
    return moved


# ---------------------------------------------------------------------------
# Enemy AI
# ---------------------------------------------------------------------------

def _aligned(e, tx, ty):
    e_cx = e["px"] + R.TANK / 2
    e_cy = e["py"] + R.TANK / 2
    t_cx = tx + R.TANK / 2
    t_cy = ty + R.TANK / 2
    direction = e["dir"]
    if direction == R.D_UP:
        return abs(e_cx - t_cx) < R.TANK and t_cy < e_cy
    if direction == R.D_DOWN:
        return abs(e_cx - t_cx) < R.TANK and t_cy > e_cy
    if direction == R.D_LEFT:
        return abs(e_cy - t_cy) < R.TANK and t_cx < e_cx
    if direction == R.D_RIGHT:
        return abs(e_cy - t_cy) < R.TANK and t_cx > e_cx
    return False


def _enemy_ai_step(state, e):
    moved = _try_move_tank(state, e, e["dir"], _ENEMY_SPEED, ignore_self=e)
    if not moved:
        e["stuck"] += 1
    else:
        e["stuck"] = 0

    e["turn_cd"] -= 1
    if e["stuck"] >= 3 or e["turn_cd"] <= 0:
        candidates = [R.D_UP, R.D_RIGHT, R.D_DOWN, R.D_LEFT]
        _shuffle(candidates)
        if random.random() < 0.6:
            pl = state["player"]
            if pl["alive"]:
                target_x = pl["px"]
                target_y = pl["py"]
            else:
                target_x = _EAGLE_TX * R.TILE
                target_y = _EAGLE_TY * R.TILE
            dx = target_x - e["px"]
            dy = target_y - e["py"]
            preferred = []
            if abs(dx) > abs(dy):
                preferred.append(R.D_RIGHT if dx > 0 else R.D_LEFT)
                preferred.append(R.D_DOWN if dy > 0 else R.D_UP)
            else:
                preferred.append(R.D_DOWN if dy > 0 else R.D_UP)
                preferred.append(R.D_RIGHT if dx > 0 else R.D_LEFT)
            candidates = preferred + [d for d in candidates if d not in preferred]
        for d in candidates:
            test_x = e["px"] + _DX[d]
            test_y = e["py"] + _DY[d]
            if _can_tank_be_at(state, int(round(test_x)), int(round(test_y)),
                               ignore_self=e):
                e["dir"] = d
                break
        e["turn_cd"] = random.randint(30, 90)
        e["stuck"] = 0

    e["fire_cd"] -= 1
    if e["fire_cd"] <= 0:
        should = False
        pl = state["player"]
        if pl["alive"] and _aligned(e, pl["px"], pl["py"]):
            should = True
        if not should and random.random() < 0.12:
            should = True
        if should:
            if _spawn_bullet_for(state, e, owner=1):
                e["fire_cd"] = random.randint(40, 90)
            else:
                e["fire_cd"] = 5
        else:
            e["fire_cd"] = random.randint(20, 50)


# ---------------------------------------------------------------------------
# Bullets
# ---------------------------------------------------------------------------

def _bullet_size(direction):
    if direction == R.D_UP or direction == R.D_DOWN:
        return R.BULLET_W, R.BULLET_H
    return R.BULLET_H, R.BULLET_W


_SUB_OFFSETS = ((0, 0), (1, 0), (0, 1), (1, 1))  # bit0=TL bit1=TR bit2=BL bit3=BR


def _bullet_hits_tile(state, b):
    """Sub-cell-precise: the bullet only collides with brick sub-cells that
    are still intact. Already-destroyed sub-cells are transparent — so once
    you blow out the bottom half of a brick, the next bullet flies through
    and hits the top half (classic Battle-City behavior)."""
    bw, bh = _bullet_size(b["dir"])
    bx = int(b["x"])
    by = int(b["y"])
    if bx < 0 or by < 0 or bx + bw > R.FIELD_W or by + bh > R.FIELD_H:
        return True
    tiles = state["tiles"]
    brick_hp = state["brick_hp"]
    sub = R.TILE // 2
    tx0 = bx // R.TILE
    ty0 = by // R.TILE
    tx1 = (bx + bw - 1) // R.TILE
    ty1 = (by + bh - 1) // R.TILE
    hit_any = False
    bx_end = bx + bw
    by_end = by + bh
    for ty in range(max(0, ty0), min(R.GRID_H, ty1 + 1)):
        for tx in range(max(0, tx0), min(R.GRID_W, tx1 + 1)):
            t = tiles[ty][tx]
            if t == R.T_BRICK:
                bits = brick_hp[ty][tx]
                if bits == 0:
                    continue
                tile_px = tx * R.TILE
                tile_py = ty * R.TILE
                new_bits = bits
                hit_this = False
                for i in range(4):
                    if not (bits & (1 << i)):
                        continue
                    ox, oy = _SUB_OFFSETS[i]
                    sx = tile_px + ox * sub
                    sy = tile_py + oy * sub
                    if (bx < sx + sub and bx_end > sx and
                            by < sy + sub and by_end > sy):
                        new_bits &= ~(1 << i)
                        hit_this = True
                if hit_this:
                    brick_hp[ty][tx] = new_bits
                    if new_bits == 0:
                        tiles[ty][tx] = R.T_EMPTY
                        R.draw_tile(tx, ty, R.T_EMPTY)
                    else:
                        R.draw_tile(tx, ty, R.T_BRICK, new_bits)
                    _snd(*_SND_BRICK)
                    hit_any = True
            elif t == R.T_STEEL:
                _snd(*_SND_STEEL)
                hit_any = True
            elif t == R.T_EAGLE:
                state["lost"] = True
                for dy in range(2):
                    for dx in range(2):
                        if 0 <= _EAGLE_TY + dy < R.GRID_H and 0 <= _EAGLE_TX + dx < R.GRID_W:
                            state["tiles"][_EAGLE_TY + dy][_EAGLE_TX + dx] = R.T_EAGLE_DEAD
                R.draw_eagle(_EAGLE_TX, _EAGLE_TY, False)
                _spawn_explosion(state,
                                 R.FIELD_X + _EAGLE_TX * R.TILE + R.TANK // 2,
                                 R.FIELD_Y + _EAGLE_TY * R.TILE + R.TANK // 2)
                _snd(*_SND_BOOM)
                hit_any = True
    return hit_any


def _bullet_hits_tank(b, tank):
    bw, bh = _bullet_size(b["dir"])
    return (b["x"] < tank["px"] + R.TANK and b["x"] + bw > tank["px"] and
            b["y"] < tank["py"] + R.TANK and b["y"] + bh > tank["py"])


def _bullets_overlap(a, b):
    aw, ah = _bullet_size(a["dir"])
    bw, bh = _bullet_size(b["dir"])
    return (a["x"] < b["x"] + bw and a["x"] + aw > b["x"] and
            a["y"] < b["y"] + bh and a["y"] + ah > b["y"])


def _process_bullets(state):
    bullets = [state["player_bullet"]] + state["ebullets"]
    for b in bullets:
        if not b["alive"]:
            continue
        if b["lx"] >= 0:
            R.erase_bullet_field(b["lx"], b["ly"], b["dir"])
            pad = R.BULLET_H
            _redraw_tiles_in_field_area(state,
                                        b["lx"] - 1, b["ly"] - 1,
                                        pad + 2, pad + 2)

        b["x"] += _DX[b["dir"]] * _BULLET_SPEED
        b["y"] += _DY[b["dir"]] * _BULLET_SPEED

        if _bullet_hits_tile(state, b):
            b["alive"] = False
            b["lx"] = -1
            continue

        owner = b["owner"]
        pl = state["player"]
        if owner == 1:
            if pl["alive"] and pl["respawn_t"] <= 0 and pl["invincible"] <= 0:
                if _bullet_hits_tank(b, pl):
                    b["alive"] = False
                    b["lx"] = -1
                    _kill_player(state)
                    continue
            pb = state["player_bullet"]
            if pb["alive"] and _bullets_overlap(b, pb):
                if pb["lx"] >= 0:
                    R.erase_bullet_field(pb["lx"], pb["ly"], pb["dir"])
                pb["alive"] = False
                pb["lx"] = -1
                b["alive"] = False
                b["lx"] = -1
                continue
        else:
            killed = False
            for e in state["enemies"]:
                if not e["alive"]:
                    continue
                if _bullet_hits_tank(b, e):
                    e["alive"] = False
                    state["enemies_left"] -= 1
                    state["score"] += 100
                    _spawn_explosion(state,
                                     R.FIELD_X + int(e["px"]) + R.TANK // 2,
                                     R.FIELD_Y + int(e["py"]) + R.TANK // 2)
                    R.erase_tank_field(int(e["px"]), int(e["py"]))
                    _snd(*_SND_KILL)
                    b["alive"] = False
                    b["lx"] = -1
                    killed = True
                    break
            if killed:
                continue

        bx = int(b["x"])
        by = int(b["y"])
        R.draw_bullet_screen(R.FIELD_X + bx, R.FIELD_Y + by, b["dir"])
        b["lx"] = bx
        b["ly"] = by


def _kill_player(state):
    p = state["player"]
    _spawn_explosion(state,
                     R.FIELD_X + int(p["px"]) + R.TANK // 2,
                     R.FIELD_Y + int(p["py"]) + R.TANK // 2)
    R.erase_tank_field(int(p["px"]), int(p["py"]))
    _snd(*_SND_BOOM)
    p["alive"] = False
    state["lives"] -= 1
    if state["lives"] <= 0:
        state["lost"] = True
    else:
        p["respawn_t"] = _RESPAWN_F


# ---------------------------------------------------------------------------
# Drawing wrappers
# ---------------------------------------------------------------------------

def _draw_player_tank(p):
    if p["invincible"] > 0 and (p["invincible"] // 4) & 1:
        return
    R.draw_tank(R.FIELD_X + int(p["px"]), R.FIELD_Y + int(p["py"]),
                p["dir"], R.PLAYER_C, R.PLAYER_TUR)


def _draw_enemy_tank(e):
    R.draw_tank(R.FIELD_X + int(e["px"]), R.FIELD_Y + int(e["py"]),
                e["dir"], R.ENEMY_BASIC, R.PLAYER_C)


# ---------------------------------------------------------------------------
# Per-frame tick
# ---------------------------------------------------------------------------

def _tick(state, held_dir, fire_pressed):
    """One frame.

    `held_dir`: the direction key currently being held (D_UP/.../D_LEFT) or
        None. If held, the tank turns to face that way and moves; if None,
        the tank stops in place but keeps its last facing direction.
    `fire_pressed`: SPACE was hit this frame.
    """
    s = state
    p = s["player"]
    s["frame"] += 1

    if not p["alive"]:
        if p["respawn_t"] > 0:
            p["respawn_t"] -= 1
            if p["respawn_t"] == 0 and s["lives"] > 0 and not s["lost"]:
                spawn_px = _PLAYER_SPAWN_TX * R.TILE
                spawn_py = _PLAYER_SPAWN_TY * R.TILE
                if _can_tank_be_at(s, spawn_px, spawn_py, ignore_self=p):
                    _spawn_player(s)
                else:
                    p["respawn_t"] = 10

    if p["alive"]:
        old_x = int(p["px"])
        old_y = int(p["py"])
        old_d = p["dir"]

        if held_dir is not None:
            _try_move_tank(s, p, held_dir, _PLAYER_SPEED, ignore_self=p)

        new_x = int(p["px"])
        new_y = int(p["py"])
        new_d = p["dir"]
        if (old_x, old_y, old_d) != (new_x, new_y, new_d):
            R.erase_tank_field(old_x, old_y)
            _redraw_tiles_in_field_area(s, old_x, old_y, R.TANK, R.TANK)

        if p["fire_cd"] > 0:
            p["fire_cd"] -= 1
        if fire_pressed and p["fire_cd"] <= 0:
            if _spawn_bullet_for(s, p, owner=0):
                p["fire_cd"] = _FIRE_COOLDOWN_F
                _snd(*_SND_FIRE)
        if p["invincible"] > 0:
            p["invincible"] -= 1

    for e in s["enemies"]:
        if not e["alive"]:
            continue
        old_x = int(e["px"])
        old_y = int(e["py"])
        old_d = e["dir"]
        _enemy_ai_step(s, e)
        new_x = int(e["px"])
        new_y = int(e["py"])
        new_d = e["dir"]
        if (old_x, old_y, old_d) != (new_x, new_y, new_d):
            R.erase_tank_field(old_x, old_y)
            _redraw_tiles_in_field_area(s, old_x, old_y, R.TANK, R.TANK)

    s["spawn_cd"] -= 1
    if s["spawn_cd"] <= 0 and s["enemies_to_spawn"] > 0:
        if _try_spawn_enemy(s):
            s["spawn_cd"] = 90
        else:
            s["spawn_cd"] = 20

    _process_bullets(s)

    for ex in s["explosions"]:
        if not ex["alive"]:
            continue
        if ex["frame"] > 0:
            R.erase_explosion(ex["cx"], ex["cy"])
            half = R.EXPLO_ERASE_HALF
            size = R.EXPLO_ERASE_SIZE
            _redraw_tiles_in_field_area(s,
                                        ex["cx"] - half - R.FIELD_X,
                                        ex["cy"] - half - R.FIELD_Y,
                                        size, size)
        ex["frame"] += 1
        if ex["frame"] > _EXPLOSION_FRAMES:
            ex["alive"] = False
            continue
        R.draw_explosion(ex["cx"], ex["cy"], ex["frame"])

    if p["alive"]:
        _draw_player_tank(p)
    for e in s["enemies"]:
        if e["alive"]:
            _draw_enemy_tank(e)

    et = s["tiles"][_EAGLE_TY][_EAGLE_TX]
    if et == R.T_EAGLE:
        R.draw_eagle(_EAGLE_TX, _EAGLE_TY, True)
    elif et == R.T_EAGLE_DEAD:
        R.draw_eagle(_EAGLE_TX, _EAGLE_TY, False)

    if p["alive"]:
        _redraw_forest_overlap(s, p["px"], p["py"])
    for e in s["enemies"]:
        if e["alive"]:
            _redraw_forest_overlap(s, e["px"], e["py"])

    if s["enemies_left"] <= 0 and not s["lost"]:
        s["won"] = True


# ---------------------------------------------------------------------------
# Input — TCA8418 raw event hook for hold-to-move.
#
# The Cardputer-Adv keyboard chip (TCA8418, I2C addr 0x34) emits BOTH press
# and release events to its FIFO, but the upstream M5 driver
# (m5stack/uiflow-micropython, `hardware/keyboard/__init__._ascii_handler`)
# drops releases on the floor:
#
#     keyevent.state and self._keyevents.append(keyevent)
#
# That makes hold-to-move impossible through `MatrixKeyboard.get_key()` — we
# never learn when the user lets go of an arrow. So we hook the
# `_tick_handler` slot on the underlying `KeyboardI2C` instance to receive
# the raw events, and restore the original handler on exit.
#
# Keys used (cardputer matrix coords from the M5 `_key_value_map`):
#     (2, 11)  ';'  — UP arrow (visual arrow on key cap)
#     (3, 11)  '.'  — DOWN
#     (3, 10)  ','  — LEFT
#     (3, 12)  '/'  — RIGHT
#     (3, 13)  SPACE — fire
#     (2, 13)  ENTER — pause / confirm
#     (2,  0)  FN
#     (0,  0)  '`'  — with FN held, ESC (= quit)
#
# No WASD: the cardputer's ; , . / keys are physically labelled as arrows,
# so we treat them as directional regardless of FN — no modifier required.
# ---------------------------------------------------------------------------

_DIR_KEYS = {
    (2, 11): R.D_UP,
    (3, 11): R.D_DOWN,
    (3, 10): R.D_LEFT,
    (3, 12): R.D_RIGHT,
}
_FN_RC    = (2, 0)
_SPACE_RC = (3, 13)
_ENTER_RC = (2, 13)
_ESC_RC   = (0, 0)


class _Input:
    def __init__(self, kb):
        self._kb = kb
        self._ikb = None
        self._orig = None
        self._installed = False
        self._held = set()
        self._dir_stack = []    # most-recent-last
        self._press = []        # 'fire' / 'pause' / 'exit'
        try:
            ikb = kb._keyboard
            if hasattr(ikb, "_tick_handler") and hasattr(ikb, "_tca"):
                self._ikb = ikb
                self._orig = ikb._tick_handler
                ikb._tick_handler = self._handler
                if hasattr(ikb, "_keyevents"):
                    ikb._keyevents.clear()
                self._installed = True
        except Exception:
            self._installed = False

    def _handler(self, events):
        # IRQ-scheduled; must never raise or the keyboard locks up.
        try:
            for ev in events:
                state = bool(ev & 0x80)
                v = (ev & 0x7F) - 1
                if v < 0:
                    continue
                rt = v // 10
                ct = v % 10
                col = rt * 2
                if ct > 3:
                    col += 1
                row = (ct + 4) % 4
                rc = (row, col)

                if state:
                    self._held.add(rc)
                else:
                    self._held.discard(rc)
                if not state:
                    continue

                if rc in _DIR_KEYS:
                    d = _DIR_KEYS[rc]
                    try:
                        self._dir_stack.remove(d)
                    except ValueError:
                        pass
                    self._dir_stack.append(d)
                elif rc == _SPACE_RC:
                    self._press.append('fire')
                elif rc == _ENTER_RC:
                    self._press.append('pause')
                elif rc == _ESC_RC and (_FN_RC in self._held):
                    self._press.append('exit')
        except Exception:
            pass

    def direction(self):
        """Most-recently-pressed direction key that is still held, else None."""
        if not self._installed:
            return None
        active = set()
        for rc in self._held:
            if rc in _DIR_KEYS:
                active.add(_DIR_KEYS[rc])
        while self._dir_stack:
            d = self._dir_stack[-1]
            if d in active:
                return d
            self._dir_stack.pop()
        if active:
            # arrow held before our hook installed: latch it.
            for d in active:
                self._dir_stack.append(d)
                return d
        return None

    def take_presses(self):
        e = self._press
        self._press = []
        return e

    def restore(self):
        if self._installed and self._ikb is not None:
            try:
                self._ikb._tick_handler = self._orig
                if hasattr(self._ikb, "_keyevents"):
                    self._ikb._keyevents.clear()
            except Exception:
                pass
            self._installed = False


def _wait_for(inp, accept):
    """Block until any press event in `accept` arrives. Returns the name."""
    while True:
        try:
            M5.update()
        except Exception:
            pass
        for name in inp.take_presses():
            if name in accept:
                return name
        time.sleep_ms(20)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run():
    try:
        M5.Speaker.begin()
        M5.Speaker.setVolume(_VOL)
    except Exception:
        pass

    kb = MatrixKeyboard()
    inp = _Input(kb)

    try:
        R.show_start(1)
        if _wait_for(inp, ('pause', 'fire', 'exit')) == 'exit':
            return

        state = _new_state()
        Lcd.clear(R.BG)
        _draw_full_map(state)
        R.draw_hud(state)
        gc.collect()

        paused = False
        last_gc = 0
        last_hud_score   = state["score"]
        last_hud_lives   = state["lives"]
        last_hud_enemies = state["enemies_left"]
        last_hud_stage   = state["stage"]

        while True:
            frame_start = time.ticks_ms()
            try:
                M5.update()
            except Exception:
                pass

            presses = inp.take_presses()
            fire_pressed = 'fire' in presses
            toggle_pause = 'pause' in presses
            do_exit = 'exit' in presses

            if do_exit:
                return

            if state["lost"] or state["won"]:
                if toggle_pause or fire_pressed:
                    if state["won"]:
                        state = _new_state(stage=state["stage"] + 1,
                                           lives=state["lives"],
                                           score=state["score"])
                    else:
                        state = _new_state()
                    Lcd.clear(R.BG)
                    _draw_full_map(state)
                    R.draw_hud(state)
                    paused = False
                    last_hud_score   = state["score"]
                    last_hud_lives   = state["lives"]
                    last_hud_enemies = state["enemies_left"]
                    last_hud_stage   = state["stage"]
                    gc.collect()
                    continue
                time.sleep_ms(_FRAME_MS)
                continue

            if toggle_pause:
                paused = not paused
                if paused:
                    R.show_message("PAUSED", "Hold arrows to move",
                                   R.TITLE_FG, R.TEXT,
                                   "ENTER resume   ESC quit")
                else:
                    _draw_full_map(state)
                    R.draw_hud(state)

            if paused:
                time.sleep_ms(_FRAME_MS)
                continue

            held_dir = inp.direction()
            _tick(state, held_dir, fire_pressed)

            if state["lost"]:
                for f, ms in _SND_OVER:
                    _snd(f, ms)
                    time.sleep_ms(ms + 20)
                R.show_message("GAME OVER", "Score " + str(state["score"]),
                               R.LOSE, R.TEXT, "ENTER restart   ESC quit")
                continue

            if state["won"]:
                for f, ms in _SND_STAGE:
                    _snd(f, ms)
                    time.sleep_ms(ms + 20)
                R.show_message("STAGE CLEAR",
                               "Stage " + str(state["stage"] + 1) + " ready",
                               R.WIN, R.TEXT, "ENTER continue   ESC quit")
                continue

            if (state["score"]        != last_hud_score
                    or state["lives"] != last_hud_lives
                    or state["enemies_left"] != last_hud_enemies
                    or state["stage"] != last_hud_stage):
                R.draw_hud(state)
                last_hud_score   = state["score"]
                last_hud_lives   = state["lives"]
                last_hud_enemies = state["enemies_left"]
                last_hud_stage   = state["stage"]

            last_gc += 1
            if last_gc >= _GC_EVERY:
                last_gc = 0
                gc.collect()

            elapsed = time.ticks_diff(time.ticks_ms(), frame_start)
            if elapsed < _FRAME_MS:
                time.sleep_ms(_FRAME_MS - elapsed)

    finally:
        try:
            M5.Speaker.stop()
        except Exception:
            pass
        inp.restore()
        _silence_codec()
