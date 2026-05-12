def draw(lcd, x, y, size, on_dark):
    """Three arcs over a dot — same shape as the status bar WiFi icon."""
    color = 0xFFFFFF if on_dark else 0x000000
    cx = x + size // 2
    cy = y + size - 2   # center the radial origin near the bottom
    r1 = size - 3
    r2 = max(2, size * 2 // 3)
    r3 = max(1, size // 3)
    lcd.fillArc(cx, cy, r1, r1 - 2, 225, 315, color)
    lcd.fillArc(cx, cy, r2, r2 - 2, 225, 315, color)
    lcd.fillCircle(cx, cy - 1, 2, color)
