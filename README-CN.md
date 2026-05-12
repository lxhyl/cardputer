# Cardputer Launcher

给 **M5Stack Cardputer-Adv** 写的一个小型「启动器系统」，跑在 MicroPython 上。
开机进入分页 app 菜单（1.14" LCD 上），带状态栏（WiFi / 电池 / 时钟），
支持嵌套分类文件夹，自带一堆实用 app：能正常配对 Mac 的蓝牙 HID 键盘、
USB 摩斯电码灯塔 + 网页解码器、加密币行情、ENV 传感器读数、系统信息浏览、
两个小游戏、终端式 Discord 推送。

> [English README → README.md](README.md)

![Cardputer-Adv 运行 launcher](https://raw.githubusercontent.com/lxhyl/cardputer/main/public/IMG_2206.JPG)

## 为啥写这个

UiFlow2 自带的启动器对图形化编程够用，但如果你写纯 MicroPython 你想要：

- 一个 `apps/<name>/app.py` 的目录，每个 app 只需要 `def run(): ...`，
  没有样板代码、没有 plist、没有 manifest。
- 状态栏显示 WiFi 状态、IP、当前 SSID、带滞回的电池百分比（Cardputer-Adv
  没有 PMIC，只能从 ADC 读电压），北京时间从 NTP 同步。
- 多个已知 WiFi（家 / 手机热点 / 公司）之间自动漫游，不用每次输密码。
- 能真的连上 macOS / iOS / Android / Windows 的 **BLE HID 键盘**
  （这固件上做这件事一点不简单 —— 见下面 BLE 笔记）。
- 一套关 ES8311 codec 不让 NS4150B 功放持续嘶嘶响的电源关闭序列
  （M5Unified 给这板子的 disable callback 是空的）。

这个仓库就是上面这些事情的答案。

## 硬件

- **M5Stack Cardputer-Adv**（Stamp-S3A，ESP32-S3FN8，8 MB flash）
- 官方 UiFlow2 MicroPython 固件（实测 v1.27.0-dirty，
  build `M5STACK_CardputerADV`）

启动器用 M5Unified 操作 LCD、键盘矩阵、ES8311 codec、扬声器。
ENV-III hat 可选（`apps/sensor/env` 用）。

## 安装

1. 确认 Cardputer-Adv 跑的是 UiFlow2 MicroPython 固件
2. USB-C 插上 Mac / Linux。macOS 上看到 `/dev/cu.usbmodem*`（PID 0x1001）
3. 装 [`mpremote`](https://docs.micropython.org/en/latest/reference/mpremote.html)：
   ```
   pip install mpremote
   ```
4. 把 launcher 拷到 `/flash`：
   ```bash
   cd launcher
   mpremote cp main.py :/flash/main.py
   mpremote cp launcher.py :/flash/launcher.py
   mpremote cp -r apps :/flash/apps
   mpremote cp -r libs :/flash/libs
   ```
5. 软复位（REPL 里 Ctrl-D 或者直接断电重启），主菜单应该出来了

首次启动 WiFi 是空的。进 *system → wifi*，扫描 → 选你的网络 → 输密码，
存到 `/flash/wifi.json`，下次开机自动重连。

## 自带 app 列表

| App | 干啥的 |
|-----|---|
| `clock` | 北京时间大字时钟 + NTP 同步 + WiFi 状态 |
| `morse` | 摩斯电码灯塔。三种模式（←/→ 切）：全屏 LCD 闪烁（摄像头解码）、700 Hz 音频侧音（麦克风解码）、音频解码器（麦克风输入）。配套网页解码器在 `apps/morse/decoder.html` |
| `prices` | 加密币行情（用 `data-api.binance.vision`，国内能访问，binance.com 被墙也能用）|
| `sensor/env` | 读外接 ENV-III hat 上的 SHT30（温湿度）+ QMP6988（气压），5 Hz 刷新 + 趋势箭头 |
| `system/wifi` | 多 SSID WiFi 管理器。显示当前连接 SSID + IP，已保存网络前缀 `*`，当前连接前缀 `>`。已知 SSID 跳过密码框。开机自动漫游到信号最强的已知 AP |
| `system/sysinfo` | 实时系统信息 —— 运行时长、CPU 频率、MCU 温度、RAM 已用 / 剩余、完整 8 MB flash 分区表、电池电压、WiFi SSID/IP/RSSI/MAC、BLE MAC、MicroPython 版本 |
| `btmacro` | **BLE HID 键盘**。能连 macOS、iOS、Android、Windows。带 bond 持久化 —— 配对一次永久记住。Live typing 模式把 Cardputer 上每个键（包括方向键 / Enter / 带 shift 的符号）转发到 host。带一个 Cmd+Ctrl+Q 锁屏 macro |
| `games/snake` | 贪吃蛇 |
| `games/bounce` | 类乒乓球 |

## 自己写一个 app

```
apps/myapp/app.py        # 必需 —— 必须导出 def run(): ...
apps/myapp/icon.py       # 可选 —— 导出 def draw(lcd, x, y, size, on_dark)
```

分类文件夹是任何包含至少一个 `<app>/app.py` 子目录的裸文件夹：

```
apps/games/snake/app.py
apps/games/bounce/app.py
```

启动器开机自动扫描。`run()` 里轮询 `MatrixKeyboard().get_key()`，
检测到 `KEYCODE_ESC` 就 return 回主菜单。

最小 hello-world：

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

## 笔记和踩坑记录

下面是这个 repo 真踩过的坑和解决方案。如果你 fork 之后遇到类似问题先看这里。

### 这固件上的 BLE HID

能连 macOS / iOS / Android / Windows，但每个都得正确配置才行：

1. **`ble.config(bond / le_secure / mitm / io)` 必须在 `active(True)` 之前调**
   —— 这些 key 通过 `config()` 的 getter 看不到，会让你以为没编译进去，
   但 setter 是真实的。`active(True)` 之后再 config 在 NimBLE 上静默 no-op。
2. **input report 字符必须带 `FLAG_READ_ENCRYPTED = 0x0200`**。
   没这个 flag host 永远不发起 SMP，HID 类驱动直接忽略所有 notify。
3. **在 `_IRQ_CENTRAL_CONNECT` 里立刻调 `ble.gap_pair(conn_handle)`**。
   macOS 特别的，它做完 GATT 探索看到加密字符之后会**直接断开**，
   不主动发 Pairing Request。需要我们这边主动发起。
4. 用默认的公共 BD_ADDR ——**不要**设 `addr_mode=2`（RPA）做首次配对。
   随机地址会让 macOS 蓝牙 UI 卡死（"Nearby Devices" 条目转圈，点都点不动）。
5. Bond 密钥回调（`_IRQ_GET_SECRET` / `_IRQ_SET_SECRET`）必须用
   `(sec_type: int, key: bytes)` 元组当 key —— 见官方
   `examples/bluetooth/ble_bonding_peripheral.py`。存到
   `/flash/ble_bonds.json`（base64 编码）。
6. 退 app 时**必须**调 `ble.active(False)`，不能只 `gap_advertise(None)`，
   否则 radio 仍在广播，host 上看到的「Nearby Device」鬼影永远不消失。

### 音频（ES8311 codec + NS4150B 功放）

- M5Unified 的 `_speaker_enabled_cb_cardputer_adv` 在这板子上的
  disable callback 是空数组 —— `M5.Speaker.end()` 什么都没干，DAC 还在
  通电，功放放大底噪 + WiFi 工作时 RF 通过 PCB 走线耦合进来变可听噪音。
- 修法是通过 I2C 直接发完整的 ES8311 power-down 序列 —— 见
  `launcher.py` 的 `_silence_codec()` 和 `apps/morse/app.py` 的 `_spk_off()`。
  寄存器值来自 Espressif ESP-ADF 驱动。
- 重要：**如果没人启用过 codec 就不要在启动时调 silence**。芯片出厂默认
  就是静的，你写自定义"muted"值反而会让它比默认还吵。

### 电池（Cardputer-Adv 没有 PMIC）

`M5.Power.getBatteryLevel()` 在这板子上不可靠，因为 M5Unified 驱动假设
有 PMIC 芯片但实际没有。只有 `getBatteryVoltage()` 准。launcher
推算百分比时用**滞回阈值**（充电 4.22 V，放电 4.10 V），避免 USB
供电时在 100% 附近抖动。

### WiFi（仅 2.4 GHz）

ESP32-S3 单频。iPhone 热点默认 5 GHz；要在 iOS 热点设置里开
"最大兼容性" Cardputer 才能扫到。

### 红外 LED

Cardputer-Adv 的 IR LED 是 GPIO 直驱（没有三极管），峰值 ~12 mA，
有效距离 30 cm 以内。再加上 MicroPython 的 `time.sleep_us()`
开销 ~30 µs/调用，长帧协议（大多数空调遥控）累计漂移超过接收器
容差。TV 遥控（NEC，~70 脉冲）能用；空调遥控不行。如果你需要稳定
红外控制，那个 app 应该用 Arduino + IRremote 写，不是 MicroPython。

## 项目布局

```
launcher/
  main.py               # 入口 —— 只 import launcher 然后跑 launcher.run()
  launcher.py           # 菜单 + 状态栏 + WiFi 漫游 + codec 关电源
  apps/
    btmacro/            # BLE HID 键盘
    clock/              # 全屏北京时间
    games/
      bounce/
      snake/
    morse/              # 闪光 + 音频摩斯灯塔，配套网页解码器
      decoder.html      # 摄像头 + 麦克风摩斯解码器 UI
      serve.py          # 解码器页面的 HTTPS server
      gencert.sh        # 生成自签 TLS 证书
    prices/             # 加密币行情
    sensor/
      env/              # SHT30 + QMP6988（ENV-III hat）
    system/
      sysinfo/          # 实时系统信息
      wifi/             # 多 SSID WiFi 管理
  libs/                 # 共享驱动（SHT30、QMP6988、BMI270）
```

## 硬件信息

- LCD：240 × 135，ST7789 系列，通过 M5.Lcd 操作
- 键盘：TCA8418 矩阵扫描芯片，I2C 地址 `0x34`，挂 **I2C peripheral 1**
  上，SDA=GPIO 8 / SCL=GPIO 9，400 kHz。**不要**在那两个引脚上自己
  创建 `I2C(0)` —— IO mux 会被破坏，键盘失联
- IMU：BMI270，I2C 地址 `0x69`，同 bus
- Codec：ES8311，I2C 地址 `0x18`，同 bus，加上 NS4150B 扬声器功放
- 电池：ADC 在 GPIO 10 上，2:1 分压 —— 见上面笔记
- IR LED：见上面笔记

## License

MIT。随便用，不提供任何担保。

如果你 fork 了发布出去，给个原项目链接是友好的，但不强制。
