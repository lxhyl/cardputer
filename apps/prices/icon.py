def draw(lcd, x, y, size, on_dark):
    """Stack of 3 coins (Bitcoin orange, Ethereum blue, mint green)."""
    btc = 0xF7931A
    eth = 0x627EEA
    grn = 0x00DD66
    edge = 0x000000 if on_dark else 0xFFFFFF
    cx = x + size // 2
    rx = size // 2 - 1
    ry = max(2, size // 6)
    bottom_y = y + size - ry - 1
    gap = max(2, ry + 1)
    # Bottom coin (BTC) — largest
    lcd.fillEllipse(cx, bottom_y, rx, ry, btc)
    lcd.drawEllipse(cx, bottom_y, rx, ry, edge)
    # Middle coin (ETH)
    lcd.fillEllipse(cx, bottom_y - gap, rx - 1, ry, eth)
    lcd.drawEllipse(cx, bottom_y - gap, rx - 1, ry, edge)
    # Top coin (PENDLE / generic) — smallest
    lcd.fillEllipse(cx, bottom_y - 2 * gap, rx - 2, ry, grn)
    lcd.drawEllipse(cx, bottom_y - 2 * gap, rx - 2, ry, edge)
