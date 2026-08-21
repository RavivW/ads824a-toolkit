# ADS824A Toolkit

本工具使用 Python、PyQt5 和 PyVISA 远程控制 OWON ADS824A（及同系列）示波器：仪器发现与连接、4 通道一屏设置面板、波形/屏幕截图/FFT 分析、原生 Save 全记录导出等。项目最初只是一个基础 SCPI 演示，功能不断扩展后已经不再只是"演示"，因此改名为 ADS824A Toolkit（原文件名 `Demo.py`）。

主程序源代码位于 `ADS824A_Toolkit.py`。

## 项目结构

```text
Demo-Python/
|-- CHANGELOG.md
|-- LICENSE
|-- README.md
|-- README_EN.md
|-- requirements.txt
|-- .gitignore
|-- ADS824A_Toolkit.py
|-- ftp_test_server.py
`-- ftp_browse_test.py
```

| 文件 | 说明 |
| --- | --- |
| `ADS824A_Toolkit.py` | PyQt5 GUI 应用程序，包含仪器发现、连接处理和 SCPI 功能封装。 |
| `requirements.txt` | Python 依赖列表，包含 `PyVISA`、`PyQt5`、`numpy`、`matplotlib` 和 `typing_extensions`。 |
| `README.md` | 中文使用说明和功能文档。 |
| `README_EN.md` | 英文使用说明和功能文档。 |
| `CHANGELOG.md` | 版本历史。 |
| `LICENSE` | MIT 许可证。 |
| `.gitignore` | 排除本地虚拟环境、`__pycache__`，以及程序运行时生成的 CSV/PNG 文件。 |
| `ftp_test_server.py` | 测试用 FTP 服务端脚本（电脑作为 FTP 服务器，供示波器 Send-to-FTP 时使用）。 |
| `ftp_browse_test.py` | 测试用 FTP 客户端脚本（连接到示波器自身的 FTP 服务器，浏览/下载文件）。 |

## 环境要求

| 项目 | 推荐版本 / 说明 |
| --- | --- |
| 操作系统 | Windows 10/11。Linux 和 macOS 也可以使用，但 VISA 后端安装方式不同。 |
| Python | Python 3.10 或兼容版本。 |
| 仪器接口 | USB、LAN、GPIB、RS232 或其他 VISA 支持的接口。 |
| 仪器协议 | 仪器必须支持 SCPI 命令。 |
| VISA 后端 | 推荐使用 NI-VISA、Keysight IO Libraries Suite 或厂商提供的 VISA 驱动。 |

PyVISA 是 Python 的 VISA API 层，不是底层硬件驱动。为了发现仪器，计算机通常还需要安装 VISA 后端，并且仪器应在操作系统或厂商 IO 工具中可见。

常见 VISA 资源示例：

```text
USB0::0x0699::0x0363::C000000::INSTR
TCPIP0::192.168.1.100::inst0::INSTR
TCPIP0::192.168.1.100::5025::SOCKET
GPIB0::1::INSTR
ASRL3::INSTR
```

## 本机型（ADS824A）的已知 LAN 配置信息

以下信息基于 2026-08-20 对一台 OWON ADS824A 通过 LAN 的实测确认，可用于节省今后使用同型号或类似仪器时的调试时间。

**查找连接信息**：本示波器内置类似 LXI 的网页界面。将其接入与电脑相同的局域网后，在浏览器中打开 `http://<示波器IP>`，仪器信息页面会显示：

- 示波器当前的 LAN IP 地址。
- **SCPI Socket Port**（本次测试为 **3000**，并非常见默认值 5025）。

**可用于 LAN 连接的 VISA 资源地址**：

```text
TCPIP0::<示波器IP>::<SCPI Socket 端口>::SOCKET
```

例如：`TCPIP0::192.168.10.108::3000::SOCKET`。

`scan_instruments()`/`list_resources()` 通常**无法**自动发现这台局域网仪器（该调用主要发现 USB/GPIB 设备）。因此 `ADS824A_Toolkit.py` 的仪器选择窗口中新增了一个手动输入 VISA 地址的文本框（预填 `ADS824A_Toolkit.py` 顶部的 `DEFAULT_LAN_RESOURCE` 常量），可以直接使用上面的地址连接，无需依赖扫描结果。

