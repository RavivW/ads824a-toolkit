# ADS824A Toolkit

This tool uses Python, PyQt5, and PyVISA to remotely control an OWON ADS824A (and same-series) oscilloscope: instrument discovery and connection, a combined 4-channel control panel, waveform/screenshot/FFT analysis, and full-record native Save export. It started out as a basic SCPI demo, but has grown well past "demo" as features were added, so it was renamed to ADS824A Toolkit (previously `Demo.py`).

The main program source is in `ADS824A_Toolkit.py`.

## Project Structure

```text
Demo-Python/
|-- CHANGELOG.md
|-- LICENSE
|-- README.md
|-- requirements.txt
|-- .gitignore
|-- ADS824A_Toolkit.py
|-- ftp_test_server.py
`-- ftp_browse_test.py
```

| File | Description |
| --- | --- |
| `ADS824A_Toolkit.py` | PyQt5 GUI application with instrument discovery, connection handling, and SCPI feature wrappers. |
| `requirements.txt` | Python dependency list, including `PyVISA`, `PyQt5`, `numpy`, `matplotlib`, and `typing_extensions`. |
| `README.md` | Usage and feature documentation. |
| `CHANGELOG.md` | Version history. |
| `LICENSE` | MIT license. |
| `.gitignore` | Excludes local venvs, `__pycache__`, and the CSV/PNG files the tool generates at runtime. |
| `ftp_test_server.py` | Test FTP *server* script (this PC acts as the FTP server, for the scope's Send-to-FTP feature). |
| `ftp_browse_test.py` | Test FTP *client* script (connects to the scope's own FTP server to browse/download files). |

## Requirements

| Item | Recommended Version / Notes |
| --- | --- |
| OS | Windows 10/11. Linux and macOS can also be used, but VISA backend installation is different. |
| Python | Python 3.10 or compatible. |
| Instrument interface | USB, LAN, GPIB, RS232, or another VISA-supported interface. |
| Instrument protocol | The instrument must support SCPI commands. |
| VISA backend | NI-VISA, Keysight IO Libraries Suite, or a vendor-provided VISA driver is recommended. |

PyVISA is the Python VISA API layer. It is not the low-level hardware driver. To discover instruments, the computer usually also needs a VISA backend installed, and the instrument should be visible in the OS or vendor IO tools.

Common VISA resource examples:

```text
USB0::0x0699::0x0363::C000000::INSTR
TCPIP0::192.168.1.100::inst0::INSTR
TCPIP0::192.168.1.100::5025::SOCKET
GPIB0::1::INSTR
ASRL3::INSTR
```

## This Scope's Known LAN Configuration (ADS824A)

The following was confirmed by hands-on testing with an OWON ADS824A over LAN on 2026-08-20, and should save time on future setups with the same or a similar unit.

**Discovering the connection details**: this scope has an LXI-style built-in web page. Once it is on the same LAN as the PC, open `http://<scope-IP>` in a browser. The instrument information page reports:

- The scope's current LAN IP address.
- **SCPI Socket Port** -- for the unit tested, this was **3000**, not the commonly assumed default of 5025.

**VISA resource address that works over LAN**:

```text
TCPIP0::<scope-IP>::<SCPI-Socket-Port>::SOCKET
```

For example: `TCPIP0::192.168.10.108::3000::SOCKET`.

`scan_instruments()` / `list_resources()` generally does **not** discover this LAN instrument automatically -- that call mostly finds USB/GPIB devices. `ADS824A_Toolkit.py`'s instrument selection window therefore also has a manual VISA-address text field (pre-filled with the `DEFAULT_LAN_RESOURCE` constant near the top of the file), so you can connect directly with the resource string above instead of relying on the scan.

Attempting `TCPIP0::<IP>::inst0::INSTR` (VXI-11) against this scope opened without error but then timed out on every query -- this firmware's remote-control service only appears to answer on the raw SCPI socket, not VXI-11.

**No VISA backend required for LAN-only use**: `pyvisa-py` (pure Python) is enough for the SOCKET connection above -- no NI-VISA / Keysight IO Libraries installation is needed. `connect()` in `ADS824A_Toolkit.py` already falls back to `pyvisa.ResourceManager("@py")` automatically when no system VISA library is present.

