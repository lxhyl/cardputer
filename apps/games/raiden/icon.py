def draw(lcd, x, y, size, on_dark):
    """Raiden-style: player ship + enemy + bullets."""
    bg   = 0x000000 if on_dark else 0xFFFFFF
    ship = 0x40C8FF
    blt  = 0xFFFF40
    foe  = 0xFF4040
    cx = x + size // 2
    # Player ship (triangle, tip up)
    ty = y + size - 4
    sh = size // 3
    sw = size // 4
    lcd.fillTriangle(cx, ty - sh, cx - sw, ty, cx + sw, ty, ship)
    # Player bullet streak
    lcd.fillRect(cx - 1, ty - sh - 5, 2, 5, blt)
    # Enemy (small diamond near top)
    ey = y + 4
    lcd.fillTriangle(cx, ey, cx - 3, ey + 4, cx + 3, ey + 4, foe)
    lcd.fillTriangle(cx, ey + 8, cx - 3, ey + 4, cx + 3, ey + 4, foe)