尝试使用 `TCPIP0::<IP>::inst0::INSTR`（VXI-11）可以成功打开连接，但之后每次查询都会超时——该固件的远程控制服务似乎只在 raw SCPI socket 上响应，不支持 VXI-11。

**仅用于 LAN 时无需安装 VISA 后端**：纯 Python 的 `pyvisa-py` 即可满足上面的 SOCKET 连接，无需安装 NI-VISA / Keysight IO Libraries。`ADS824A_Toolkit.py` 中的 `connect()` 在系统没有 VISA 库时会自动回退到 `pyvisa.ResourceManager("@py")`。

**raw SOCKET 资源需要注意的两个设置**（`connect()` 检测到地址中含 `SOCKET` 时会自动设置第一项）：

- `read_termination` / `write_termination` 设为 `'\n'` —— 文本类 SCPI 交互（`*IDN?`、测量查询等）需要它，因为 raw socket 本身没有消息分帧机制。
- 但对于**二进制**的截屏查询（`:DISplay:DATA?`），同样的 `'\n'` termination 会造成问题：PNG 文件签名本身第 6 个字节就是 `0x0A`（完整签名为 `89 50 4E 47 0D 0A 1A 0A`）。因此 `save_screen_image_png()` 会在这一次读取时临时关闭 `read_termination`，**并且**关闭 pyvisa-py 的 `suppress_end_enabled` 属性（读取结束后恢复原值，详见方法内注释）。`suppress_end_enabled` 这一步不能省略：pyvisa-py 的 `TCPIPSocketSession.read()` 对 SOCKET 资源默认将该属性设为 `True`，这会导致没有终止符的读取一直阻塞到整个 timeout 结束，然后把已经收到的数据全部丢弃，而不是在仪器数据发送完毕、变安静时就正常返回。

**已验证固件**：通过 `*IDN?` 确认的实测机型信息：

```text
OWON,ADS824A,25380320,V1.0.1.5.2
```

## 波形 CSV 导出相关发现（2026-08-21）

以下内容基于 2026-08-21 对该示波器的实测确认，此时程序已扩展为 4 通道图形界面。

**`:CURVe?` 返回的原始数据既不是电压值，也不带时间信息。** 在 `:DATa:ENCdg ASCii` 和 `:DATa:WIDth 2` 设置下，`:CURVe?` 返回的每个数值都是原始数字化编码（范围 -32768 到 32767），不是电压，也完全没有时间轴。要转换为真实单位，需要使用 WFMOutpre 系列的比例/时基参数（编程手册 2.24 节），按照标准的 Tektronix 风格换算公式：

```text
voltage = (raw_code - YOFf) * YMULt + YZEro
time    = XZEro + index * XINcr
```

`ADS824A_Toolkit.py` 中的 `save_channel_waveform_csv()` 会在读取 `:CURVe?` 之前先查询 `:WFMOutpre:YMUlt?`、`:WFMOutpre:YOFf?`、`:WFMOutpre:YZEro?`、`:WFMOutpre:XINcr?` 和 `:WFMOutpre:XZEro?`，并写出包含 `Time (s)` 和 `Voltage (V)` 两列的 CSV，而不是单独一列原始数值。

**`:DATa:TYPe SCREEN` 返回的是每个屏幕列一对（最小值、最大值），而不是单纯的时间序列。** 如果把每个原始采样点都当成独立的连续时间点处理，波形会呈现锯齿状的"噪声"。经与示波器实际显示对比实测确认：每两个连续的原始采样点实际上是该显示列时间片内捕获到的局部最小值和最大值（一种与显示分辨率绑定的峰值检测/包络式压缩），将每一对（最小值、最大值）取平均后即可还原真实信号。因此 `save_channel_waveform_csv()` 会始终把连续两个原始采样点配成一对，并为每一对写出一行取平均后的（时间、电压）数据。

