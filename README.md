# Cardputer Launcher

A small home-grown "launcher OS" for the **M5Stack Cardputer-Adv** running
MicroPython. Boots into a paged app menu on the 1.14" LCD with a
status bar (WiFi / battery / clock), supports nested category folders,
and ships with a handful of useful apps: a real BLE HID keyboard, a
USB-Morse beacon + decoder, a crypto ticker, an env-sensor reader, a
sysinfo browser, two minigames, and a terminal-style Discord poster.

> [中文文档 → README-CN.md](README-CN.md)

![cardputer](https://docs.m5stack.com/en/core/Cardputer-Adv)

## Why

The stock UiFlow2 launcher is fine for visual blocks, but if you write
plain MicroPython you want:

- A folder of `apps/<name>/app.py` files where each just exposes
  `def run(): ...` — no boilerplate, no plist, no manifest.
- A status bar that shows WiFi state, IP, current SSID, battery percent
  with hysteresis (Cardputer-Adv has no PMIC — only the ADC voltage),
  and Beijing-time clock from NTP.
- Auto-roaming WiFi between known APs (home / phone hotspot / office)
  without prompting for a password each time.
- A working **BLE HID keyboard** that actually pairs with macOS,
  iOS, Android and Windows (this is non-trivial on this firmware —
  see the BLE notes below).
- An ES8311 codec power-down sequence that doesn't leave the
  NS4150B amp hissing forever (M5Unified's own disable callback is
  empty for this board).

This repo is the answer to all of those.

## Hardware

- **M5Stack Cardputer-Adv** (Stamp-S3A, ESP32-S3FN8, 8 MB flash)
- Stock UiFlow2 MicroPython firmware (tested on v1.27.0-dirty,
  build `M5STACK_CardputerADV`)

The launcher uses M5Unified bindings for the LCD, the matrix keyboard,
the ES8311 codec, and the speaker. ENV-III hat optional (used by
`apps/sensor/env`).

## Installation

1. Make sure your Cardputer-Adv runs UiFlow2 MicroPython firmware.
2. Plug in via USB-C. macOS sees `/dev/cu.usbmodem*` (PID 0x1001).
3. Install [`mpremote`](https://docs.micropython.org/en/latest/reference/mpremote.html):
   ```
   pip install mpremote
   ```
4. Copy the launcher into `/flash`:
   ```bash
   cd launcher
   mpremote cp main.py :/flash/main.py
   mpremote cp launcher.py :/flash/launcher.py
   mpremote cp -r apps :/flash/apps
   mpremote cp -r libs :/flash/libs
   ```
5. Soft-reset the device (Ctrl-D in REPL or just power-cycle). The
   launcher main menu should come up.

On first boot WiFi will be empty. Open the **WiFi** app under
*system → wifi*, scan, pick your network, type the password — it'll be
saved in `/flash/wifi.json` and auto-reconnected on subsequent boots.

## Built-in apps

| App | What it does |
|-----|---|
| `clock` | Big Beijing-time clock with NTP sync + WiFi state. |
| `morse` | Morse-code beacon. Three modes (cycle with ←/→): fullscreen LCD flash (decoded by camera), 700 Hz audio sidetone (decoded by mic), audio decoder (mic input). The companion web decoder lives in `apps/morse/decoder.html`. |
| `prices` | Crypto price ticker (uses `data-api.binance.vision` so it works from networks where binance.com is blocked). |
| `discord` | Type a message, hit Enter, it POSTs to a configurable webhook. Useful as a one-line nudge to yourself or a Discord channel. |
| `sensor/env` | Reads SHT30 (temp + humidity) and QMP6988 (pressure) from an attached ENV-III hat. 5 Hz refresh + trend arrows. |
| `system/wifi` | Multi-SSID WiFi manager. Shows currently connected SSID + IP, marks saved networks with `*` and the current with `>`. Skips the password prompt for known APs. Auto-roams to whichever known AP is in range on boot. |
| `system/sysinfo` | Live system info — uptime, CPU MHz, MCU temperature, RAM free / used, full 8 MB flash partition map, battery voltage, WiFi SSID / IP / RSSI / MAC, BLE MAC, MicroPython build. |
| `btmacro` | **BLE HID keyboard.** Pairs with macOS, iOS, Android, Windows. Bonded — re-pair only on first connect. Live-typing mode forwards every Cardputer keystroke (including arrows / Enter / shifted symbols) to the host. Includes a Cmd+Ctrl+Q lock-screen macro. |
| `games/snake` | Snake. |
| `games/bounce` | Pong-ish bouncing ball. |

## Building your own app

```
apps/myapp/app.py        # required — must export `def run(): ...`
apps/myapp/icon.py       # optional — exports `def draw(lcd, x, y, size, on_dark)`
```

Categories are bare folders that contain at least one `<app>/app.py`:

```
apps/games/snake/app.py
apps/games/bounce/app.py
```

The launcher discovers them at boot. `run()` should poll
`MatrixKeyboard().get_key()` and return on `KEYCODE_ESC` to come back
to the menu.

A minimal hello-world:

```python
import time
from M5 import Lcd
from hardware.matrix_keyboard import MatrixKeyboard
from startup.cardputeradv.framework import KeyCode

def run():
    Lcd.clear(0x000000)
    Lcd.setCursor(10, 30)
    Lcd.print("Hello from my app")
    kb = MatrixKeyboard()
    while kb.get_key() != KeyCode.KEYCODE_ESC:
        time.sleep_ms(40)
```

## Notes & gotchas

These are surprises this codebase has hit and worked around. If you fork
this and find yourself debugging similar issues, start here.

### BLE HID on this firmware

It works on macOS / iOS / Android / Windows, but every one of these had
to be set up correctly before the host accepts the device:

1. **`ble.config(bond / le_secure / mitm / io)` MUST come before
   `active(True)`** — those keys aren't returned by `config()`'s getter
   so it looks like they don't exist, but the setters are real. Setting
   them after `active(True)` silently no-ops on NimBLE.
2. **Set `FLAG_READ_ENCRYPTED = 0x0200` on the input report
   characteristic.** Without it the host never initiates SMP and HID
   class drivers ignore the notification stream.
3. **Proactively call `ble.gap_pair(conn_handle)` from
   `_IRQ_CENTRAL_CONNECT`.** macOS specifically does GATT discovery,
   sees the encrypted-required characteristic, and disconnects without
   ever sending a Pairing Request. We have to send it.
4. Use the public BD_ADDR — don't set `addr_mode=2` (RPA) for first
   pair. Random addresses make macOS' Bluetooth UI unresponsive
   ("Nearby Devices" entry stuck spinning, click does nothing).
5. The bond-key callbacks (`_IRQ_GET_SECRET` / `_IRQ_SET_SECRET`) must
   key by `(sec_type: int, key: bytes)` tuple — see the canonical
   `examples/bluetooth/ble_bonding_peripheral.py`. Stored in
   `/flash/ble_bonds.json` (base64-encoded).
6. On exit, call `ble.active(False)` not just `gap_advertise(None)` —
   otherwise the radio keeps broadcasting and the host sees a ghost
   "Nearby Device" entry that never goes away.

### Audio (ES8311 codec + NS4150B amp)

- M5Unified's `_speaker_enabled_cb_cardputer_adv` disable path is an
  empty array on this board — `M5.Speaker.end()` does nothing, the DAC
  stays powered, and the amp idle-noise gets amplified into an audible
  hiss whenever the WiFi radio is active (RF couples into the
  always-on amp through the PCB).
- The fix is to send a real ES8311 power-down sequence over I2C — see
  `_silence_codec()` in `launcher.py` and `_spk_off()` in
  `apps/morse/app.py`. Values come from Espressif's ESP-ADF driver.
- Importantly: **don't** silence the codec at boot if nothing has
  enabled it. The chip's power-on default is quiet. Writing your own
  "muted" registers can leave it noisier than the default.

### Battery (Cardputer-Adv has no PMIC)

`M5.Power.getBatteryLevel()` is unreliable here because the M5Unified
driver assumes a PMIC chip the board doesn't have. Only
`getBatteryVoltage()` works. The launcher derives a percentage with
**hysteresis** (charging threshold 4.22 V, discharging threshold 4.10 V)
to avoid flicker around 100% under USB power.

### WiFi (2.4 GHz only)

ESP32-S3 is single-band. iPhone hotspots default to 5 GHz; turn on
"Maximize Compatibility" in iOS hotspot settings or the Cardputer
won't see it.

### IR LED

The Cardputer-Adv's IR LED is GPIO-direct (no transistor), ~12 mA
peak — useful range under 30 cm. Plus MicroPython's
`time.sleep_us()` overhead is ~30 µs/call, so any protocol with long
frames (most AC remotes) drifts beyond the receiver's tolerance window.
TV remotes (NEC, ~70 pulses) work; AC remotes don't. If you need
reliable IR, write that app in Arduino + IRremote, not MicroPython.

## Project layout

```
launcher/
  main.py               # boot entry — just imports launcher.run()
  launcher.py           # menu + status bar + WiFi auto-roam + codec helpers
  apps/
    btmacro/            # BLE HID keyboard
    clock/              # full-screen Beijing clock
    discord/            # webhook poster
    games/
      bounce/
      snake/
    morse/              # flash + audio Morse beacon, web decoder
      decoder.html      # camera + microphone Morse decoder UI
      serve.py          # HTTPS server for the decoder page
      gencert.sh        # generates the self-signed TLS cert
    prices/             # crypto ticker
    sensor/
      env/              # SHT30 + QMP6988 (ENV-III hat)
    system/
      sysinfo/          # live system info
      wifi/             # multi-SSID WiFi manager
  libs/                 # shared drivers (SHT30, QMP6988, BMI270)
```

## Hardware reference

- LCD: 240 × 135, ST7789-family, controlled via M5.Lcd
- Keyboard: TCA8418 matrix scanner, I2C addr `0x34`, **I2C peripheral 1**
  on SDA=GPIO 8 / SCL=GPIO 9, 400 kHz. Don't create your own `I2C(0)`
  on those pins — IO mux gets clobbered and the keyboard goes dead.
- IMU: BMI270 at I2C addr `0x69`, same bus
- Codec: ES8311 at I2C addr `0x18`, same bus, plus NS4150B speaker amp
- Battery: ADC on GPIO 10 with 2:1 divider — see notes above
- IR LED: see notes above

## License

MIT. Do whatever you want; no warranty.

If you publish a fork, a link back is appreciated but not required.