**Two settings that matter for a raw SOCKET resource** (`connect()` sets the first one automatically whenever it detects `SOCKET` in the address):

- `read_termination` / `write_termination` = `'\n'` -- required for text SCPI exchanges (`*IDN?`, measurement queries, etc.), since a raw socket has no built-in message framing.
- For the **binary** screen-capture query (`:DISplay:DATA?`) specifically, that same `'\n'` termination breaks things, because the PNG file signature itself contains a `0x0A` byte a few bytes in (`89 50 4E 47 0D 0A 1A 0A`). `save_screen_image_png()` therefore disables `read_termination` **and** pyvisa-py's `suppress_end_enabled` attribute just for that one read (see the comments in the method), then restores both afterwards. Skipping the `suppress_end_enabled` toggle is not optional: pyvisa-py's `TCPIPSocketSession.read()` defaults that attribute to `True` for SOCKET resources, which makes an unterminated read block for the *entire* instrument timeout and then discard whatever it did receive, instead of returning as soon as the instrument goes quiet.

**Verified firmware**: confirmed via `*IDN?`:

```text
OWON,ADS824A,25380320,V1.0.1.5.2
```

## Waveform CSV Export Findings (2026-08-21)

Confirmed by hands-on testing on 2026-08-21, after the 4-channel GUI expansion of this tool.

**Raw `:CURVe?` data is neither in volts nor associated with time.** With `:DATa:ENCdg ASCii` and `:DATa:WIDth 2`, each value `:CURVe?` returns is a raw digitizer code (-32768..32767), not a voltage, and there is no time axis at all. Converting to real units requires the WFMOutpre scaling/timing parameters (section 2.24 of the Programming Manual) and the standard Tektronix-style curve-scaling formula:

```text
voltage = (raw_code - YOFf) * YMULt + YZEro
time    = XZEro + index * XINcr
```

`save_channel_waveform_csv()` in `ADS824A_Toolkit.py` queries `:WFMOutpre:YMUlt?`, `:WFMOutpre:YOFf?`, `:WFMOutpre:YZEro?`, `:WFMOutpre:XINcr?`, and `:WFMOutpre:XZEro?` before reading `:CURVe?`, and writes a two-column `Time (s)` / `Voltage (V)` CSV instead of a single raw-code column.

**`:DATa:TYPe SCREEN` returns (min, max) pairs per screen column, not a plain time series.** Treating every raw sample as its own sequential time point produces a zig-zag/"noisy" trace. Confirmed empirically against the on-screen waveform: every two consecutive raw samples are actually the local minimum and maximum captured within that display column's time slot (a peak-detect/envelope-style compression tied to display resolution), and averaging each (min, max) pair recovers the real signal. `save_channel_waveform_csv()` therefore always pairs up consecutive raw samples and writes one averaged (time, voltage) row per pair.

**The ~900-point (1800 raw samples) count is a hard screen-resolution cap, not a Record Length setting.** Explicitly setting `:DATa:STARt`/`:DATa:STOP` to the full configured `:HORizontal:RECordlength` (tested up to 1,000,000) had **no effect** on the number of samples `:CURVe?` returns -- it stayed at 1800 raw / 900 averaged points regardless. The alternate combined query `:WAVFrm?` was also tested and showed the same ~1800-2000 point cap. This looks like a firmware-level limitation of the SCPI waveform-transfer path on this unit: it always serves the screen-resolution representation, not the full acquisition memory, no matter what Record Length or `:DATa:STARt`/`:DATa:STOP` are set to.

**Getting the full record (up to the configured Record Length) requires the native Save Command Subsystem (section 2.22), not `:CURVe?`/`:WAVFrm?`.** This is the same mechanism as the front-panel **Copy** button on the instrument -- confirmed directly on the scope by the user: it can export the full record (e.g. all 1,000,000 samples) with a time column, for all channels together, in CSV format. `ADS824A_Toolkit.py`'s "Save (Native)" tab triggers it remotely over SCPI:

```text
:SAVe:ASSIgn:TYPe WAVEform
:SAVe:WAVEform:FILEFormat CSV
:SAVe:WAVEform:SOUrce ALL
:SAVe:PATH EXTERNal
:SAVe:WAVEform <filename>
```