**约 900 点（1800 个原始采样点）是屏幕分辨率带来的硬性上限，与 Record Length 设置无关。** 将 `:DATa:STARt`/`:DATa:STOP` 显式设置为当前完整的 `:HORizontal:RECordlength`（测试过最大到 1,000,000）后，`:CURVe?` 返回的采样点数量**完全没有变化**——始终是 1800 个原始点/900 个平均点。另外测试的组合查询指令 `:WAVFrm?` 也表现出同样约 1800-2000 点的上限。这看起来是该机型固件在 SCPI 波形传输路径上的限制：无论 Record Length 或 `:DATa:STARt`/`:DATa:STOP` 如何设置，它始终只提供屏幕分辨率级别的数据，而不是完整的采集内存。

**要获取完整记录（最多到当前配置的 Record Length），需要使用原生的 Save 命令子系统（2.22 节），而不是 `:CURVe?`/`:WAVFrm?`。** 这与仪器面板上的 **Copy** 快捷键功能相同——已由用户直接在示波器上确认：它可以将完整记录（例如全部 1,000,000 个采样点）连同时间列一起，以 CSV 格式导出，并且是所有通道一起导出。`ADS824A_Toolkit.py` 新增的 "Save (Native)" 标签页会通过 SCPI 远程触发它：

```text
:SAVe:ASSIgn:TYPe WAVEform
:SAVe:WAVEform:FILEFormat CSV
:SAVe:WAVEform:SOUrce ALL
:SAVe:PATH EXTERNal
:SAVe:WAVEform <文件名>
```

已确认可以正常工作：通过 LAN 远程触发后，示波器执行了与面板 Copy 按钮相同的保存/导出操作，并将文件写入了连接在示波器上的 U 盘。**需要注意的重要限制：** 这条路径只是在仪器上触发保存，本身并不会把文件通过 LAN 传回电脑。当 `:SAVe:PATH` 设为 `EXTERNal` 时，示波器上必须插着 U 盘，之后需要手动从 U 盘取出文件。当设为 `INTERNal` 时，文件会保存到示波器内部存储；不拔 U 盘取回该文件的几种实测方法，见下文"如何把原生 Save 生成的文件取回电脑"一节。

`ADS824A_Toolkit.py` 中现有两条波形导出路径的对比：

| 路径 | 方法 / 按钮 | 采样点数量 | 数据传输方式 |
| --- | --- | --- | --- |
| `:CURVe?`（各通道 CH1-CH4 标签页） | `save_channel_waveform_csv()` | 受屏幕分辨率限制，约 900 个平均点，与 Record Length 无关 | 通过 LAN 直接传回电脑 |
| 原生 Save（"Save (Native)" 标签页） | `save_waveform_native()` | 完整的已配置 Record Length（测试过最大 1,000,000） | 只写入仪器本身——`EXTERNal` 写入 U 盘，`INTERNal` 写入内部存储——不会通过 LAN 传回 |

## 如何把原生 Save 生成的文件取回电脑（2026-08-21）

上面的原生 Save 子系统只会把文件写到仪器自身的存储上，不会自动通过 LAN 传回电脑。本节记录了几种不用从示波器上拔 U 盘、把文件取回电脑的实测方法，按从最简单可靠到最麻烦排列。`ADS824A_Toolkit.py` 目前还没有自动化其中任何一种——这里先记录调研结果，之后再挑一种写成代码。

**USB Device 接口（推荐）。** 示波器后面板上有一个独立于前面板 USB-A "Host" 接口的第二个 USB 接口，用户手册中的描述是："USB Device Interface：当示波器作为'从设备'连接外部 USB 设备时，通过该 USB Device 接口传输数据，例如可用该接口连接电脑。" 经实测确认：用一根普通的 USB-A 转 USB-B 线连接该接口和电脑后，示波器的存储会直接以普通 U 盘的形式出现在 Windows 资源管理器中，不需要 VISA、不需要装驱动、也完全不涉及任何网络设置——体验和插一个普通 U 盘完全一样。截图确认：插上后出现的盘符里，"Internal shared storage"（内部共享存储）和 "USB drive"（U 盘）会作为两个独立的可浏览位置分别列出，所以不管保存时用的是 `:SAVe:PATH INTERNal` 还是 `:SAVe:PATH EXTERNal`，这条路径都能取到文件——这一点比下面的 FTP 方式更好，FTP 只能访问到 Internal。插上 USB-B 线后，根据保存时用的路径进对应的那个位置把文件复制出来即可。（编程手册里另外提到这同一个物理接口也可以承载 SCPI 通信——"communicate with the computer via USB or LAN"——但那是这个接口的另一种用法，和这里说的"当 U 盘用"是两回事。）

