import math


def draw(lcd, x, y, size, on_dark):
    """Satellite icon: tilted elliptical orbit with satellite body."""
    acc = 0x40A0FF     # orbit arc colour
    sat = 0xFFFFFF if on_dark else 0x000000
    earth = 0x2255CC

    cx = x + size // 2
    cy = y + size // 2

    # Earth (small filled circle at centre)
    er = max(2, size // 6)
    lcd.fillCircle(cx, cy, er, earth)

    # Orbit (tilted ellipse)
    orx = size // 2 - 2
    ory = max(2, size // 4)
    lcd.drawEllipse(cx, cy, orx, ory, acc)

    # Satellite body at upper-right of orbit
    angle = -40.0 * math.pi / 180.0
    sx = cx + int(orx * math.cos(angle))
    sy = cy + int(ory * math.sin(angle))
    dot_r = max(1, size // 9)
    lcd.fillCircle(sx, sy, dot_r, sat)

    # Solar panels (horizontal bar through satellite)
    pan = max(2, size // 5)
    lcd.drawLine(sx - pan, sy, sx + pan, sy, acc)
