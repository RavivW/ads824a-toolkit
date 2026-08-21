# CHANGELOG

This document records version changes for the ADS824A Toolkit (previously the "Python SCPI Demo", `Demo.py`).

## [2.0.0] - 2026-08-21

### Changed

- Renamed the project from `Demo.py` / "Python SCPI Demo" to `ADS824A_Toolkit.py` / "ADS824A Toolkit" -- the tool had grown well past a basic demo.
- Prepared the project for a GitHub repository: added an MIT `LICENSE`, added a `.gitignore` (excludes local venvs, `__pycache__`, and the CSV/PNG files generated at runtime), and re-saved `requirements.txt` as plain UTF-8 (it was previously UTF-16 with a BOM, inherited from the original vendor package).
- Expanded from one-channel-at-a-time tabs to a combined 4-channel control panel, all visible together.
- Controls now apply immediately on change (scale, position, coupling, bandwidth, invert, probe ratio, trigger level/holdoff, horizontal scale/delay), matching how the instrument's own front-panel controls behave -- no separate "Set" button.
- Scale-type fields (vertical/horizontal scale, probe ratio) now use dropdown lists of valid 1-2-5-per-decade values with engineering-notation labels instead of free-text entry, and support mouse-wheel scrolling.
- Fixed the startup window layout so all 4 channel panels are visible without manually resizing the window.

### Added

- Added a Reconnect action to recover the LAN connection after the instrument is powered off or the link drops, without restarting the app.
- Added Trigger and Horizontal control panels.
- Fixed channel waveform CSV export to write real `Time (s)` / `Voltage (V)` columns, correctly averaging the min/max sample pairs returned by `:DATa:TYPe SCREEN`, instead of a single column of raw ADC codes.
- Added native Save Command Subsystem integration ("Save (Native)" tab) for full-record (up to the configured Record Length) multi-channel CSV export via the instrument's own Save mechanism.
- Added an interactive "FFT from CSV..." window (matplotlib pan/zoom, log-scale/dB toggle) that reads any Time + one-or-more-voltage-column CSV, showing all channels found (e.g. the native Save subsystem's combined Time+CH1-CH4 file) as separate subplots, with a view selector to switch between the FFT and a scope-style time-domain trace.
- Added `ftp_test_server.py` and `ftp_browse_test.py` as standalone test utilities used while investigating ways to retrieve native-Save files from the instrument over FTP, Bluetooth, and the rear USB Device port -- see the README for findings.

## [1.0.0] - 2026-04-30

### Added

- Added a startup instrument scan window that displays the VISA resource list, allows instrument selection, and connects to the selected instrument.
- Added the main function window shown after connection, including SCPI function buttons and a text output area at the bottom.
- Added the `*IDN?` query function and display of the returned result in the text output area.
- Added a function to save the channel 1 waveform as a CSV file in the Documents directory.
- Added a function to read the channel 1 period automatic measurement.
- Added a function to save a screen capture as a PNG file in the Documents directory.