**通过 WiFi 使用 FTP（可行，但有不少限制）。** 示波器的 Save 界面里有一个发送到 FTP 的选项，但它专门依赖 WiFi 无线网卡——即使有线 LAN 连接（用于 SCPI 控制）正常工作，它依然会提示 "NO WIFI NETWORK"。实测发现：

- 这台机器上 WiFi 和有线 LAN 接口是互斥的——打开 WiFi 会导致 LAN 连接断开，所以 SCPI 远程控制和这种取文件的方式不能同时使用。
- WiFi 连接成功后，示波器会给出类似 `ftp://192.168.0.197:2121` 的地址——也就是说示波器本身就是 FTP **服务端**，需要用电脑上的普通 FTP 客户端（或本项目里的 `ftp_browse_test.py` 辅助脚本）主动去连接示波器，而不是反过来。电脑必须接入同一个 WiFi 网络才能连上。
- 通过这种方式只能访问到 Internal（内部存储）区域，插在示波器上的 External（U 盘）区域没有出现在 FTP 里。
- 还发现一个"目录列表过期不刷新"的现象：如果 FTP 连接已经建立，之后在示波器上新保存的文件不会立刻出现，需要断开重连才能看到。因此每次取文件都应该重新建立一次连接，而不是复用旧连接——`ftp_browse_test.py` 本身就是这样写的。

**通过蓝牙 Send。** Save 文件列表里"选中文件后点 Send"这个操作，实测发现其实是走蓝牙（Bluetooth Object Push）发送，跟 FTP 或任何网络设置都没有关系。实测确认可行：先把示波器和电脑做蓝牙配对，在电脑上打开 Windows 自带的"蓝牙文件传输"向导（可以搜索 "Bluetooth File Transfer"，或直接运行 `fsquirt.exe`，选择"接收文件"），然后在示波器上点 Send，一个约 35MB 的文件成功传完——速度慢，但确实完整传过去了。有一个界面上的小怪癖：只选中一个文件点 Send 没有任何反应；选中两个文件才会触发蓝牙设备搜索并发送。变通做法：始终多选一个无关紧要的文件一起发送，收到后再把多余的那个删掉。由于蓝牙和 LAN/WiFi 是完全独立的无线电，理论上不应该像 WiFi 那样把正在使用的 LAN SCPI 连接断开，但这个组合还没有实测验证过。

目前测试过的几种方式对比：

| 方式 | 需要什么 | 能否与 LAN SCPI 控制同时使用 | 备注 |
| --- | --- | --- | --- |
| USB Device 接口 | 一根 USB-A 转 USB-B 线 | 可以——与 LAN 是完全独立的物理连接 | 最简单：直接当成普通盘符，复制/拖拽即可 |
| 蓝牙 Send | 蓝牙配对 + Windows 接收向导 | 大概率可以（尚未实测组合使用） | 必须多选 2 个文件才会触发 Send；大文件传输较慢 |
| 通过 WiFi 使用 FTP | WiFi 网络 + FTP 客户端 | 不可以——打开 WiFi 会断开有线 LAN | 只能访问 Internal 存储；要看到新保存的文件需要重新连接 FTP |
| 手动拔插 U 盘 | 插在示波器上的 U 盘 | 可以 | 最早测试过的原始方法——始终可行，只是需要走到仪器旁边操作 |

## 安装依赖

在项目根目录运行以下命令：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

如果 PowerShell 阻止脚本激活，请临时修改当前终端的执行策略：

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process
.\.venv\Scripts\Activate.ps1
```

也可以不激活虚拟环境，直接使用虚拟环境中的 Python：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe ADS824A_Toolkit.py
```