This was confirmed working: triggering it remotely over LAN activated the same save the front-panel Copy button performs, writing the file to a USB drive plugged into the scope. **Important limitation:** this only triggers the save on the instrument -- it does not transfer the file back over LAN by itself. With `:SAVe:PATH EXTERNal`, a USB drive must be plugged into the scope and the file collected from it afterwards. With `:SAVe:PATH INTERNal`, the file goes to the scope's internal storage; see "Getting the Native-Save File Off the Instrument" below for the methods tested for retrieving it without removing a USB drive.

Summary of the two waveform-export paths now in `ADS824A_Toolkit.py`:

| Path | Method / Button | Sample count | Delivery |
| --- | --- | --- | --- |
| `:CURVe?` (per-channel CH1-CH4 tabs) | `save_channel_waveform_csv()` | Capped at ~900 averaged points (screen resolution), regardless of Record Length | Direct to the PC over LAN |
| Native Save (`Save (Native)` tab) | `save_waveform_native()` | Full configured Record Length (tested up to 1,000,000) | Written on the instrument only -- USB drive for `EXTERNal`, or internal storage for `INTERNal` -- not transferred over LAN |

## Getting the Native-Save File Off the Instrument (2026-08-21)

The native Save subsystem above only writes to the instrument's own storage -- it does not push the file back over LAN by itself. Several ways to get that file onto the PC without physically pulling a USB drive out of the scope were tested by hand; they are summarized here from simplest/most reliable to most involved. `ADS824A_Toolkit.py` does not yet automate any of these -- this is knowledge gathered ahead of picking one to script.

**USB Device port (recommended).** The scope's rear panel has a second USB connector separate from the front USB-A "Host" ports, described in the User Manual as: *"USB Device Interface: When the oscilloscope is connected to an external USB device as a 'slave device', the USB Device interface is used to transmit the data. For example, use the interface to connect a PC."* Confirmed by hands-on testing: plugging a standard USB-A-to-USB-B cable between this port and the PC makes the scope's storage appear as an ordinary USB mass-storage drive in Windows Explorer, with no VISA, no driver install, and no network setup of any kind -- the same experience as plugging in a USB flash drive. Confirmed by screenshot: the resulting drive shows both `Internal shared storage` and `USB drive` as separate browsable entries, so this path reaches files saved with either `:SAVe:PATH INTERNal` or `:SAVe:PATH EXTERNal` -- unlike the FTP path below, which only reached Internal storage. Plug in the USB-B cable and copy the file from whichever of the two locations matches how the file was saved. (The Programming Manual separately states this same physical port can also carry SCPI communication -- "communicate with the computer via USB or LAN" -- but that is a different use of the connector from the mass-storage behavior described here.)

**FTP over WiFi (works, but with real caveats).** The scope's Save screen has an FTP send option, gated on the WiFi radio specifically -- it reported "NO WIFI NETWORK" even while the wired LAN connection (used for SCPI) was active and working. Key findings from testing:

- WiFi and the wired LAN interface are mutually exclusive on this unit -- enabling WiFi disconnects the LAN link, so SCPI control and this file-pull path cannot be used at the same time.
- Once WiFi connects, the scope announces an address such as `ftp://192.168.0.197:2121` -- the scope itself is the FTP *server*; a normal FTP client on the PC (or the `ftp_browse_test.py` helper script in this project) connects *to* the scope, not the other way around. The PC must be on the same WiFi network to reach it.
- Only the Internal storage area was reachable this way; the External (USB drive plugged into the scope) area did not appear over FTP.
- A stale-directory-listing quirk was observed: a file saved after an FTP connection was already open did not show up until the connection was closed and reopened. Always open a fresh connection per fetch rather than reusing one -- which is how `ftp_browse_test.py` already works.

**Bluetooth Send.** The "Send" action next to a saved file in the Save file browser turned out to be a Bluetooth Object Push send, unrelated to FTP or any network setting. Confirmed working: after pairing the scope with the PC over Bluetooth and using Windows' built-in Bluetooth File Transfer wizard (search "Bluetooth File Transfer", or run `fsquirt.exe`, and choose "Receive files") before pressing Send on the scope, a ~35 MB file transferred successfully -- slowly, but completely. One UI quirk: selecting exactly one file before pressing Send did nothing visible; selecting two files triggered the Bluetooth device search and sent both. Workaround: always select the wanted file plus one harmless extra file, then discard the extra after receiving. Since Bluetooth is a separate radio from LAN/WiFi, it should not disconnect an active LAN SCPI session the way WiFi does -- this combination has not been tested yet.

