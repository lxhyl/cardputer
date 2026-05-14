def draw(lcd, x, y, size, on_dark):
    """Stylised QR motif: top-left finder + sparse data modules."""
    fg = 0xFFFFFF if on_dark else 0x000000
    bg = 0x000000 if on_dark else 0xFFFFFF
    # 9-module grid scaled to the icon. m=2 for the launcher's 18px slot.
    n = 9
    m = max(1, size // n)
    actual = m * n
    ox = x + (size - actual) // 2
    oy = y + (size - actual) // 2
    lcd.fillRect(x, y, size, size, bg)
    pattern = (
        0b111111101,
        0b100000101,
        0b101110100,
        0b101110101,
        0b101110100,
        0b100000101,
        0b111111101,
        0b000000000,
        0b101010101,
    )
    for r in range(n):
        bits = pattern[r]
        for c in range(n):
            if (bits >> (n - 1 - c)) & 1:
                lcd.fillRect(ox + c * m, oy + r * m, m, m, fg)