## 运行工具

仪器连接并开机后，运行：

```powershell
python ADS824A_Toolkit.py
```

或使用虚拟环境中的 Python：

```powershell
.\.venv\Scripts\python.exe ADS824A_Toolkit.py
```

应用程序工作流程：

1. 打开仪器选择窗口。
2. 应用程序扫描计算机可用的 VISA 资源。
3. 在列表中选择目标仪器。
4. 点击 `Connect` 打开仪器会话并显示控制窗口。
5. 点击功能按钮发送对应的 SCPI 命令。
6. 命令结果或保存消息会显示在输出文本框中。

如果未找到仪器，窗口会显示 `No connected instruments found.`。请检查仪器电源、线缆、网络配置和 VISA 驱动状态。

## UI 功能

| 按钮 | 功能 | 输出框 |
| --- | --- | --- |
| `Query *IDN?` | 查询仪器身份信息。 | 显示 `*IDN?` 响应字符串。 |
| `Save CH1 Waveform CSV` | 读取 CH1 屏幕波形数据，移除响应头，并保存为本地 CSV 文件。 | 显示成功消息和保存文件路径。 |
| `Read CH1 Period` | 读取通道 1 的自动周期测量值。 | 显示 `:MEASUrement:IMMed:VALue?` 响应。 |
| `Save Screen PNG` | 读取当前屏幕图像二进制数据，移除响应头，并保存为本地 PNG 文件。 | 显示成功消息和保存文件路径。 |

## 本地输出文件

`Save CH1 Waveform CSV` 会将波形数据保存到当前用户的 Documents 文件夹：

```text
wavefrom.csv
```

`Save Screen PNG` 会将截图数据保存到当前用户的 Documents 文件夹：

```text
screen_image.png
```

成功时，输出框会显示 `File saved successfully.` 以及完整文件路径。在 Windows 上，程序使用系统 Documents 文件夹。在其他平台上，会使用用户主目录下的 `Documents` 文件夹。

## SCPI 功能封装

### 1. 扫描仪器

方法：

```python
scan_instruments()
```

PyVISA 枚举可用 VISA 资源：

```python
resource_manager.list_resources()
```

### 2. 连接仪器

方法：

```python
connect(resource_address)
```

连接后，程序会应用常用 SCPI 通信设置。波形和截图传输可能比简单文本响应更大，因此会增加超时时间和接收块大小：

```python
instrument.timeout = 30000
instrument.chunk_size = 1024 * 1024
```

如果目标仪器需要不同的超时时间、终止符或数据块大小，请调整这些值。

### 3. 查询 IDN

方法：

```python
query_idn()
```

命令：

```text
*IDN?
```

这是标准 SCPI 身份查询。通常会返回厂商、型号、序列号和固件版本。

### 4. 读取并保存某个通道的波形 CSV

方法：

```python
save_channel_waveform_csv(channel)  # channel 取值 1-4
```

命令序列（以 CH1 为例；`_fetch_channel_waveform()` 会把通道号替换成实际请求的通道）：

```text
:DATa INIT
:DATa:TYPe SCREEN
:DATa:SOUrce CH1
:DATa:WIDth 2
:DATa:ENCdg ASCii
:DATa:STARt 1
:DATa:STOP <记录长度>
:WFMOutpre:YMUlt?
:WFMOutpre:YOFf?
:WFMOutpre:YZEro?
:WFMOutpre:XINcr?
:WFMOutpre:XZEro?
:CURVe?
```

程序先配置波形传输，查询 WFMOutpre 系列的比例/时基参数，再通过 `:CURVe?` 读取原始采样点，去掉 `:CURVE` 前缀，把每两个连续的原始采样点换算成一个取平均后的（时间、电压）数据点（原因见上文"波形 CSV 导出相关发现"一节），最后将结果以 `Time (s)` / `Voltage (V)` 两列 CSV 的形式写入 Documents 文件夹下的 `waveform_ch<通道号>.csv`。

