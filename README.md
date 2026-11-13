本项目 fork 自 [gxcreator/ktmicro-tools](https://github.com/gxcreator/ktmicro-tools)。

## 本机实测芯片信息

用 `python ktmicro.py state` 读到的真实设备（**与 `KT_USB_PROTOCOL.md` 里记录的默认
VID:PID `31B2:0111` 不同**，说明出厂固件已烧入定制配置）：

| 项目 | 实测值 |
|---|---|
| 枚举 VID:PID | `0x31B2:0x2038`（HID interface 3，11 字节报文） |
| 厂商字符串 (0x40) | `RYDZ Technology` |
| 产品字符串 (0x48) | `KT USB Audio` |
| 序列号 (0x50) | `202601151650` |
| 固件版本 (0x04) | `2.0.0` |
| Protocol / 型号码 (0x01) | `0x00000200` → bit9 置位 = **单 DAC 机型**（无 DACL/DACR） |
| Magic (0xE1) | `0x12345678` |
| 出厂模拟增益 A_DAC (0x3B) | `idx 12` = **−1.5 dB**（可调范围 mute / −18 … +3 dB，1.5 dB 步进） |
| 出厂模拟增益 A_ADC (0x3A) | `idx 4` = **20 dB**（麦克风/ADC 侧，与耳机输出无关） |
| 数字增益 DIG_ADC (0x65) / DIG_DAC (0x66) | `0 dB` / `0 dB` |
| EQ | ADC、DAC 各 5 段，均**关闭** |

> 设备**没有** feature report 通道：`HidP_GetCaps` 返回
> `input_len=11 output_len=11 feature_len=0`。因此 bootloader 那套 feature report
> 命令在 run 模式下无法使用。

---

## 初始化（Windows / Linux 通用）

```bash
python -m venv .venv

# Windows PowerShell
.venv\Scripts\Activate.ps1
# 或直接用解释器：.venv\Scripts\python.exe ...

# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
```

`requirements.txt`：

```
hidapi>=0.14
windows-curses>=2.3; sys_platform == "win32"
```

- `hidapi`：HID 通信（必需）。
- `windows-curses`：Windows 上跑 `ktmicro_tui.py` 和测试需要（CPython 不自带 `_curses`；
  Linux/macOS 自带）。

> 注意：README 早期版本写的"Linux only"已不成立。设备发现已改为 PID 自适应：
> 先按 VID+PID 精确匹配，再按 HID interface 3 匹配，最后退回同 VID 的任意 HID；
> Windows 下走 `\\?\HID#...` 设备路径，Linux 下走 `/dev/hidrawN`。

---

## 命令行用法

```bash
python ktmicro.py list              # 枚举 VID=0x31B2 的 HID 接口（显示真实 PID / interface）
python ktmicro.py info              # 芯片识别信息
python ktmicro.py state             # 完整 DSP 状态（音量 / EQ / DRC）
python ktmicro.py dump              # 0x00–0xFF 全部 256 个寄存器
python ktmicro.py read 0x3B         # 读单个寄存器
python ktmicro.py write 0x3B 1      # 写单个寄存器（hex 或十进制）
```

### 音量（`vol`）

```bash
python ktmicro.py vol                              # 读当前 PGA + 数字增益
python ktmicro.py vol --set pga_adc 14            # A_ADC：dB 值或索引（0 dB / −6 / 8 / 14 / 20 / 26 / 32 / 44）
python ktmicro.py vol --set pga_dac -18           # A_DAC：mute 或 −18 … +3 dB（1.5 dB 步进）
python ktmicro.py vol --set dig_adc -6            # 数字 ADC（0.5 dB 步进）
python ktmicro.py vol --set dig_dac -12           # 数字 DAC（单 DAC 机型只写 byte0）
```

### EQ（`eq`）

```bash
python ktmicro.py eq adc                          # 查看 ADC 侧 5 段 PEQ
python ktmicro.py eq dac --enable                 # 打开 DAC EQ
python ktmicro.py eq adc --set 0 --freq 1000 --gain +3.0 --q 1.0
python ktmicro.py eq adc --set 1 --ftype LPF --freq 8000 --q 0.7
```

滤波器类型：`P`（峰值）、`LPF`、`HPF`、`LS`（低架）、`HS`（高架）。

### DRC（`ng` / `lim`）

```bash
python ktmicro.py ng                              # NoiseGate 读取
python ktmicro.py ng --set en 0                   # 关闭 NoiseGate
python ktmicro.py lim --set point -6 --rt 50      # Limiter 阈值 / 释放时间
```

### 交互式 TUI

```bash
python ktmicro_tui.py            # 真机
python ktmicro_tui.py --demo     # 无硬件演示（带实时 biquad 频响曲线）
```

### 固件烧写（`flash`）

```bash
python ktmicro.py flash fw.bin --chip-type 0x10
```

⚠️ 仅在设备处于 **bootloader 模式**（PID `0x0101`，feature report 通道）时可用。
run 模式（`0x2038`）下无法切换进去，详见下文。

---

## 关键实测结论（务必先读）

### 1. 真正控制耳机音量的是 `A_DAC`（0x3B），不是数字增益

| 寄存器 | 实测行为 |
|---|---|
| `0x3B` A_DAC（模拟 PGA） | **写入即时生效**。设为 `mute`（idx 0）耳机立刻完全静音；设为 `1`（−18 dB）音量明显下降。范围 `mute / −18 … +3 dB`，1.5 dB 步进 |
| `0x66` DIG_DAC（数字增益） | 写入与回读都正常（如 `0x…E8` = −12 dB 可读回），但**听感毫无变化** → 该固件对输出被旁路或被主机音量覆盖，**不要依赖** |
| `0x3A` A_ADC（模拟 PGA） | 作用于 ADC/麦克风侧，与耳机输出音量无关，别用它调音量 |

另外：**Windows 音量滑块不写任何 DSP 寄存器**（100% → 0% 全扫 256 个寄存器，无一变化），
主机音量由芯片内部 UAC 逻辑处理。

### 2. 运行态寄存器写入是**易失**的，本仓库没有持久化通路

- 实测：设 `0x3B = 1`（−18 dB）、`0x66 = 0x…E8`（−12 dB），听感确认有效；**拔插后**
  `0x3B` 回读 `12`（−1.5 dB）、`0x66` 回读 `0x00000200`（0 dB）。
- 已知协议只有 `0x52` 读 / `0x57` 写 / `0x43` 握手，全部作用于运行态 RAM。
- run 模式**没有 feature report 通道**（`feature_len=0`），bootloader 的 `.KTM`/`write_block`
  命令物理上够不到。
- run 模式 PID（`0x2038`）≠ bootloader PID（`0x0101`），仓库里没有任何"run → bootloader"切换命令。
- 结论：持久化只能靠 (a) 重刷一份"出厂默认即 −18 dB"的固件，或 (b) 厂商的提交命令；
  两者都需要 `KT_USB_APP.exe` 或 flash 读回能力，本仓库目前都不具备。

### 3. 安全警告

- **不要发送 `0x08` 命令**。仅发一次读请求，就让芯片进入持续噪声的挂死状态，
  必须断电重插才能恢复（本机已复现一次）。`KT_USB_PROTOCOL.md` 里把它标为
  "Read Extended" 的描述与 `0x2038` 固件不符。
- 不要盲写 flash：失败的后果是变砖，而救砖同样需要原厂工具。

---

## 降低"最低音量"的唯一可行方案

```bash
python ktmicro.py vol --set pga_dac -18
```

−18 dB 是 A_DAC 的硬件下限（再往下只有 mute），比出厂 −1.5 dB 低 **16.5 dB**，
实测最小音量明显变小。代价是最大音量也整体降 16.5 dB。

唯一缺点：拔插即还原，必须重新执行一次（原因见上文"运行态寄存器写入是易失的"）。
要不要做成开机自动重放，由使用者自行决定（本仓库不附带守护进程）。

---

## 文件说明

| 文件 | 用途 |
|---|---|
| `ktmicro.py` | CLI：`list/info/state/dump/read/write/ng/lim/vol/eq/flash` |
| `ktmicro_tui.py` | curses 交互式 TUI，含实时 biquad 频响曲线（`--demo` 免硬件） |
| `kt02h20.py` | 设备驱动类，全部参数的语义化 get/set 与编码/解码 |
| `kt02h20_boot.py` | bootloader 固件烧写（HID feature report 协议） |
| `hid_transport.py` | HID 传输层：PID 自适应发现、11 字节中断报文、feature report 封装 |
| `test_ktmicro.py` | 39 个编解码往返单测，不需硬件 |
| `KT_USB_PROTOCOL.md` | 寄存器表与协议细节（含 Persistence 实测章节） |

## 测试

```bash
python -m unittest test_ktmicro -v
```

全部为纯函数/编解码测试，**无需连接硬件**。

## 协议文档

寄存器映射、报文格式、EQ/NoiseGate/Limiter 编码，以及本次的持久化实测记录，
见 [`KT_USB_PROTOCOL.md`](KT_USB_PROTOCOL.md)。

## 许可

见 [`LICENSE`](LICENSE)。
