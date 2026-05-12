def draw(lcd, x, y, size, on_dark):
    """Analog clock face — white dial, hour + minute hands at ~10:10."""
    face = 0xFFFFFF
    edge = 0x000000
    hand = 0x000000
    cx = x + size // 2
    cy = y + size // 2
    r = size // 2 - 1
    lcd.fillCircle(cx, cy, r, face)
    lcd.drawCircle(cx, cy, r, edge if not on_dark else 0x444444)
    # 12 / 3 / 6 / 9 tick marks
    for dx, dy in ((0, -r + 1), (r - 1, 0), (0, r - 1), (-r + 1, 0)):
        lcd.drawPixel(cx + dx, cy + dy, edge)
    # Hour hand (shorter): pointing roughly to "10"
    lcd.drawLine(cx, cy, cx - max(2, r // 2), cy - max(1, r // 4), hand)
    # Minute hand (longer): pointing roughly to "2"
    lcd.drawLine(cx, cy, cx + max(2, r // 2 + 1), cy - max(2, r // 2), hand)
    # Center dot
    lcd.fillCircle(cx, cy, 1, hand)
