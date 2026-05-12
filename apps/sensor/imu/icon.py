def draw(lcd, x, y, size, on_dark):
    """Tri-axis indicator: red X (right), green Y (up), blue Z (diagonal)
    radiating from a small origin dot."""
    edge = 0xFFFFFF if on_dark else 0x000000
    r = 0xFF6464   # X
    g = 0x64FF64   # Y
    b = 0x64A0FF   # Z
    cx = x + size // 3 + 1
    cy = y + size - size // 3 - 1
    arm = size - 4
    # Y axis: straight up
    lcd.drawLine(cx, cy, cx, cy - arm, g)
    lcd.drawLine(cx + 1, cy, cx + 1, cy - arm, g)
    lcd.fillTriangle(cx - 2, cy - arm + 3, cx + 3, cy - arm + 3, cx, cy - arm - 1, g)
    # X axis: straight right
    lcd.drawLine(cx, cy, cx + arm, cy, r)
    lcd.drawLine(cx, cy + 1, cx + arm, cy + 1, r)
    lcd.fillTriangle(cx + arm - 3, cy - 2, cx + arm - 3, cy + 3, cx + arm + 1, cy, r)
    # Z axis: diagonal up-left (depth)
    z_len = arm * 3 // 4
    zx = cx - z_len // 2
    zy = cy - z_len // 2
    lcd.drawLine(cx, cy, zx, zy, b)
    lcd.drawLine(cx - 1, cy + 1, zx - 1, zy + 1, b)
    lcd.fillTriangle(zx + 2, zy + 1, zx + 1, zy + 2, zx - 2, zy - 2, b)
    # Origin dot
    lcd.fillCircle(cx, cy, 1, edge)
