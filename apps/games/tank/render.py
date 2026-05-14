"""Drawing primitives for the Tank game — full-screen layout.

Layout: 240x120 playfield on the BOTTOM (24x12 tiles of 10 px), 15-px HUD strip
on TOP. No side panel — the screen is too small to waste 45% of width on text.

Keep this file lean; CLAUDE.md flags hard-faults when a single .py grows past
~25KB or a function past ~100 lines. We split app.py + render.py for that
reason.
"""

import M5
from M5 import Lcd

# ---------------------------------------------------------------------------
# Geometry — duplicated from app.py for self-containment.  app.py imports
# these names so this file is the single source of truth.
# ---------------------------------------------------------------------------
W = 240
H = 135
TILE = 10
GRID_W = 24
GRID_H = 12
FIELD_W = TILE * GRID_W          # 240
FIELD_H = TILE * GRID_H          # 120
HUD_H = H - FIELD_H              # 15
FIELD_X = 0
FIELD_Y = HUD_H                  # 15
TANK = 2 * TILE                  # 20
BULLET_W = 3
BULLET_H = 6

# Tile constants
T_EMPTY      = 0
T_BRICK      = 1
T_STEEL      = 2
T_WATER      = 3
T_FOREST     = 4
T_EAGLE      = 9
T_EAGLE_DEAD = 10

# Direction constants
D_UP    = 0
D_RIGHT = 1
D_DOWN  = 2
D_LEFT  = 3

# Colors — NES-flavoured palette
BG          = 0x000000
FIELD_BG    = 0x080808
HUD_BG      = 0x000000
HUD_LINE    = 0x202020
TEXT        = 0xFFFFFF
DIM         = 0x707088
PLAYER_C    = 0xC8A040     # ochre body
PLAYER_TUR  = 0xFFE060     # bright yellow trim
ENEMY_BASIC = 0xE0E0E0     # gray (basic)
ENEMY_FAST  = 0xFFB060     # orange (fast)
ENEMY_POWER = 0xFF80E0     # pink (rapid)
ENEMY_ARMOR = 0xA0FFA0     # green (armored)
ENEMY_BONUS = 0xFF4040     # red flash (drops powerup)
BULLET_C    = 0xFFFFFF
BRICK       = 0xC07040
BRICK_DARK  = 0x603018
STEEL       = 0xA0A0B0
STEEL_DARK  = 0x404050
WATER       = 0x4060FF
WATER_DARK  = 0x2030A0
FOREST      = 0x40C040
FOREST_DARK = 0x208020
EAGLE_BODY  = 0xE0D0A0
EAGLE_DARK  = 0x603018
EAGLE_DEAD  = 0x804020
EXPLO_OUT   = 0xFFA040
EXPLO_IN    = 0xFFFF80
TITLE_FG    = 0xFFD040
LOSE        = 0xFF4040
WIN         = 0x40FF60

FONT_S = M5.Lcd.FONTS.DejaVu12
FONT_M = M5.Lcd.FONTS.DejaVu18
FONT_L = M5.Lcd.FONTS.DejaVu24


# ---------------------------------------------------------------------------
# Coord helpers
# ---------------------------------------------------------------------------

def tx_to_px(tx):
    return FIELD_X + tx * TILE


def ty_to_py(ty):
    return FIELD_Y + ty * TILE


# ---------------------------------------------------------------------------
# Field background
# ---------------------------------------------------------------------------

def draw_field_bg():
    Lcd.fillRect(FIELD_X, FIELD_Y, FIELD_W, FIELD_H, FIELD_BG)


# ---------------------------------------------------------------------------
# Tile drawing
# ---------------------------------------------------------------------------

