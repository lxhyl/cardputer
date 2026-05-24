def draw(lcd, x, y, size, on_dark):
    """Concentric range rings around a center pin — GPS-style locator."""
    ring = 0x64C8FF
    pin = 0xFFD040

    cx = x + size // 2
    cy = y + size // 2

    r_outer = max(4, size // 2 - 2)
    r_mid = max(3, size // 3)
    r_inner = max(2, size // 5)

    lcd.drawCircle(cx, cy, r_outer, ring)
    lcd.drawCircle(cx, cy, r_mid, ring)
    lcd.drawCircle(cx, cy, r_inner, ring)
    lcd.fillCircle(cx, cy, max(1, size // 8), pin)
