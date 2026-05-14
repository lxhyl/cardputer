def draw(lcd, x, y, size, on_dark):
    """English-learning app icon — large 'A' over a smaller 'B' suggesting
    a vocabulary card. Colors chosen for either light or dark background."""
    bg = 0x004488 if on_dark else 0x6699FF
    fg = 0xFFFFFF
    accent = 0xFFD040
    lcd.fillRect(x, y, size, size, bg)
    # Bold 'A' top-left, 'B' bottom-right
    # Draw 'A' as two diagonals and a crossbar
    s = size
    # 'A' bounding box: top-left quarter, slightly enlarged
    ax = x + 1
    ay = y + 2
    ah = (s * 5) // 8
    aw = (s * 5) // 8
    cx = ax + aw // 2
    lcd.drawLine(ax, ay + ah, cx, ay, fg)
    lcd.drawLine(ax + 1, ay + ah, cx + 1, ay, fg)
    lcd.drawLine(cx, ay, ax + aw - 1, ay + ah, fg)
    lcd.drawLine(cx + 1, ay, ax + aw, ay + ah, fg)
    # Crossbar
    lcd.drawLine(ax + aw // 4, ay + (ah * 2) // 3,
                 ax + (aw * 3) // 4, ay + (ah * 2) // 3, fg)
    # 'B' bottom-right as a small block hint
    bx = x + s - (s * 3) // 8 - 1
    by = y + s - (s * 3) // 8 - 1
    bw = (s * 3) // 8
    bh = (s * 3) // 8
    lcd.drawRect(bx, by, bw, bh, accent)
    lcd.drawLine(bx, by + bh // 2, bx + bw - 1, by + bh // 2, accent)