Comparison of the methods tried so far:

| Method | Requires | Coexists with LAN SCPI control? | Notes |
| --- | --- | --- | --- |
| USB Device port | USB-A-to-B cable | Yes -- separate physical connection from LAN | Simplest: mounts as an ordinary drive, plain copy/drag-and-drop |
| Bluetooth Send | Bluetooth pairing + Windows receive wizard | Likely yes (not yet tested together) | Must select 2 files to trigger Send; slow for large files |
| FTP over WiFi | WiFi network + FTP client | No -- WiFi disconnects the wired LAN | Internal storage only; reconnect the FTP session to see newly-saved files |
| Physical USB drive removal | A USB flash drive plugged into the scope | Yes | The original method from earlier testing -- always works, just requires walking to the instrument |

## Install Dependencies

Run these commands from the project root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

If PowerShell blocks script activation, temporarily change the execution policy for the current terminal:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process
.\.venv\Scripts\Activate.ps1
```

You can also use the virtual environment Python directly without activating it:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe ADS824A_Toolkit.py
```

## Run the Tool

After the instrument is connected and powered on, run:

```powershell
python ADS824A_Toolkit.py
```

Or use the virtual environment Python:

```powershell
.\.venv\Scripts\python.exe ADS824A_Toolkit.py
```

Application workflow:

1. Open the instrument selection window.
2. The application scans VISA resources available to the computer.
3. Select the target instrument in the list.
4. Click `Connect` to open the instrument session and show the control window.
5. Click a function button to send the corresponding SCPI commands.
6. Command results or save messages appear in the output text box.

If no instrument is found, the window shows `No connected instruments found.`. Check the instrument power, cable, network configuration, and VISA driver status.

## UI Function

| Button | Function | Output Box |
| --- | --- | --- |
| `Query *IDN?` | Query instrument identity information. | Show the `*IDN?` response string. |
| `Save CH1 Waveform CSV` | Read CH1 screen waveform data, remove the response header, and save a local CSV file. | Show a success message and the saved file path. |
| `Read CH1 Period` | Read the channel 1 automatic period measurement. | Show the `:MEASUrement:IMMed:VALue?` response. |
| `Save Screen PNG` | Read current screen image binary data, remove the response header, and save a local PNG file. | Show a success message and the saved file path. |

## Local Output Files

`Save CH1 Waveform CSV` saves waveform data to the current user's Documents folder:

```text
wavefrom.csv
```

`Save Screen PNG` saves screenshot data to the current user's Documents folder:

```text
screen_image.png
```

On success, the output box shows `File saved successfully.` and the full file path. On Windows, the tool uses the system Documents folder. On other platforms, it uses the `Documents` folder under the user's home directory.

## SCPI Feature Wrappers

### 1. Scan Instruments

Method:

```python
scan_instruments()
```

PyVISA enumerates available VISA resources:

```python
resource_manager.list_resources()
```

### 2. Connect Instrument

Method:

```python
connect(resource_address)
```

After connection, the tool applies common SCPI communication settings. Waveform and screenshot transfers can be larger than simple text responses, so the timeout and receive chunk size are increased:

```python
instrument.timeout = 30000
instrument.chunk_size = 1024 * 1024
```

Adjust these values if the target instrument requires a different timeout, terminator, or data block size.

### 3. Query IDN

Method:

```python
query_idn()
```

Command:

```text
*IDN?
```

This is the standard SCPI identity query. It usually returns the vendor, model, serial number, and firmware version.

### 4. Read and Save a Channel's Waveform CSV

Method:

```python
save_channel_waveform_csv(channel)  # channel is 1-4
```

Command sequence (shown for CH1; `_fetch_channel_waveform()` substitutes the requested channel number):

```text
:DATa INIT
:DATa:TYPe SCREEN
:DATa:SOUrce CH1
:DATa:WIDth 2
:DATa:ENCdg ASCii
:DATa:STARt 1
:DATa:STOP <record length>
:WFMOutpre:YMUlt?
:WFMOutpre:YOFf?
:WFMOutpre:YZEro?
:WFMOutpre:XINcr?
:WFMOutpre:XZEro?
:CURVe?
```

