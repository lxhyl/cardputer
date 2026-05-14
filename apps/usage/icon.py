def draw(lcd, x, y, size, on_dark):
    """Orange Claude mascot — small rounded creature with two eye gaps."""
    accent = 0xE07550
    bg = 0x000000 if on_dark else 0xFFFFFF
    lcd.fillRect(x, y, size, size, bg)
    rows = (
        0b0011111111100,
        0b0111111111110,
        0b1111111111111,
        0b1101111111011,
        0b1101111111011,
        0b1111111111111,
        0b0111111111110,
        0b0011111111100,
        0b0001000010000,
    )
    w = 13
    h = len(rows)
    # Pixel scale so the mascot fills the icon.
    m = max(1, min(size // w, size // h))
    actual_w = m * w
    actual_h = m * h
    ox = x + (size - actual_w) // 2
    oy = y + (size - actual_h) // 2
    for r, bits in enumerate(rows):
        for c in range(w):
            if (bits >> (w - 1 - c)) & 1:
                lcd.fillRect(ox + c * m, oy + r * m, m, m, accent)