def draw_tile(tx, ty, ttype, hp_bits=0):
    """Draw or erase a single TILE x TILE tile. For brick, hp_bits is a 4-bit
    field: bit0=TL bit1=TR bit2=BL bit3=BR. Sub-cell size = TILE//2."""
    px = tx_to_px(tx)
    py = ty_to_py(ty)
    Lcd.fillRect(px, py, TILE, TILE, FIELD_BG)
    sub = TILE // 2  # 5
    if ttype == T_BRICK:
        for i, (sx, sy) in enumerate(((0, 0), (sub, 0), (0, sub), (sub, sub))):
            if hp_bits & (1 << i):
                bx = px + sx
                by = py + sy
                Lcd.fillRect(bx, by, sub, sub, BRICK)
                # Mortar fleck: dark crosshair to suggest brick courses
                Lcd.drawLine(bx, by + sub // 2,
                             bx + sub - 1, by + sub // 2, BRICK_DARK)
                Lcd.drawPixel(bx + sub // 2, by, BRICK_DARK)
                Lcd.drawPixel(bx + sub // 2, by + sub - 1, BRICK_DARK)
    elif ttype == T_STEEL:
        Lcd.fillRect(px, py, TILE, TILE, STEEL)
        # 4 dark corners + small center pip = NES-ish chrome look
        Lcd.drawPixel(px, py, STEEL_DARK)
        Lcd.drawPixel(px + TILE - 1, py, STEEL_DARK)
        Lcd.drawPixel(px, py + TILE - 1, STEEL_DARK)
        Lcd.drawPixel(px + TILE - 1, py + TILE - 1, STEEL_DARK)
        Lcd.drawLine(px + 2, py + 2, px + TILE - 3, py + 2, STEEL_DARK)
        Lcd.drawLine(px + 2, py + TILE - 3, px + TILE - 3, py + TILE - 3,
                     STEEL_DARK)
    elif ttype == T_WATER:
        Lcd.fillRect(px, py, TILE, TILE, WATER)
        # Two wave lines, offset between adjacent tiles via y parity
        Lcd.drawLine(px + 1, py + 2, px + 4, py + 2, WATER_DARK)
        Lcd.drawLine(px + 5, py + 6, px + TILE - 2, py + 6, WATER_DARK)
    elif ttype == T_FOREST:
        Lcd.fillRect(px, py, TILE, TILE, FOREST)
        # Scatter darker pixels — variable per-tile so adjacent tiles blend
        Lcd.drawPixel(px + 1, py + 1, FOREST_DARK)
        Lcd.drawPixel(px + 4, py + 2, FOREST_DARK)
        Lcd.drawPixel(px + 7, py + 4, FOREST_DARK)
        Lcd.drawPixel(px + 2, py + 6, FOREST_DARK)
        Lcd.drawPixel(px + 5, py + 7, FOREST_DARK)
        Lcd.drawPixel(px + 8, py + 8, FOREST_DARK)


def draw_eagle(tx, ty, alive):
    """Eagle occupies a 2x2 tile area (TANK x TANK = 20x20)."""
    px = tx_to_px(tx)
    py = ty_to_py(ty)
    Lcd.fillRect(px, py, TANK, TANK, FIELD_BG)
    if alive:
        # Body — main pale shape
        Lcd.fillRect(px + 3, py + 8, TANK - 6, 10, EAGLE_BODY)
        # Head / chest
        Lcd.fillRect(px + 5, py + 3, TANK - 10, 7, EAGLE_BODY)
        # Beak
        Lcd.fillRect(px + 8, py + 1, 4, 4, EAGLE_DARK)
        # Eyes
        Lcd.drawPixel(px + 7, py + 5, EAGLE_DARK)
        Lcd.drawPixel(px + 12, py + 5, EAGLE_DARK)
        # Wings
        Lcd.drawLine(px + 3, py + 10, px + 6, py + 14, EAGLE_DARK)
        Lcd.drawLine(px + TANK - 4, py + 10,
                     px + TANK - 7, py + 14, EAGLE_DARK)
        # Tail feathers
        Lcd.drawLine(px + 8, py + 17, px + 8, py + TANK - 1, EAGLE_DARK)
        Lcd.drawLine(px + 11, py + 17, px + 11, py + TANK - 1, EAGLE_DARK)
    else:
        # Wreckage
        Lcd.fillRect(px + 3, py + 8, TANK - 6, 10, EAGLE_DEAD)
        Lcd.drawLine(px + 2, py + 2, px + TANK - 3, py + TANK - 3, LOSE)
        Lcd.drawLine(px + TANK - 3, py + 2, px + 2, py + TANK - 3, LOSE)


# ---------------------------------------------------------------------------
# Tank drawing
# ---------------------------------------------------------------------------

def draw_tank(px, py, direction, body_color, turret_color):
    """Draw a TANK x TANK tank at TL (px, py). TANK = 20 with TILE=10.

    Layout (NES-style):
      * treads (left+right):  4 wide, 20 tall, body color
      * cleats (dark notches every 3 rows)
      * body (between treads): 12 wide, 16 tall, lighter color
      * turret square        : 8x8 centered
      * cannon               : 2 wide x 10 long, pointing in `direction`
    """
    Lcd.fillRect(px, py, 4, TANK, body_color)
    Lcd.fillRect(px + TANK - 4, py, 4, TANK, body_color)
    # Tread cleats
    for i in range(0, TANK, 3):
        Lcd.drawPixel(px + 1, py + i, 0x000000)
        Lcd.drawPixel(px + 2, py + i, 0x000000)
        Lcd.drawPixel(px + TANK - 3, py + i, 0x000000)
        Lcd.drawPixel(px + TANK - 2, py + i, 0x000000)
    # Body chassis
    Lcd.fillRect(px + 4, py + 2, TANK - 8, TANK - 4, turret_color)
    # Turret square
    Lcd.fillRect(px + 6, py + 6, 8, 8, body_color)
    cx = px + TANK // 2
    cy = py + TANK // 2
    # Cannon
    if direction == D_UP:
        Lcd.fillRect(cx - 1, py, 2, 10, body_color)
    elif direction == D_DOWN:
        Lcd.fillRect(cx - 1, cy, 2, 10, body_color)
    elif direction == D_LEFT:
        Lcd.fillRect(px, cy - 1, 10, 2, body_color)
    elif direction == D_RIGHT:
        Lcd.fillRect(cx, cy - 1, 10, 2, body_color)


def erase_tank_field(fx, fy):
    Lcd.fillRect(FIELD_X + fx, FIELD_Y + fy, TANK, TANK, FIELD_BG)


# ---------------------------------------------------------------------------
# Bullet drawing
# ---------------------------------------------------------------------------

def draw_bullet_screen(sx, sy, direction):
    if direction == D_UP or direction == D_DOWN:
        Lcd.fillRect(int(sx), int(sy), BULLET_W, BULLET_H, BULLET_C)
    else:
        Lcd.fillRect(int(sx), int(sy), BULLET_H, BULLET_W, BULLET_C)


def erase_bullet_field(fx, fy, direction):
    if direction == D_UP or direction == D_DOWN:
        Lcd.fillRect(FIELD_X + fx, FIELD_Y + fy, BULLET_W, BULLET_H, FIELD_BG)
    else:
        Lcd.fillRect(FIELD_X + fx, FIELD_Y + fy, BULLET_H, BULLET_W, FIELD_BG)


# ---------------------------------------------------------------------------
# Explosion
# ---------------------------------------------------------------------------

def draw_explosion(cx, cy, frame):
    if frame < 2:
        r = 5 + frame * 2
        c = EXPLO_IN
    elif frame < 4:
        r = 10
        c = EXPLO_OUT
    else:
        r = 7
        c = BRICK_DARK
    Lcd.fillCircle(cx, cy, r, c)


_EXPLO_HALF = 11
EXPLO_ERASE_HALF = _EXPLO_HALF
EXPLO_ERASE_SIZE = _EXPLO_HALF * 2 + 1


def erase_explosion(cx, cy):
    Lcd.fillRect(cx - _EXPLO_HALF, cy - _EXPLO_HALF,
                 _EXPLO_HALF * 2 + 1, _EXPLO_HALF * 2 + 1, FIELD_BG)


# ---------------------------------------------------------------------------
# Top HUD strip — 15 px tall, 240 wide.
#
#    [ I-P x 3 ]    [ STG 1 ]    [ SCR  00500 ]    [ enemy dots ]
#    pos 4         pos 64        pos 110           pos 168
#
# Drawn without trailing erases (we re-fillRect the HUD background area on
# every update for simplicity — it only happens when a stat actually changes,
# which is a few times per second at most).
# ---------------------------------------------------------------------------

def _draw_mini_tank(x, y, color):
    """Tiny 7x6 tank icon."""
    # treads
    Lcd.fillRect(x, y, 2, 6, color)
    Lcd.fillRect(x + 5, y, 2, 6, color)
    # body
    Lcd.fillRect(x + 2, y + 1, 3, 4, color)
    # cannon
    Lcd.fillRect(x + 3, y - 1, 1, 2, color)


def _draw_flag(x, y, color, pole_color):
    """7x9 flag icon."""
    Lcd.drawLine(x, y, x, y + 8, pole_color)
    Lcd.fillRect(x + 1, y, 6, 4, color)
    Lcd.drawPixel(x + 6, y + 3, BG)  # triangular notch
    Lcd.drawPixel(x + 5, y + 4, BG)


def draw_hud(state):
    Lcd.fillRect(0, 0, W, HUD_H, HUD_BG)
    Lcd.drawLine(0, HUD_H - 1, W - 1, HUD_H - 1, HUD_LINE)
    Lcd.setFont(FONT_S)

    # --- LIVES: "1P" label + mini tank + "xN"
    Lcd.setTextColor(DIM, HUD_BG)
    Lcd.setCursor(2, 1)
    Lcd.print("1P")
    _draw_mini_tank(22, 4, PLAYER_TUR)
    Lcd.setTextColor(PLAYER_TUR, HUD_BG)
    Lcd.setCursor(32, 1)
    Lcd.print("x" + str(state["lives"]))

    # --- STAGE
    Lcd.setTextColor(DIM, HUD_BG)
    Lcd.setCursor(56, 1)
    Lcd.print("ST")
    Lcd.setTextColor(TEXT, HUD_BG)
    Lcd.setCursor(72, 1)
    Lcd.print(str(state["stage"]))

    # --- SCORE (right-leaning)
    Lcd.setTextColor(DIM, HUD_BG)
    Lcd.setCursor(90, 1)
    Lcd.print("SC")
    Lcd.setTextColor(TITLE_FG, HUD_BG)
    score_str = "{:05d}".format(state["score"])
    Lcd.setCursor(108, 1)
    Lcd.print(score_str)

    # --- ENEMIES: column of mini icons (3px squares, packed grid).
    # 20 max, in 2 rows × 10 cols, fits in ~80 px wide.
    n = state["enemies_left"]
    base_x = 150
    base_y = 2
    cell = 4   # 3-px square + 1-px gap
    for i in range(20):
        col = i % 10
        row = i // 10
        ex = base_x + col * cell
        ey = base_y + row * (cell + 1)
        if i < n:
            Lcd.fillRect(ex, ey, 3, 3, ENEMY_BASIC)
        else:
            # Already-killed slot: empty cell (already black from clear above)
            pass


# ---------------------------------------------------------------------------
# Overlays
# ---------------------------------------------------------------------------

def show_message(line1, line2, color1, color2, hint):
    bx = 16
    bw = W - 32
    by = 28
    bh = 80
    Lcd.fillRoundRect(bx, by, bw, bh, 5, 0x101020)
    Lcd.drawRoundRect(bx, by, bw, bh, 5, color1)
    Lcd.setFont(FONT_M)
    Lcd.setTextColor(color1, 0x101020)
    mw = Lcd.textWidth(line1, FONT_M)
    Lcd.setCursor((W - mw) // 2, by + 8)
    Lcd.print(line1)
    Lcd.setFont(FONT_S)
    Lcd.setTextColor(color2, 0x101020)
    mw2 = Lcd.textWidth(line2, FONT_S)
    Lcd.setCursor((W - mw2) // 2, by + 34)
    Lcd.print(line2)
    Lcd.setTextColor(DIM, 0x101020)
    hw = Lcd.textWidth(hint, FONT_S)
    Lcd.setCursor((W - hw) // 2, by + 58)
    Lcd.print(hint)


def show_start(stage):
    Lcd.fillRect(0, 0, W, H, BG)
    Lcd.setFont(FONT_L)
    Lcd.setTextColor(TITLE_FG, BG)
    msg = "BATTLE CITY"
    mw = Lcd.textWidth(msg, FONT_L)
    Lcd.setCursor((W - mw) // 2, 4)
    Lcd.print(msg)

    Lcd.setFont(FONT_S)
    Lcd.setTextColor(TEXT, BG)
    lines = [
        "Hold ; , . / = move",
        "Release = stop",
        "Space = fire",
        "Defend the EAGLE",
        "Stage " + str(stage),
    ]
    y = 40
    for ln in lines:
        if not ln:
            y += 6
            continue
        w = Lcd.textWidth(ln, FONT_S)
        Lcd.setCursor((W - w) // 2, y)
        Lcd.print(ln)
        y += 14

    Lcd.setTextColor(DIM, BG)
    h = "ENTER start    ESC quit"
    hw = Lcd.textWidth(h, FONT_S)
    Lcd.setCursor((W - hw) // 2, H - 14)
    Lcd.print(h)
