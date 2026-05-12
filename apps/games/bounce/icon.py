def draw(lcd, x, y, size, on_dark):
    """Yellow ball above a blue paddle."""
    ball = 0xFFD040
    paddle = 0x64A0FF
    edge = 0x000000 if on_dark else 0xFFFFFF
    cx = x + size // 2
    paddle_w = size - 4
    paddle_h = max(2, size // 6)
    paddle_y = y + size - paddle_h - 1
    # Paddle (rounded blue bar near bottom)
    lcd.fillRoundRect(x + 2, paddle_y, paddle_w, paddle_h, 1, paddle)
    # Ball (yellow circle above paddle, slightly left of center for motion feel)
    ball_r = max(2, size // 5)
    bx = cx - 1
    by = y + ball_r + 2
    lcd.fillCircle(bx, by, ball_r, ball)
    # Tiny dotted trajectory line
    for i in range(2):
        py = by + ball_r + 2 + i * 3
        lcd.drawPixel(bx + 1 + i, py, edge)
