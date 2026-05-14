def draw(lcd, x, y, size, on_dark):
    """Battle City tank: chassis + turret + bullet streak."""
    bg = 0x000000 if on_dark else 0xFFFFFF
    body = 0xC0A050     # tan/khaki tank
    tread = 0x604030
    turret = 0xFFE060
    bullet = 0xFFFFFF
    cx = x + size // 2
    cy = y + size // 2

    # Chassis body fills most of icon, leaves room for turret on top
    bw = size - 4
    bh = size - 6
    bx = x + 2
    by = y + 4
    lcd.fillRect(bx, by, bw, bh, body)
    # Treads (top + bottom strip darker)
    lcd.fillRect(bx, by, bw, 2, tread)
    lcd.fillRect(bx, by + bh - 2, bw, 2, tread)
    # Turret square in middle
    tw = max(3, size // 3)
    th = max(3, size // 3)
    lcd.fillRect(cx - tw // 2, cy - th // 2, tw, th, turret)
    # Cannon barrel pointing up
    cannon_h = max(2, size // 4)
    lcd.fillRect(cx - 1, y + 1, 2, cannon_h + 1, turret)
    # Bullet just above cannon
    lcd.fillRect(cx - 1, y, 2, 1, bullet)