### 5. 读取 CH1 周期测量

方法：

```python
read_channel1_period_measurement()
```

命令序列：

```text
:MEASUrement:IMMed:SOURCE1 CH1
:MEASUrement:IMMed:TYPe PERIod
:MEASUrement:IMMed:VALue?
```

程序将自动测量源设置为通道 1，将测量类型设置为周期，然后读取当前值。

### 6. 读取并保存屏幕 PNG

方法：

```python
save_screen_image_png()
```

查询命令：

```text
:DISplay:DATA?
```

示波器会返回当前屏幕图像的二进制数据。程序会移除 `:DISPLAY:DATA` 前缀，当剩余数据长度大于 4 字节时移除前 4 个字节，在存在尾随换行字节 `\n` 时将其移除，并将结果作为 `screen_image.png` 保存到 Documents 文件夹。

## 说明

`Save CH1 Waveform CSV` 和 `Save Screen PNG` 会将文件保存到当前用户的 Documents 文件夹。它们不再使用 `SAVe:PATH INTERNal` 将文件保存到仪器内部存储。

不同厂商和型号对于 `:CURVe?`、`:DISplay:DATA?`、响应头、二进制块头和终止符的返回格式可能不同。如果保存的 CSV 或 PNG 不正确，请检查目标型号的编程手册，并调整 `save_channel_waveform_csv()`（或其内部使用的 `_fetch_channel_waveform()`）或 `save_screen_image_png()` 中的解析逻辑。

## 扩展指南

添加新功能时：

1. 在 `ScpiInstrumentController` 中添加一个独立方法。
2. 将所需的设置命令和查询命令放入该方法。
3. 在 `InstrumentControlWindow` 中添加按钮。
4. 将按钮点击事件连接到新的处理函数。
5. 在处理函数中调用 `_run_action()`，使结果以一致方式显示在输出框中。

示例：

```python
def query_system_error(self) -> str:
    """Read the next item in the instrument error queue."""

    instrument = self._require_instrument()
    return instrument.query("SYSTem:ERRor?").strip()
```

## 故障排查

### 1. 未找到仪器

可能原因：

- 仪器未开机或线缆已断开。
- 未安装 NI-VISA、Keysight IO Libraries Suite 或厂商 VISA 驱动。
- USB 仪器驱动未正常工作。
- LAN 仪器 IP、网关或端口设置不正确。
- 仪器未启用远程控制模式。

请使用 NI MAX、Keysight Connection Expert 或厂商 IO 工具确认仪器对计算机可见。

### 2. PyQt5 或 PyVISA 导入失败

确认依赖已安装在当前活动的 Python 环境中：

```powershell
python -m pip install -r requirements.txt
python -c "import PyQt5; import pyvisa; print('OK')"
```

如果使用虚拟环境，请确保安装依赖和运行程序使用的是同一个 Python。

### 3. 点击按钮后出现 SCPI 错误

不同厂商和型号的 SCPI 命令可能不同。建议检查：

- 先点击 `Query *IDN?`，确认已连接正确仪器。
- 查看目标仪器的 Programming Manual 或 SCPI Command Reference。
- 根据仪器手册调整 `ScpiInstrumentController` 中的相关方法。
- 如果支持，可查询错误队列，例如 `SYSTem:ERRor?`。

### 4. 查询耗时过长或超时

可能原因：

- 查询命令不会返回数据。
- 终止符与仪器设置不匹配。
- 超时时间太短。
- 波形或截图数据较大，需要更多传输时间。

如仪器需要，请调整设置：

```python
instrument.timeout = 30000
instrument.chunk_size = 1024 * 1024
```

## 验证

基础检查：

```powershell
.\.venv\Scripts\python.exe -m py_compile ADS824A_Toolkit.py
.\.venv\Scripts\python.exe -c "import PyQt5; import pyvisa; print('PyQt5 and PyVISA import OK')"
```

验证 VISA 扫描入口：

```powershell
.\.venv\Scripts\python.exe -c "from test import ScpiInstrumentController; c = ScpiInstrumentController(); print(c.scan_instruments()); c.close()"
```