The tool configures waveform transfer, queries the WFMOutpre scaling/timing parameters, then reads the raw samples with `:CURVe?`, strips the `:CURVE` prefix, converts each pair of consecutive raw samples into one averaged (time, voltage) point (see "Waveform CSV Export Findings" above for why), and writes the result as a two-column `Time (s)` / `Voltage (V)` CSV to `waveform_ch<channel>.csv` in the Documents folder.

### 5. Read CH1 Period Measurement

Method:

```python
read_channel1_period_measurement()
```

Command sequence:

```text
:MEASUrement:IMMed:SOURCE1 CH1
:MEASUrement:IMMed:TYPe PERIod
:MEASUrement:IMMed:VALue?
```

The tool sets the automatic measurement source to channel 1, sets the measurement type to period, and then reads the current value.

### 6. Read and Save Screen PNG

Method:

```python
save_screen_image_png()
```

Query command:

```text
:DISplay:DATA?
```

The oscilloscope returns current screen image binary data. The tool removes the `:DISPLAY:DATA` prefix, removes the first 4 bytes when the remaining data is longer than 4 bytes, removes a trailing newline byte `\n` when present, and saves the result as `screen_image.png` in the Documents folder.

## Notes

`Save CH1 Waveform CSV` and `Save Screen PNG` save files to the current user's Documents folder. They no longer use `SAVe:PATH INTERNal` to save files to the instrument's internal storage.

Different vendors and models may return different formats for `:CURVe?`, `:DISplay:DATA?`, response headers, binary block headers, and terminators. If the saved CSV or PNG is not correct, check the target model's programming manual and adjust the parsing logic in `save_channel_waveform_csv()` (or the `_fetch_channel_waveform()` helper it uses) or `save_screen_image_png()`.

## Extension Guidance

When adding a new function:

1. Add an independent method to `ScpiInstrumentController`.
2. Put the required setup commands and query command in that method.
3. Add a button to `InstrumentControlWindow`.
4. Connect the button click event to a new handler.
5. Call `_run_action()` in the handler so the result is displayed consistently in the output box.

Example:

```python
def query_system_error(self) -> str:
    """Read the next item in the instrument error queue."""

    instrument = self._require_instrument()
    return instrument.query("SYSTem:ERRor?").strip()
```

## Troubleshooting

### 1. No Instruments Found

Possible causes:

- The instrument is not powered on or the cable is disconnected.
- NI-VISA, Keysight IO Libraries Suite, or the vendor VISA driver is not installed.
- The USB instrument driver is not working.
- LAN instrument IP, gateway, or port settings are incorrect.
- Remote-control mode is not enabled on the instrument.

Use NI MAX, Keysight Connection Expert, or the vendor IO tool to confirm the instrument is visible to the computer.

### 2. PyQt5 or PyVISA Import Fails

Confirm dependencies are installed in the active Python environment:

```powershell
python -m pip install -r requirements.txt
python -c "import PyQt5; import pyvisa; print('OK')"
```

If you use a virtual environment, make sure the same Python is used for both installing dependencies and running the program.

### 3. SCPI Error After Clicking a Button

SCPI commands may differ between vendors and models. Recommended checks:

- Click `Query *IDN?` first to confirm the correct instrument is connected.
- Check the target instrument Programming Manual or SCPI Command Reference.
- Adjust the relevant method in `ScpiInstrumentController` according to the instrument manual.
- If supported, query the error queue, such as `SYSTem:ERRor?`.

### 4. Query Takes Too Long or Times Out

Possible causes:

- The query command does not return data.
- The terminator does not match the instrument setting.
- The timeout is too short.
- Waveform or screenshot data is large and needs more transfer time.

Adjust settings if required by the instrument:

```python
instrument.timeout = 30000
instrument.chunk_size = 1024 * 1024
```

## Verification

Basic checks:

```powershell
.\.venv\Scripts\python.exe -m py_compile ADS824A_Toolkit.py
.\.venv\Scripts\python.exe -c "import PyQt5; import pyvisa; print('PyQt5 and PyVISA import OK')"
```

To verify the VISA scan entry point:

```powershell
.\.venv\Scripts\python.exe -c "from test import ScpiInstrumentController; c = ScpiInstrumentController(); print(c.scan_instruments()); c.close()"
```
