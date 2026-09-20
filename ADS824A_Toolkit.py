import ctypes
import csv
import math
import re
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Callable, List, Optional, Tuple, Union

import pyvisa as visa
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

# Default LAN resource address for this scope, used to pre-fill the manual
# address field. SCPI Socket Port 3000. PyVISA needs explicit '\n'
# terminators for the raw SOCKET resource type because, unlike VXI-11
# (::inst0::INSTR), a plain TCP socket has no built-in message framing.
#
# The IP itself comes from DHCP and can change (it moved from .108 to
# .104 on 2026-09-20) -- this constant is only a fallback starting point
# for the manual field. The scanned-list Connect button does not depend
# on this at all: see _socket_resource_for_tcpip_instr() below, which
# rewrites whatever IP scan_instruments() currently discovers.
DEFAULT_LAN_RESOURCE = "TCPIP0::192.168.10.104::3000::SOCKET"

# scan_instruments() discovers this scope as a VXI-11-style resource, e.g.
# "TCPIP::192.168.10.104::INSTR" -- but per the README, VXI-11 opens
# without error and then times out on every query; only the raw SCPI
# socket (port 3000) actually answers on this firmware. So the address a
# scan finds is never directly connectable -- connect_selected_instrument()
# uses this to rewrite it into the working SOCKET form before connecting,
# which also means an IP change (DHCP) is picked up automatically the next
# time the instrument list is scanned, with no manual editing needed.
_TCPIP_INSTR_PATTERN = re.compile(r"^TCPIP\d*::([^:]+)::(?:.+::)?INSTR$", re.IGNORECASE)


def _socket_resource_for_tcpip_instr(resource_address: str) -> str:
    """Rewrite a scanned VXI-11-style TCPIP/INSTR resource into this
    scope's working raw-socket address (see comment above). Returns the
    address unchanged if it doesn't match that shape (already a SOCKET
    address, or a USB/GPIB resource)."""

    match = _TCPIP_INSTR_PATTERN.match(resource_address.strip())
    if not match:
        return resource_address
    host = match.group(1)
    return f"TCPIP0::{host}::3000::SOCKET"

# Standard 8-byte PNG file signature, used to sanity-check that the parsed
# screen capture response actually looks like a PNG before saving it.
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Number of analog channels this GUI exposes. The ADS824A is a 4-channel
# model; change this if the demo is adapted for a 2-channel model.
CHANNEL_COUNT = 4

# Measurement types accepted by :MEASUrement:IMMed:TYPe for an analog channel
# source, taken from section 2.19 of the ADS Series Programming Manual. Not
# every model/firmware necessarily supports every entry here -- if one
# returns an error, check the target instrument's own command reference.
MEASUREMENT_TYPES = [
    "AMPlitude", "AREa", "BURst", "CARea", "CMEan", "CRMs", "FALL",
    "FREQuency", "HIGH", "LOW", "MAXimum", "MEAN", "MEDian", "MINImum",
    "NDUty", "NOVershoot", "NWIdth", "PDUty", "PERIod", "PHAse", "PK2Pk",
    "POVershoot", "PWIdth", "RISe", "RMS", "SDUty", "STDdev",
]


def _generate_1_2_5_values(min_value: float, max_value: float) -> List[float]:
    """Return the 1-2-5-per-decade "friendly step" sequence covering
    [min_value, max_value], e.g. ... 1, 2, 5, 10, 20, 50 ...

    This is the standard step pattern real oscilloscope Scale-type
    controls snap to, and matches the Programming Manual's repeated
    "the return value will be rounded to the nearest value" notes on
    NR3 scale parameters (vertical scale, horizontal scale, probe
    ratio). Presenting only these values in the GUI (instead of a free
    text field) means what you pick is what the instrument actually
    uses, with no silent rounding surprise.
    """

    start_exponent = math.floor(math.log10(min_value))
    end_exponent = math.ceil(math.log10(max_value))
    values = []
    for exponent in range(start_exponent, end_exponent + 1):
        for mantissa in (1, 2, 5):
            value = mantissa * (10.0 ** exponent)
            if min_value * 0.999 <= value <= max_value * 1.001:
                values.append(value)
    return values


# (lower-bound, prefix) pairs, largest factor last so the search below can
# just keep matching while the value still qualifies for a bigger prefix.
_SI_PREFIXES = [
    (1e-9, "n"),
    (1e-6, "u"),
    (1e-3, "m"),
    (1.0, ""),
    (1e3, "k"),
]


def _format_engineering(value: float, unit: str) -> str:
    """Format a value with the closest-fitting SI prefix, e.g. 0.001 -> '1 mV'."""

    chosen_factor, chosen_prefix = 1.0, ""
    for factor, prefix in _SI_PREFIXES:
        if value >= factor * 0.999:
            chosen_factor, chosen_prefix = factor, prefix
    scaled = value / chosen_factor
    return f"{scaled:g} {chosen_prefix}{unit}"


def _populate_scale_combo(combo: QComboBox, values: List[float], unit: str) -> None:
    """Fill a combo box with (formatted label, numeric value) pairs."""

    for value in values:
        combo.addItem(_format_engineering(value, unit), value)


# Vertical scale (BASE, i.e. at probe ratio 1x): the instrument's real
# hardware range is 500 uV/div .. 10 V/div. Probe ratio and Scale are
# two independent settings:
#   - Probe ratio (:CHx:PRObe:GAIN) only affects what the instrument
#     displays -- it multiplies the on-screen V/div by the ratio, with
#     no effect on the real analog front end. This works correctly on
#     this instrument; there is nothing to compensate for here.
#   - Scale (:CHx:SCALe) sets the real gain at the input. This is where
#     the one confirmed firmware bug lives (see
#     _SCALE_FIRMWARE_QUIRK_FACTOR below).
# The GUI's Scale combo shows this base list multiplied by whatever
# probe ratio is currently selected (effective = level * ratio, purely
# a label -- see ChannelPanel._populate_scale_combo_for_ratio), the
# same way the instrument's own on-screen display works.
BASE_VERTICAL_SCALE_VALUES = _generate_1_2_5_values(5e-4, 10.0)

# Horizontal (timebase) scale: 1 ns/div .. 100 s/div. This is a generous
# superset -- the ADS824A's real minimum/maximum scale depends on its
# sample rate and has not been independently verified against this list,
# so treat values at the extreme ends as untested until confirmed.
HORIZONTAL_SCALE_VALUES = _generate_1_2_5_values(1e-9, 100.0)

# Probe attenuation ratio: 0.001x .. 1000x (covers both standard voltage
# probes such as 1x/10x/100x and low-ratio current probes). Confirmed
# correct as-is -- no compensation needed for this one.
PROBE_RATIO_VALUES = _generate_1_2_5_values(1e-3, 1000.0)

# The one confirmed firmware bug on this instrument: :CHx:SCALe's raw
# value is silently divided by 10 by the instrument's own firmware
# before being applied to the real analog front end (confirmed by
# sending raw SCALe=100 and getting a real 10 V/div result). To make
# the real gain equal some intended value, send 10x that value. This
# is unconditional and independent of probe ratio -- ratio changes
# never need a fresh Scale write (see _on_probe_gain_changed).
_SCALE_FIRMWARE_QUIRK_FACTOR = 10.0


def _load_waveform_csv(csv_path: Path) -> List[Tuple[str, List[float], List[float]]]:
    """Load a waveform CSV: a time column followed by one or more voltage
    columns, all sharing the same time axis.

    This covers both shapes this demo (and the scope itself) can produce:
    - 2 columns (Time, Voltage) -- save_channel_waveform_csv(),
      save_channel_waveform_csv_full_record().
    - Time + up to 4 voltage columns, one per channel -- the native Save
      subsystem's CSV, which writes all enabled channels into one file
      together when :SAVe:WAVEform:SOUrce is set to ALL.

    A header row is skipped automatically if the first row isn't itself
    numeric; when present, its column labels (e.g. "CH1", "Channel 2 (V)")
    are used as the returned channel labels, otherwise columns default to
    "CH1".."CHn".

    Returns a list of (label, times, voltages) tuples, one per voltage
    column -- always at least one entry.
    """

    with csv_path.open("r", newline="", encoding="utf-8-sig") as csv_file:
        rows = list(csv.reader(csv_file))

    if not rows:
        raise RuntimeError("The selected file is empty.")

    header: Optional[List[str]] = None
    start_index = 0
    try:
        float(rows[0][0])
    except (ValueError, IndexError):
        header = rows[0]
        start_index = 1  # first row looks like a header -- skip it

    data_rows: List[List[float]] = []
    for row in rows[start_index:]:
        if len(row) < 2:
            continue
        try:
            values = [float(cell) for cell in row]
        except ValueError:
            continue
        data_rows.append(values)

    if len(data_rows) < 2:
        raise RuntimeError(
            "Could not find at least 2 numeric data rows in this file. "
            "Expected a CSV with a time column followed by one or more "
            "voltage columns."
        )

    column_count = min(len(row) for row in data_rows)
    if column_count < 2:
        raise RuntimeError(
            "Each row needs a time column plus at least one voltage column."
        )

    times = [row[0] for row in data_rows]

    channels: List[Tuple[str, List[float], List[float]]] = []
    for column_index in range(1, column_count):
        if header and column_index < len(header) and header[column_index].strip():
            label = header[column_index].strip()
        else:
            label = f"CH{column_index}"
        voltages = [row[column_index] for row in data_rows]
        channels.append((label, times, voltages))

    return channels


def _compute_fft(times: List[float], voltages: List[float]) -> Tuple[List[float], List[float], float]:
    """Compute an FFT magnitude spectrum from (times, voltages).

    Returns (frequencies, magnitude, sample_rate). Requires numpy --
    raises a RuntimeError with an actionable install hint if it is
    missing, rather than a raw ImportError.

    The sample interval is taken as the *median* consecutive time
    difference rather than just times[1]-times[0], so this tolerates
    minor jitter/rounding in a CSV's time column (this demo's own CSV
    output is exactly uniform, but a CSV from another source might not
    be).
    """

    try:
        import numpy as np
    except ImportError as error:
        raise RuntimeError(
            "This feature needs numpy, which is not installed in this "
            "Python environment. Install it with: pip install numpy"
        ) from error

    time_array = np.array(times, dtype=float)
    diffs = np.diff(time_array)
    if len(diffs) == 0 or np.any(diffs <= 0):
        raise RuntimeError(
            "The time column must be strictly increasing to compute an FFT."
        )
    sample_interval = float(np.median(diffs))
    sample_rate = 1.0 / sample_interval

    voltage_array = np.array(voltages, dtype=float)
    voltage_array = voltage_array - np.mean(voltage_array)  # drop DC component

    spectrum = np.fft.rfft(voltage_array)
    frequencies = np.fft.rfftfreq(len(voltage_array), d=sample_interval)
    magnitude = np.abs(spectrum) / len(voltage_array)

    return list(frequencies), list(magnitude), sample_rate


class ScpiInstrumentController:
    """Wrap all VISA and SCPI operations used by this demo.

    The GUI classes call the public methods in this class instead of sending
    SCPI commands directly. This keeps the instrument workflow easy to reuse
    when customers add their own buttons or replace the PyQt5 interface.

    Command syntax below is taken from the ADS Series Programming Manual
    (sections 2.2 Autoset, 2.3 ACQuire, 2.6 Vertical/CH<x>, 2.15 HORizontal,
    2.19 Measurement, and 2.23 Trigger Base & Edge).
    """

    def __init__(self) -> None:
        self.resource_manager: Optional[visa.ResourceManager] = None
        self.instrument = None
        self.instrument_address: Optional[str] = None
        self.output_directory = self._get_documents_directory()

    @staticmethod
    def _get_documents_directory() -> Path:
        """Return the current user's Documents folder with platform fallbacks."""

        if sys.platform.startswith("win"):
            try:
                class GUID(ctypes.Structure):
                    _fields_ = [
                        ("Data1", ctypes.c_ulong),
                        ("Data2", ctypes.c_ushort),
                        ("Data3", ctypes.c_ushort),
                        ("Data4", ctypes.c_ubyte * 8),
                    ]

                # FOLDERID_Documents resolves redirected and localized Documents
                # folders better than building a path from USERPROFILE manually.
                folder_id = GUID(
                    0xFDD39AD0,
                    0x238F,
                    0x46AF,
                    (ctypes.c_ubyte * 8)(
                        0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7
                    ),
                )
                path_pointer = wintypes.LPWSTR()
                result = ctypes.windll.shell32.SHGetKnownFolderPath(
                    ctypes.byref(folder_id),
                    0,
                    None,
                    ctypes.byref(path_pointer),
                )
                if result == 0 and path_pointer.value:
                    documents_path = Path(path_pointer.value)
                    ctypes.windll.ole32.CoTaskMemFree(path_pointer)
                    documents_path.mkdir(parents=True, exist_ok=True)
                    return documents_path
            except Exception:
                pass

        documents_path = Path.home() / "Documents"
        documents_path.mkdir(parents=True, exist_ok=True)
        return documents_path

    def _get_resource_manager(self) -> visa.ResourceManager:
        """Create the VISA resource manager lazily and reuse it afterwards.

        Creating the manager can fail if the computer has no VISA backend
        installed. Keeping this in one method lets the selection window report
        that error during scanning instead of failing before the GUI appears.
        """

        if self.resource_manager is None:
            try:
                # Prefer a system VISA library (NI-VISA, Keysight IO Libraries)
                # when one is installed, since it also covers USB/GPIB.
                self.resource_manager = visa.ResourceManager()
            except Exception:
                # Fall back to the pure-Python pyvisa-py backend. This covers
                # LAN (TCPIP SOCKET / VXI-11) instruments without requiring
                # any vendor VISA driver to be installed on this computer.
                self.resource_manager = visa.ResourceManager("@py")
        return self.resource_manager

    def scan_instruments(self) -> List[str]:
        """Return all VISA resource addresses currently discovered by PyVISA."""

        resource_manager = self._get_resource_manager()
        return list(resource_manager.list_resources())

    def connect(self, resource_address: str) -> None:
        """Open the selected VISA resource and prepare it for SCPI traffic.

        Any scanned VXI-11-style TCPIP/INSTR address is rewritten to this
        scope's actually-working raw-socket form first (see
        _socket_resource_for_tcpip_instr) -- callers never need to do this
        themselves, and self.instrument_address always ends up holding the
        address that really works, for display and for reconnect()."""

        resource_address = _socket_resource_for_tcpip_instr(resource_address)
        self.close_instrument()
        resource_manager = self._get_resource_manager()
        self.instrument = resource_manager.open_resource(resource_address)
        self.instrument_address = resource_address

        # These values are common for SCPI instruments. Adjust them if a
        # specific model requires another terminator or a longer timeout.
        self.instrument.timeout = 30000
        self.instrument.chunk_size = 1024 * 1024

        # A raw TCPIP SOCKET resource (as opposed to VXI-11's ::inst0::INSTR)
        # has no built-in message framing, so PyVISA does not know where one
        # SCPI response ends and the next begins unless we tell it. This
        # scope's socket service (port 3000, confirmed via its web page)
        # terminates each response with a single '\n'.
        if "SOCKET" in resource_address.upper():
            self.instrument.read_termination = "\n"
            self.instrument.write_termination = "\n"

        self._initialize_channels()

    def _initialize_channels(self) -> None:
        """Put every channel into a known state (ratio=1x, 1 V/div) right
        after connecting, through the same public methods the GUI uses --
        so the always-on Scale compensation (_SCALE_FIRMWARE_QUIRK_FACTOR)
        is applied here too, and the instrument actually starts out at a
        real 1 V/div matching the GUI's own default controls."""

        for channel in (1, 2, 3, 4):
            self.set_channel_probe_gain(channel, 1.0)
            self.set_channel_scale(channel, 1.0)

    def _require_instrument(self):
        """Return the active instrument or raise a clear error for the GUI."""

        if self.instrument is None:
            raise RuntimeError("No instrument is connected.")
        return self.instrument

    def _write(self, command: str) -> None:
        """Send a SCPI setting command with no reply."""

        self._require_instrument().write(command)

    def _query(self, command: str) -> str:
        """Send a SCPI query and return the trimmed text reply."""

        return self._require_instrument().query(command).strip()

    # ------------------------------------------------------------------
    # General
    # ------------------------------------------------------------------

    def query_idn(self) -> str:
        """Send the standard identification query and return the response."""

        return self._query("*IDN?")

    # ------------------------------------------------------------------
    # Acquisition / run control (2.2 Autoset, 2.3 ACQuire, trigger force)
    # ------------------------------------------------------------------

    def autoset(self) -> str:
        """Run the instrument's automatic setup (:AUTOSet EXECute)."""

        self._write(":AUTOSet EXECute")
        return "Autoset executed."

    def run_acquisition(self) -> str:
        """Start (resume) acquisition, equivalent to pressing Run."""

        self._write(":ACQuire:STATE RUN")
        return "Acquisition running."

    def stop_acquisition(self) -> str:
        """Stop acquisition, equivalent to pressing Stop."""

        self._write(":ACQuire:STATE STOP")
        return "Acquisition stopped."

    def single_acquisition(self) -> str:
        """Arm a single acquisition (trigger mode SINGle, then Run)."""

        self._write(":TRIGger:A:MODe SINGle")
        self._write(":ACQuire:STATE RUN")
        return "Single acquisition armed."

    def force_trigger(self) -> str:
        """Force a trigger event immediately."""

        self._write(":TRIGgerFORCe")
        return "Trigger forced."

    def set_acquire_mode(self, mode: str) -> str:
        """Set the acquisition mode (SAMPle/AVERage/PEAK/HIRes)."""

        self._write(f":ACQuire:MODe {mode}")
        return f"Acquire mode set to {mode}."

    # ------------------------------------------------------------------
    # Vertical / channel control (2.6 Vertical Command Subsystem)
    # ------------------------------------------------------------------

    def set_channel_enabled(self, channel: int, enabled: bool) -> str:
        """Turn a channel's on-screen display on or off."""

        self._write(f":SELect:CH{channel} {'ON' if enabled else 'OFF'}")
        return f"CH{channel} display {'enabled' if enabled else 'disabled'}."

    def set_channel_scale(self, channel: int, volts_per_div: float) -> str:
        """Set a channel's *raw* vertical scale in volts/division -- the
        value :CHx:SCALe would mean at probe ratio 1x, before the
        ChannelPanel's ratio-multiplied combo box divides back down to
        this. Internally compensates for the firmware quirk described
        above _SCALE_FIRMWARE_QUIRK_FACTOR, so callers never need to
        think about it."""

        raw_value = volts_per_div * _SCALE_FIRMWARE_QUIRK_FACTOR
        self._write(f":CH{channel}:SCALe {raw_value}")
        return f"CH{channel} scale set near {volts_per_div} V/div."

    def set_channel_position(self, channel: int, divisions: float) -> str:
        """Set a channel's vertical position in divisions above/below center."""

        self._write(f":CH{channel}:POSition {divisions}")
        return f"CH{channel} vertical position set to {divisions} div."

    def set_channel_coupling(self, channel: int, coupling: str) -> str:
        """Set a channel's input coupling (AC/DC/GND)."""

        self._write(f":CH{channel}:COUPling {coupling}")
        return f"CH{channel} coupling set to {coupling}."

    def set_channel_bandwidth(self, channel: int, limit: str) -> str:
        """Set a channel's bandwidth limit. `limit` is 'FULl' or a frequency."""

        self._write(f":CH{channel}:BANdwidth {limit}")
        return f"CH{channel} bandwidth limit set to {limit}."

    def set_channel_invert(self, channel: int, enabled: bool) -> str:
        """Turn a channel's waveform inversion on or off."""

        self._write(f":CH{channel}:INVert {'ON' if enabled else 'OFF'}")
        return f"CH{channel} invert {'enabled' if enabled else 'disabled'}."

    def set_channel_probe_gain(self, channel: int, ratio: float) -> str:
        """Set a channel's probe attenuation ratio (e.g. 10 for a 10X probe)."""

        self._write(f":CH{channel}:PRObe:GAIN {ratio}")
        return f"CH{channel} probe ratio set to {ratio}x."

    def _fetch_channel_waveform(self, channel: int) -> Tuple[List[float], List[float]]:
        """Fetch a channel's waveform as parallel (times, voltages) lists.

        Shared by save_channel_waveform_csv() and save_channel_fft_png() so
        both work from the exact same acquisition/scaling/averaging logic.

        :CURVe? only returns raw digitizer codes (with :DATa:ENCdg ASCii and
        :DATa:WIDth 2, each point is an integer -32768..32767) and no time
        axis at all -- on their own those are neither real voltages nor
        associated with a point in time. The WFMOutpre subsystem (section
        2.24 of the Programming Manual) describes exactly how to turn that
        raw stream into both:

            voltage = (raw_code - YOFf) * YMULt + YZEro
            time    = XZEro + index * XINcr

        (the standard Tektronix-style curve-scaling formula; XZEro/XINcr are
        relative to the trigger position and describe the *complete*
        on-instrument waveform, so they line up directly with `index` as
        long as :DATa:STARt is 1, which is what this method explicitly
        sets it to below).

        On this instrument, :DATa:TYPe SCREEN returns two raw samples per
        displayed column rather than one continuous time series: a local
        minimum and maximum captured during that column's time slot (a
        peak-detect/envelope-style compression tied to display resolution).
        Treating all raw samples as one sequential trace zig-zags between
        each column's min and max, which reads as noise. Confirmed
        empirically against the on-screen trace: averaging each consecutive
        (min, max) pair recovers the real signal. So every two raw samples
        are combined into one output point: voltage is the average of the
        pair, and time is the average of the two points' individual
        timestamps. Because pairs always advance by exactly 2 raw indices,
        the resulting times are evenly spaced -- convenient for feeding
        straight into an FFT.

        :DATa INIT resets :DATa:STARt/:DATa:STOP to 1/10000, which silently
        caps how much of the acquired record :CURVe? can return regardless
        of the configured acquisition record length. This method instead
        points STARt/STOP at the full current record length (queried live
        from :HORizontal:RECordlength?) so :CURVe? is asked for everything
        that was actually captured. In practice this instrument's SCREEN
        readback caps out at screen resolution regardless (see the README
        for the empirical test that established this), but setting it
        explicitly is still correct and harmless.
        """

        instrument = self._require_instrument()

        # Configure waveform transfer before querying the relatively large
        # screen waveform data from the selected channel.
        instrument.write(":DATa INIT")
        instrument.write(":DATa:TYPe SCREEN")
        instrument.write(f":DATa:SOUrce CH{channel}")
        instrument.write(":DATa:WIDth 2")
        instrument.write(":DATa:ENCdg ASCii")

        # Ask for the full acquired record instead of the 1..10000 default
        # left behind by :DATa INIT above.
        record_length = int(float(self._query(":HORizontal:RECordlength?")))
        instrument.write(":DATa:STARt 1")
        instrument.write(f":DATa:STOP {record_length}")

        # Scaling/timing parameters that describe the waveform :DATa:SOUrce
        # currently points at -- must be read after DATa:SOUrce is set and
        # before it changes again.
        y_mult = float(self._query(":WFMOutpre:YMUlt?"))
        y_off = float(self._query(":WFMOutpre:YOFf?"))
        y_zero = float(self._query(":WFMOutpre:YZEro?"))
        x_incr = float(self._query(":WFMOutpre:XINcr?"))
        x_zero = float(self._query(":WFMOutpre:XZEro?"))

        # A larger record length takes longer to transfer as ASCII text
        # over the LAN link than the default timeout allows for. Scale the
        # timeout up for this one transfer, then restore it afterwards.
        previous_timeout = instrument.timeout
        instrument.timeout = max(previous_timeout, 5000 + record_length // 20)
        try:
            waveform_data = instrument.query(":CURVe?").strip()
        finally:
            instrument.timeout = previous_timeout

        csv_data = waveform_data.replace(":CURVE", "").strip()
        raw_samples = [
            sample.strip()
            for sample in csv_data.split(",")
            if sample.strip()
        ]

        # Pair up consecutive raw samples (min/max per screen column) and
        # average each pair. Any unpaired trailing sample (an odd-length
        # response, not expected on this instrument) is dropped.
        pair_count = len(raw_samples) // 2

        times: List[float] = []
        voltages: List[float] = []
        for pair_index in range(pair_count):
            first_index = pair_index * 2
            second_index = first_index + 1

            first_voltage = (float(raw_samples[first_index]) - y_off) * y_mult + y_zero
            second_voltage = (float(raw_samples[second_index]) - y_off) * y_mult + y_zero
            voltages.append((first_voltage + second_voltage) / 2)

            first_time = x_zero + first_index * x_incr
            second_time = x_zero + second_index * x_incr
            times.append((first_time + second_time) / 2)

        return times, voltages

    def save_channel_waveform_csv(self, channel: int) -> str:
        """Fetch a channel's waveform and save Time(s)/Voltage(V) as CSV.

        See _fetch_channel_waveform() for the acquisition/scaling/pairing
        details -- this method just writes the result to a CSV file.
        """

        csv_path = self.output_directory / f"waveform_ch{channel}.csv"
        times, voltages = self._fetch_channel_waveform(channel)

        with csv_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(["Time (s)", "Voltage (V)"])
            for time_seconds, voltage in zip(times, voltages):
                writer.writerow([f"{time_seconds:.9g}", f"{voltage:.6g}"])

        return (
            f"File saved successfully ({len(voltages)} averaged points).\n\n"
            f"Saved location:\n{csv_path}"
        )

    def save_channel_fft_png(self, channel: int) -> str:
        """Fetch a channel's waveform and save a plot of its FFT magnitude
        spectrum, computed entirely in software from the same time-domain
        samples save_channel_waveform_csv() uses.

        This does not rely on the instrument's own FFT display feature at
        all -- once the time-domain samples are on the PC, numpy can
        compute the spectrum directly. It is a one-shot snapshot (computed
        fresh each time the button is clicked), not a live/continuously
        updating spectrum -- the same query/response round trip used
        everywhere else in this demo, not a streaming connection.

        This saves a static PNG snapshot. For an interactive plot (zoom
        into a frequency range, toggle a log/dB scale to see low
        amplitudes next to a big peak) use the "FFT from CSV..." button
        in the top bar instead, which opens FftPlotWindow against a CSV
        you choose -- including a CSV this method's sibling
        save_channel_waveform_csv() just saved.

        Requires numpy and matplotlib, which are not required by the rest
        of this demo -- install with `pip install numpy matplotlib` if
        this raises an import error.
        """

        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError as error:
            raise RuntimeError(
                "This feature needs matplotlib, which is not installed in "
                "this Python environment. Install it with: "
                "pip install matplotlib"
            ) from error

        times, voltages = self._fetch_channel_waveform(channel)
        if len(times) < 2:
            raise RuntimeError("Not enough samples were returned to compute an FFT.")

        frequencies, magnitude, sample_rate = _compute_fft(times, voltages)

        figure, axes = plt.subplots(figsize=(9, 5))
        axes.plot(frequencies, magnitude)
        axes.set_xlabel("Frequency (Hz)")
        axes.set_ylabel("Magnitude (V)")
        axes.set_title(
            f"CH{channel} FFT -- {len(voltages)} points, "
            f"~{sample_rate:.4g} Sa/s"
        )
        axes.grid(True, alpha=0.3)
        figure.tight_layout()

        image_path = self.output_directory / f"waveform_ch{channel}_fft.png"
        figure.savefig(image_path, dpi=150)
        plt.close(figure)

        return (
            f"FFT image saved successfully ({len(voltages)} "
            f"time-domain points, ~{sample_rate:.4g} Sa/s).\n\n"
            f"Saved location:\n{image_path}"
        )

    def save_channel_waveform_csv_full_record(self, channel: int) -> str:
        """Experimental: fetch a channel's waveform via :WAVFrm? instead of
        :DATa:TYPe SCREEN + :CURVe?, to test whether it can return more
        than the ~900-point screen-resolution cap confirmed (empirically,
        by setting Record Length to 1,000,000 and seeing no change in the
        point count) on this instrument's SCREEN-mode :CURVe? path.

        :WAVFrm? (section 2.24 of the Programming Manual) returns waveform
        preset information and curve data in a single combined response,
        rather than requiring separate :WFMOutpre:* queries plus :CURVe?.
        Per the manual's example, the response is a semicolon-separated
        list of NAME<value> fields followed by a trailing
        ":CURVE<comma-separated samples>" section.

        This scope has already proven the manual's exact command/response
        details are not always accurate for this specific firmware (see
        the LAN connection notes in the README -- VXI-11 is documented but
        does not actually work on this unit). So this method parses the
        response defensively with regular expressions instead of assuming
        exact spacing/casing, and raises a clear error (saving the raw
        response to a debug file) if a field can't be found, rather than
        silently producing a wrong result.

        Unlike save_channel_waveform_csv(), this does NOT assume the
        returned raw samples are (min, max) pairs needing averaging --
        that pairing was specific to the SCREEN/:CURVe? path. If this
        CSV's Voltage column still looks like it zig-zags between two
        envelopes when plotted, the same pairing/averaging is probably
        still happening here too and this method should be adjusted the
        same way.
        """

        instrument = self._require_instrument()
        csv_path = self.output_directory / f"waveform_ch{channel}_full_record.csv"

        instrument.write(":DATa INIT")
        instrument.write(f":DATa:SOUrce CH{channel}")
        instrument.write(":DATa:WIDth 2")
        instrument.write(":DATa:ENCdg ASCii")

        response = instrument.query(":WAVFrm?")

        curve_match = re.search(r":?CURVE\s*", response, re.IGNORECASE)
        if not curve_match:
            debug_path = self.output_directory / "wavfrm_raw_debug.txt"
            debug_path.write_text(response, encoding="utf-8")
            raise RuntimeError(
                "Could not find a CURVE section in the :WAVFrm? response. "
                f"Saved the full raw response to {debug_path} for inspection."
            )

        preamble = response[:curve_match.start()]
        curve_text = response[curve_match.end():]

        def extract_field(name: str) -> float:
            match = re.search(
                rf"{name}\s*([+-]?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)",
                preamble,
                re.IGNORECASE,
            )
            if not match:
                debug_path = self.output_directory / "wavfrm_raw_debug.txt"
                debug_path.write_text(response, encoding="utf-8")
                raise RuntimeError(
                    f"Could not find field '{name}' in the :WAVFrm? preamble. "
                    f"Saved the full raw response to {debug_path} for inspection."
                )
            return float(match.group(1))

        y_mult = extract_field("YMULT")
        y_off = extract_field("YOFF")
        y_zero = extract_field("YZERO")
        x_incr = extract_field("XINCR")
        x_zero = extract_field("XZERO")

        raw_samples = [
            sample.strip()
            for sample in curve_text.split(",")
            if sample.strip()
        ]

        with csv_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(["Time (s)", "Voltage (V)"])
            for index, raw_sample in enumerate(raw_samples):
                voltage = (float(raw_sample) - y_off) * y_mult + y_zero
                time_seconds = x_zero + index * x_incr
                writer.writerow([f"{time_seconds:.9g}", f"{voltage:.6g}"])

        return (
            f"[Experimental, :WAVFrm?] File saved successfully "
            f"({len(raw_samples)} raw points -- no min/max pairing was "
            f"applied here, so check the plotted trace for zig-zag noise "
            f"before trusting these values).\n\n"
            f"Saved location:\n{csv_path}"
        )

    # ------------------------------------------------------------------
    # Measurement (2.19 Measurement Command Subsystem, IMMed slot)
    # ------------------------------------------------------------------

    def read_measurement(self, channel: int, measurement_type: str) -> str:
        """Configure the immediate-measurement slot and read its value."""

        self._write(f":MEASUrement:IMMed:SOURCE1 CH{channel}")
        self._write(f":MEASUrement:IMMed:TYPe {measurement_type}")
        value = self._query(":MEASUrement:IMMed:VALue?")
        try:
            units = self._query(":MEASUrement:IMMed:UNIts?").strip('"')
        except Exception:
            units = ""
        return f"CH{channel} {measurement_type}: {value} {units}".strip()

    # ------------------------------------------------------------------
    # Trigger (2.23 Trigger Base & Edge Command Subsystem)
    # ------------------------------------------------------------------

    def set_trigger_type(self, trigger_type: str) -> str:
        """Set the overall trigger type (EDGe/LOGlc/PULSe/BUS/VIDeo)."""

        self._write(f":TRIGger:A:TYPe {trigger_type}")
        return f"Trigger type set to {trigger_type}."

    def set_trigger_mode(self, mode: str) -> str:
        """Set the trigger acquire mode (AUTO/NORMal/SINGle)."""

        self._write(f":TRIGger:A:MODe {mode}")
        return f"Trigger mode set to {mode}."

    def set_trigger_edge_source(self, source: str) -> str:
        """Set the edge trigger source (CH1-4/EXT/EXT/5/LINE)."""

        self._write(f":TRIGger:A:EDGE:SOUrce {source}")
        return f"Edge trigger source set to {source}."

    def set_trigger_edge_slope(self, slope: str) -> str:
        """Set the edge trigger slope (RISe/FALL)."""

        self._write(f":TRIGger:A:EDGE:SLOPe {slope}")
        return f"Edge trigger slope set to {slope}."

    def set_trigger_edge_coupling(self, coupling: str) -> str:
        """Set the edge trigger coupling (AC/DC/HFRej)."""

        self._write(f":TRIGger:A:EDGE:COUPling {coupling}")
        return f"Edge trigger coupling set to {coupling}."

    def set_trigger_level(self, channel: int, level_volts: float) -> str:
        """Set the trigger level for a specific channel, in volts."""

        self._write(f":TRIGger:A:LEVel:CH{channel} {level_volts}")
        return f"Trigger level for CH{channel} set to {level_volts} V."

    def set_trigger_level_50_percent(self) -> str:
        """Set the trigger level to 50% of the signal amplitude."""

        self._write(":TRIGger:ASETLevel")
        return "Trigger level set to 50%."

    def set_trigger_holdoff(self, seconds: float) -> str:
        """Set the trigger holdoff time, in seconds."""

        self._write(f":TRIGger:A:HOLDoff:TIMe {seconds}")
        return f"Trigger holdoff set to {seconds} s."

    # ------------------------------------------------------------------
    # Horizontal (2.15 HORizontal Command Subsystem)
    # ------------------------------------------------------------------

    def set_horizontal_scale(self, seconds_per_div: float) -> str:
        """Set the main timebase scale in seconds/division."""

        self._write(f":HORizontal:SCAle {seconds_per_div}")
        return f"Horizontal scale set near {seconds_per_div} s/div."

    def set_horizontal_delay(self, seconds: float) -> str:
        """Set the horizontal trigger position offset time, in seconds."""

        self._write(f":HORizontal:DELay:TIMe {seconds}")
        return f"Horizontal delay set to {seconds} s."

    def set_horizontal_reference_mode(self, mode: str) -> str:
        """Set the horizontal reference mode (CENTer/TRIG)."""

        self._write(f":HORizontal:HREFerence:MODE {mode}")
        return f"Horizontal reference mode set to {mode}."

    def set_horizontal_record_length(self, length: int) -> str:
        """Set the waveform record length (storage depth)."""

        self._write(f":HORizontal:RECordlength {length}")
        return f"Record length set to {length}."

    # ------------------------------------------------------------------
    # Native Save (2.22 Save Command Subsystem)
    # ------------------------------------------------------------------

    def save_waveform_native(
            self,
            file_format: str,
            path: str,
            source: str,
            filename: str,
    ) -> str:
        """Trigger the scope's own Save Command Subsystem -- the exact
        function behind the front-panel Copy button.

        This is NOT limited to screen resolution the way :CURVe?/:WAVFrm?
        turned out to be (confirmed empirically: raising Record Length or
        DATa:STARt/STOP had no effect on those). The user confirmed
        directly on the instrument's own front-panel Copy menu that this
        mechanism can export the full configured record (e.g. all
        1,000,000 samples) with a time column, for all channels together.

        This method only *triggers* the save on the instrument -- it does
        not transfer the resulting file back over LAN:
        - path="EXTERNal" writes to a USB drive plugged into the scope;
          collect the file from that drive afterwards.
        - path="INTERNal" writes to the scope's internal storage;
          retrieving that file remotely (LAN) has not been set up yet --
          this may be possible through the scope's LXI web interface or
          an FTP server if either exposes a file browser, but that has
          not been checked yet.
        """

        self._write(":SAVe:ASSIgn:TYPe WAVEform")
        self._write(f":SAVe:WAVEform:FILEFormat {file_format}")
        if source == "ALL":
            self._write(":SAVe:WAVEform:SOUrce ALL")
        else:
            self._write(f":SAVe:WAVEform:SOUrce:{source} ON")
        self._write(f":SAVe:PATH {path}")
        self._write(f":SAVe:WAVEform {filename}")

        if path == "EXTERNal":
            location_note = "Check the USB drive plugged into the scope for the file."
        else:
            location_note = (
                "Saved to the scope's internal storage. Retrieving it over "
                "LAN has not been set up in this demo yet -- check the "
                "scope's own file/USB menu for now."
            )

        return (
            f"Save triggered: format={file_format}, source={source}, "
            f"path={path}, filename='{filename}'.\n\n{location_note}"
        )

    # ------------------------------------------------------------------
    # Screen capture (2.9 Display & XY Command Subsystem)
    # ------------------------------------------------------------------

    def save_screen_image_png(self) -> str:
        """Fetch the current screen image data and save it as a local PNG file."""

        instrument = self._require_instrument()
        image_path = self.output_directory / "screen_image.png"

        # PNG files start with the fixed 8-byte signature
        # 89 50 4E 47 0D 0A 1A 0A -- note the 0x0A ('\n') bytes built right
        # into that signature. Two pyvisa-py settings used for this scope's
        # SOCKET connection get in the way of reading that:
        #
        # 1. read_termination='\n' stops the read the instant it sees that
        #    byte, which happens almost immediately, inside the signature
        #    itself. That is why this used to come back as a truncated
        #    few-byte file.
        #
        # 2. Less obviously: pyvisa-py's TCPIPSocketSession defaults
        #    suppress_end_enabled to True for SOCKET resources. Looking at
        #    its read() implementation, that setting disables the "no more
        #    data is coming right now, return what has been received so
        #    far" behaviour. Without it, a read that does not fill the
        #    entire requested buffer (guaranteed here, since a screen
        #    capture's exact size is unknown and read_raw() defaults to
        #    asking for a full chunk_size) just blocks until the whole
        #    instrument.timeout elapses and then raises -- discarding
        #    everything that was already received in the process. That is
        #    why a plain read_raw() with termination disabled either
        #    returned nothing or, if it partially worked, left unread bytes
        #    on the socket that corrupted every SCPI exchange afterwards
        #    (seen as *IDN?/period-measurement timeouts following a PNG
        #    capture).
        #
        # Disabling both for this one binary read makes read_raw() do the
        # right thing: keep reading chunks as they arrive and return as
        # soon as the instrument goes quiet, with nothing left behind.
        previous_read_termination = instrument.read_termination
        suppress_end_attribute = visa.constants.ResourceAttribute.suppress_end_enabled
        previous_suppress_end = instrument.get_visa_attribute(suppress_end_attribute)
        try:
            instrument.read_termination = None
            instrument.set_visa_attribute(suppress_end_attribute, False)
            instrument.write(":DISplay:DATA?")
            raw_data = instrument.read_raw()
        finally:
            instrument.read_termination = previous_read_termination
            instrument.set_visa_attribute(suppress_end_attribute, previous_suppress_end)

        if not raw_data:
            raise RuntimeError("No screen data was received from the instrument.")

        image_data = raw_data.replace(b':DISPLAY:DATA', b'').strip()

        if image_data and len(image_data) > 4:
            image_data = image_data[4:]

        if image_data.endswith(b"\n"):
            image_data = image_data[:-1]

        if not image_data.startswith(PNG_SIGNATURE):
            # The header/offset assumptions above may not exactly match this
            # firmware's response format. Save the full raw response too so
            # the parsing above can be corrected based on what was actually
            # received, instead of failing silently with a corrupt PNG.
            debug_path = self.output_directory / "screen_image_raw_debug.bin"
            debug_path.write_bytes(raw_data)
            raise RuntimeError(
                "Parsed data does not start with the PNG signature. "
                f"Saved the full raw response to {debug_path} for inspection."
            )

        image_path.write_bytes(image_data)

        return f"File saved successfully.\n\nSaved location:\n{image_path}"

    def close_instrument(self) -> None:
        """Close the active instrument session without closing the manager."""

        if self.instrument is not None:
            try:
                self.instrument.close()
            except Exception:
                # The link may already be broken (instrument powered off,
                # LAN drop, etc). Closing an already-dead session can itself
                # raise -- that must never block opening a fresh one, which
                # is exactly the situation reconnect() below is meant to
                # recover from.
                pass
            finally:
                self.instrument = None
                self.instrument_address = None

    def reconnect(self, resource_address: str) -> str:
        """Close the current session (if any) and reopen the same address.

        Use this after the instrument was powered off/on again or the LAN
        link dropped for a moment. It repeats the same connect() steps
        (including the SOCKET termination setup) without needing to close
        and relaunch the whole application.
        """

        self.connect(resource_address)
        return f"Reconnected to {self.instrument_address}."

    def close(self) -> None:
        """Release both the instrument session and the VISA resource manager."""

        self.close_instrument()
        if self.resource_manager is not None:
            self.resource_manager.close()
            self.resource_manager = None


class InstrumentSelectionWindow(QWidget):
    """Startup window that scans instruments and lets the user connect one."""

    def __init__(self, controller: ScpiInstrumentController) -> None:
        super().__init__()
        self.controller = controller
        self.control_window: Optional[InstrumentControlWindow] = None

        self.setWindowTitle("ADS824A Toolkit - Select Instrument")
        self.resize(520, 420)

        self.status_label = QLabel("Scanning instruments...")
        self.instrument_list = QListWidget()
        self.instrument_list.itemSelectionChanged.connect(
            self._update_connect_button_state
        )

        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.scan_instruments)

        self.connect_button = QPushButton("Connect")
        self.connect_button.setEnabled(False)
        self.connect_button.clicked.connect(self.connect_selected_instrument)

        button_layout = QHBoxLayout()
        button_layout.addWidget(self.refresh_button)
        button_layout.addStretch(1)
        button_layout.addWidget(self.connect_button)

        # LAN instruments (including this scope) are usually not found by
        # scan_instruments()/list_resources(): that call mainly discovers
        # USB/GPIB devices, not devices sitting on the network. So we also
        # offer a manual VISA address field, pre-filled with this scope's
        # known LAN address, as a direct alternative to the scanned list.
        separator = QFrame()
        separator.setFrameShape(QFrame.HLine)
        separator.setFrameShadow(QFrame.Sunken)

        manual_label = QLabel("Or enter a VISA address manually (e.g. a LAN instrument):")

        self.manual_address_field = QLineEdit()
        self.manual_address_field.setText(DEFAULT_LAN_RESOURCE)
        self.manual_address_field.returnPressed.connect(self.connect_manual_address)

        self.manual_connect_button = QPushButton("Connect to Address")
        self.manual_connect_button.clicked.connect(self.connect_manual_address)

        manual_layout = QHBoxLayout()
        manual_layout.addWidget(self.manual_address_field, 1)
        manual_layout.addWidget(self.manual_connect_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.status_label)
        layout.addWidget(self.instrument_list, 1)
        layout.addLayout(button_layout)
        layout.addWidget(separator)
        layout.addWidget(manual_label)
        layout.addLayout(manual_layout)

        # Start the scan after the window is visible, so long VISA discovery
        # does not make the application appear frozen during startup.
        QTimer.singleShot(0, self.scan_instruments)

    def scan_instruments(self) -> None:
        """Scan VISA resources and display them in the selectable list."""

        self.instrument_list.clear()
        self.connect_button.setEnabled(False)
        self.status_label.setText("Scanning instruments...")
        QApplication.setOverrideCursor(Qt.WaitCursor)

        try:
            instruments = self.controller.scan_instruments()
        except Exception as error:
            self.status_label.setText("Instrument scan failed.")
            QMessageBox.critical(self, "Scan Failed", self._format_error(error))
            return
        finally:
            QApplication.restoreOverrideCursor()

        if not instruments:
            self.status_label.setText("No connected instruments found.")
            return

        self.instrument_list.addItems(instruments)
        self.status_label.setText("Select an instrument, then click Connect.")

    def _update_connect_button_state(self) -> None:
        """Enable Connect only when the user has selected a resource."""

        self.connect_button.setEnabled(
            self.instrument_list.currentItem() is not None
        )

    def connect_selected_instrument(self) -> None:
        """Connect to the resource selected in the scanned list."""

        selected_item = self.instrument_list.currentItem()
        if selected_item is None:
            return

        self._connect_to_address(selected_item.text())

    def connect_manual_address(self) -> None:
        """Connect to whatever VISA address the user typed in manually.

        This is the path used for LAN instruments such as this scope, since
        they typically do not show up in scan_instruments()'s results.
        """

        resource_address = self.manual_address_field.text().strip()
        if not resource_address:
            QMessageBox.warning(self, "Missing Address", "Enter a VISA address first.")
            return

        self._connect_to_address(resource_address)

    def _connect_to_address(self, resource_address: str) -> None:
        """Shared connect logic for both the scanned list and manual entry."""

        QApplication.setOverrideCursor(Qt.WaitCursor)
        self.connect_button.setEnabled(False)
        self.manual_connect_button.setEnabled(False)

        try:
            self.controller.connect(resource_address)
        except Exception as error:
            QMessageBox.critical(self, "Connection Failed", self._format_error(error))
            self._update_connect_button_state()
            self.manual_connect_button.setEnabled(True)
            return
        finally:
            QApplication.restoreOverrideCursor()

        # connect() may have rewritten resource_address (e.g. a scanned
        # VXI-11-style TCPIP/INSTR address into the working SOCKET form) --
        # use the address it actually ended up using, not the one we passed
        # in, so the control window's display and its future reconnect()
        # calls use the address that really works.
        self.control_window = InstrumentControlWindow(
            controller=self.controller,
            resource_address=self.controller.instrument_address,
        )
        self.control_window.show()
        self.close()

    @staticmethod
    def _format_error(error: Exception) -> str:
        """Return a short error string that is useful to end users."""

        return f"{type(error).__name__}: {error}"


class ChannelPanel(QWidget):
    """One tab's worth of controls for a single analog channel.

    Every control calls back into the owning window's `run_action()` so
    results and errors are reported the same way everywhere in the app, and
    every button is disabled while an action is in flight (see
    InstrumentControlWindow._run_action).
    """

    def __init__(
            self,
            channel: int,
            controller: ScpiInstrumentController,
            run_action: Callable[[str, Callable[[], str]], None],
    ) -> None:
        super().__init__()
        self.channel = channel
        self.controller = controller
        self.run_action = run_action
        self.controls: List[QWidget] = []

        form = QFormLayout()

        title = QLabel(f"<b>CH{channel}</b>")
        form.addRow(title)

        self.enabled_checkbox = QCheckBox(f"Show CH{channel} on screen")
        self.enabled_checkbox.setChecked(True)
        self.enabled_checkbox.stateChanged.connect(self._on_enabled_changed)
        form.addRow(self.enabled_checkbox)
        self.controls.append(self.enabled_checkbox)

        # Scale/probe ratio use combo boxes of the real 1-2-5 "friendly
        # step" values the instrument snaps to (see BASE_VERTICAL_SCALE_
        # VALUES / PROBE_RATIO_VALUES), instead of a free-text field -- this
        # avoids entering a value the instrument would silently round to
        # something else. Selecting an item applies immediately (mouse
        # wheel or click both work on a focused combo box), with no
        # separate Set button, the same way the coupling/bandwidth combos
        # already behave.
        self.scale_combo = QComboBox()
        self._populate_scale_combo_for_ratio(1.0)
        self.scale_combo.currentIndexChanged.connect(self._on_scale_changed)
        form.addRow("Scale (V/div):", self.scale_combo)
        self.controls.append(self.scale_combo)

        # Vertical position is not a quantized "friendly step" setting on
        # this instrument, so it stays continuous -- but as a spin box
        # instead of a free-text field, so the arrows/mouse wheel adjust
        # it immediately (setKeyboardTracking(False) means typed digits
        # only apply on Enter/focus-out, not on every keystroke, while the
        # arrows/wheel still apply on every step -- see valueChanged
        # handler below).
        self.position_spin = QDoubleSpinBox()
        self.position_spin.setRange(-100.0, 100.0)
        self.position_spin.setDecimals(2)
        self.position_spin.setSingleStep(0.1)
        self.position_spin.setSuffix(" div")
        self.position_spin.setKeyboardTracking(False)
        self.position_spin.valueChanged.connect(self._on_position_changed)
        form.addRow("Vertical position:", self.position_spin)
        self.controls.append(self.position_spin)

        self.coupling_combo = QComboBox()
        self.coupling_combo.addItems(["DC", "AC", "GND"])
        self.coupling_combo.currentTextChanged.connect(self._on_coupling_changed)
        form.addRow("Coupling:", self.coupling_combo)
        self.controls.append(self.coupling_combo)

        self.bandwidth_combo = QComboBox()
        self.bandwidth_combo.addItem("Full bandwidth", "FULl")
        self.bandwidth_combo.addItem("20 MHz limit", "20E+6")
        self.bandwidth_combo.currentIndexChanged.connect(self._on_bandwidth_changed)
        form.addRow("Bandwidth limit:", self.bandwidth_combo)
        self.controls.append(self.bandwidth_combo)

        self.invert_checkbox = QCheckBox("Invert waveform")
        self.invert_checkbox.stateChanged.connect(self._on_invert_changed)
        form.addRow(self.invert_checkbox)
        self.controls.append(self.invert_checkbox)

        self.probe_combo = QComboBox()
        _populate_scale_combo(self.probe_combo, PROBE_RATIO_VALUES, "x")
        self.probe_combo.setCurrentText(_format_engineering(1.0, "x"))
        self.probe_combo.currentIndexChanged.connect(self._on_probe_gain_changed)
        form.addRow("Probe ratio:", self.probe_combo)
        self.controls.append(self.probe_combo)

        self.waveform_button = QPushButton(f"Save CH{channel} Waveform CSV")
        self.waveform_button.clicked.connect(self._on_save_waveform)
        form.addRow(self.waveform_button)
        self.controls.append(self.waveform_button)

        self.waveform_full_record_button = QPushButton(
            f"Save CH{channel} Waveform CSV (Full Record, experimental)"
        )
        self.waveform_full_record_button.setToolTip(
            "Tries :WAVFrm? instead of :DATa:TYPe SCREEN + :CURVe?, to see "
            "whether it can return more than the ~900-point screen cap. "
            "Not yet verified against real hardware -- check the output "
            "message and the resulting CSV carefully."
        )
        self.waveform_full_record_button.clicked.connect(
            self._on_save_waveform_full_record
        )
        form.addRow(self.waveform_full_record_button)
        self.controls.append(self.waveform_full_record_button)

        self.fft_button = QPushButton(f"Save CH{channel} FFT (PNG)")
        self.fft_button.setToolTip(
            "Computes the FFT magnitude spectrum in software (numpy) from "
            "a freshly captured waveform and saves a plot -- a one-shot "
            "snapshot, not a live spectrum. Requires numpy and matplotlib "
            "(pip install numpy matplotlib)."
        )
        self.fft_button.clicked.connect(self._on_save_fft)
        form.addRow(self.fft_button)
        self.controls.append(self.fft_button)

        self.measurement_combo = QComboBox()
        self.measurement_combo.addItems(MEASUREMENT_TYPES)
        self.measurement_combo.setCurrentText("PERIod")
        measurement_button = QPushButton("Read Measurement")
        measurement_button.clicked.connect(self._on_read_measurement)
        form.addRow("Measurement:", self._paired(self.measurement_combo, measurement_button))
        self.controls += [self.measurement_combo, measurement_button]

        self.setLayout(form)

        # Visual separation between the 4 channel panels now that they all
        # sit together in one grid instead of one-per-tab.
        self.setObjectName("channelPanel")
        self.setStyleSheet(
            "#channelPanel { border: 1px solid palette(mid);"
            " border-radius: 4px; }"
        )

    @staticmethod
    def _paired(widget: QWidget, button: QPushButton) -> QWidget:
        """Lay a field/combo and its action button out on one row."""

        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(widget, 1)
        row.addWidget(button)
        return container

    def set_controls_enabled(self, enabled: bool) -> None:
        for control in self.controls:
            control.setEnabled(enabled)

    def _on_enabled_changed(self) -> None:
        enabled = self.enabled_checkbox.isChecked()
        self.run_action(
            f"CH{self.channel} display {'on' if enabled else 'off'}",
            lambda: self.controller.set_channel_enabled(self.channel, enabled),
        )

    def _populate_scale_combo_for_ratio(self, ratio: float) -> None:
        """(Re)fill the Scale combo's *labels* for a new probe ratio --
        effective label = base level * ratio, purely cosmetic, matching
        exactly how the instrument's own on-screen display reacts to a
        ratio change (confirmed correct, no bug there). The same *level*
        (list index into BASE_VERTICAL_SCALE_VALUES) stays selected
        across the change, because the real analog scale does not
        change just because the ratio label did -- so nothing is sent
        to the instrument here; see _on_probe_gain_changed."""

        level_index = max(self.scale_combo.currentIndex(), 0)
        self.scale_combo.blockSignals(True)
        self.scale_combo.clear()
        _populate_scale_combo(
            self.scale_combo,
            [level * ratio for level in BASE_VERTICAL_SCALE_VALUES],
            "V",
        )
        self.scale_combo.setCurrentIndex(min(level_index, self.scale_combo.count() - 1))
        self.scale_combo.blockSignals(False)

    def _on_scale_changed(self) -> None:
        effective_value = self.scale_combo.currentData()
        ratio = self.probe_combo.currentData()
        raw_value = effective_value / ratio
        self.run_action(
            f"CH{self.channel} scale",
            lambda: self.controller.set_channel_scale(self.channel, raw_value),
        )

    def _on_position_changed(self, value: float) -> None:
        self.run_action(
            f"CH{self.channel} position",
            lambda: self.controller.set_channel_position(self.channel, value),
        )

    def _on_coupling_changed(self, coupling: str) -> None:
        self.run_action(
            f"CH{self.channel} coupling",
            lambda: self.controller.set_channel_coupling(self.channel, coupling),
        )

    def _on_bandwidth_changed(self) -> None:
        limit = self.bandwidth_combo.currentData()
        self.run_action(
            f"CH{self.channel} bandwidth",
            lambda: self.controller.set_channel_bandwidth(self.channel, limit),
        )

    def _on_invert_changed(self) -> None:
        enabled = self.invert_checkbox.isChecked()
        self.run_action(
            f"CH{self.channel} invert",
            lambda: self.controller.set_channel_invert(self.channel, enabled),
        )

    def _on_probe_gain_changed(self) -> None:
        ratio = self.probe_combo.currentData()
        # Ratio only changes the label (the instrument's own display
        # updates itself immediately and correctly -- confirmed, no bug
        # there). Only relabel the combo here; nothing is sent for
        # Scale, since the real analog scale is unaffected by ratio.
        self._populate_scale_combo_for_ratio(ratio)
        self.run_action(
            f"CH{self.channel} probe ratio",
            lambda: self.controller.set_channel_probe_gain(self.channel, ratio),
        )

    def _on_save_waveform(self) -> None:
        self.run_action(
            f"Save CH{self.channel} Waveform CSV",
            lambda: self.controller.save_channel_waveform_csv(self.channel),
        )

    def _on_save_waveform_full_record(self) -> None:
        self.run_action(
            f"Save CH{self.channel} Waveform CSV (Full Record, experimental)",
            lambda: self.controller.save_channel_waveform_csv_full_record(self.channel),
        )

    def _on_save_fft(self) -> None:
        self.run_action(
            f"Save CH{self.channel} FFT (PNG)",
            lambda: self.controller.save_channel_fft_png(self.channel),
        )

    def _on_read_measurement(self) -> None:
        measurement_type = self.measurement_combo.currentText()
        self.run_action(
            f"CH{self.channel} {measurement_type}",
            lambda: self.controller.read_measurement(self.channel, measurement_type),
        )


class TriggerPanel(QWidget):
    """Controls for the trigger subsystem (type, mode, edge settings, level)."""

    def __init__(
            self,
            controller: ScpiInstrumentController,
            run_action: Callable[[str, Callable[[], str]], None],
    ) -> None:
        super().__init__()
        self.controller = controller
        self.run_action = run_action
        self.controls: List[QWidget] = []

        form = QFormLayout()

        self.type_combo = QComboBox()
        self.type_combo.addItems(["EDGe", "LOGlc", "PULSe", "BUS", "VIDeo"])
        self.type_combo.currentTextChanged.connect(self._on_type_changed)
        form.addRow("Trigger type:", self.type_combo)
        self.controls.append(self.type_combo)

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["AUTO", "NORMal", "SINGle"])
        self.mode_combo.currentTextChanged.connect(self._on_mode_changed)
        form.addRow("Trigger mode:", self.mode_combo)
        self.controls.append(self.mode_combo)

        self.edge_source_combo = QComboBox()
        self.edge_source_combo.addItems(
            ["CH1", "CH2", "CH3", "CH4", "EXT", "EXT/5", "LINE"]
        )
        self.edge_source_combo.currentTextChanged.connect(self._on_edge_source_changed)
        form.addRow("Edge source:", self.edge_source_combo)
        self.controls.append(self.edge_source_combo)

        self.edge_slope_combo = QComboBox()
        self.edge_slope_combo.addItems(["RISe", "FALL"])
        self.edge_slope_combo.currentTextChanged.connect(self._on_edge_slope_changed)
        form.addRow("Edge slope:", self.edge_slope_combo)
        self.controls.append(self.edge_slope_combo)

        self.edge_coupling_combo = QComboBox()
        self.edge_coupling_combo.addItems(["DC", "AC", "HFRej"])
        self.edge_coupling_combo.currentTextChanged.connect(self._on_edge_coupling_changed)
        form.addRow("Edge coupling:", self.edge_coupling_combo)
        self.controls.append(self.edge_coupling_combo)

        # Trigger level is continuous (no documented "friendly step" list),
        # so it uses a spin box: the arrows/mouse wheel apply immediately,
        # and typed values apply on Enter/focus-out (setKeyboardTracking
        # False) rather than on every keystroke. The channel combo just
        # picks which channel's level the spin box controls; it has no
        # SCPI effect of its own.
        self.level_channel_combo = QComboBox()
        self.level_channel_combo.addItems(["CH1", "CH2", "CH3", "CH4"])
        self.level_spin = QDoubleSpinBox()
        self.level_spin.setRange(-100.0, 100.0)
        self.level_spin.setDecimals(4)
        self.level_spin.setSingleStep(0.01)
        self.level_spin.setSuffix(" V")
        self.level_spin.setKeyboardTracking(False)
        self.level_spin.valueChanged.connect(self._on_level_changed)
        level_row = QWidget()
        level_layout = QHBoxLayout(level_row)
        level_layout.setContentsMargins(0, 0, 0, 0)
        level_layout.addWidget(self.level_channel_combo)
        level_layout.addWidget(self.level_spin, 1)
        form.addRow("Trigger level:", level_row)
        self.controls += [self.level_channel_combo, self.level_spin]

        level_50_button = QPushButton("Set Level to 50%")
        level_50_button.clicked.connect(self._on_set_level_50)
        form.addRow(level_50_button)
        self.controls.append(level_50_button)

        self.holdoff_spin = QDoubleSpinBox()
        self.holdoff_spin.setRange(0.0, 10.0)
        self.holdoff_spin.setDecimals(9)
        self.holdoff_spin.setSingleStep(1e-7)
        self.holdoff_spin.setSuffix(" s")
        self.holdoff_spin.setValue(1e-7)
        self.holdoff_spin.setKeyboardTracking(False)
        self.holdoff_spin.setToolTip(
            "Arrows/mouse wheel step by 100 ns. For a very different "
            "magnitude, just type the exact value and press Enter."
        )
        self.holdoff_spin.valueChanged.connect(self._on_holdoff_changed)
        form.addRow("Holdoff:", self.holdoff_spin)
        self.controls.append(self.holdoff_spin)

        force_button = QPushButton("Force Trigger")
        force_button.clicked.connect(self._on_force_trigger)
        form.addRow(force_button)
        self.controls.append(force_button)

        self.setLayout(form)

    @staticmethod
    def _paired(widget: QWidget, button: QPushButton) -> QWidget:
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(widget, 1)
        row.addWidget(button)
        return container

    def set_controls_enabled(self, enabled: bool) -> None:
        for control in self.controls:
            control.setEnabled(enabled)

    def _on_type_changed(self, value: str) -> None:
        self.run_action("Trigger type", lambda: self.controller.set_trigger_type(value))

    def _on_mode_changed(self, value: str) -> None:
        self.run_action("Trigger mode", lambda: self.controller.set_trigger_mode(value))

    def _on_edge_source_changed(self, value: str) -> None:
        self.run_action(
            "Edge trigger source",
            lambda: self.controller.set_trigger_edge_source(value),
        )

    def _on_edge_slope_changed(self, value: str) -> None:
        self.run_action(
            "Edge trigger slope",
            lambda: self.controller.set_trigger_edge_slope(value),
        )

    def _on_edge_coupling_changed(self, value: str) -> None:
        self.run_action(
            "Edge trigger coupling",
            lambda: self.controller.set_trigger_edge_coupling(value),
        )

    def _on_level_changed(self, value: float) -> None:
        channel = int(self.level_channel_combo.currentText().replace("CH", ""))
        self.run_action(
            "Trigger level",
            lambda: self.controller.set_trigger_level(channel, value),
        )

    def _on_set_level_50(self) -> None:
        self.run_action(
            "Trigger level 50%",
            self.controller.set_trigger_level_50_percent,
        )

    def _on_holdoff_changed(self, value: float) -> None:
        self.run_action(
            "Trigger holdoff",
            lambda: self.controller.set_trigger_holdoff(value),
        )

    def _on_force_trigger(self) -> None:
        self.run_action("Force trigger", self.controller.force_trigger)


class HorizontalPanel(QWidget):
    """Controls for the horizontal/timebase subsystem."""

    def __init__(
            self,
            controller: ScpiInstrumentController,
            run_action: Callable[[str, Callable[[], str]], None],
    ) -> None:
        super().__init__()
        self.controller = controller
        self.run_action = run_action
        self.controls: List[QWidget] = []

        form = QFormLayout()

        # Timebase scale is a 1-2-5 "friendly step" setting on real
        # oscilloscopes (same reasoning as the per-channel Scale combo) --
        # see HORIZONTAL_SCALE_VALUES for the caveat that its exact
        # range has not been independently verified against this
        # instrument's real sample-rate limits.
        self.scale_combo = QComboBox()
        _populate_scale_combo(self.scale_combo, HORIZONTAL_SCALE_VALUES, "s")
        self.scale_combo.setCurrentText(_format_engineering(1e-6, "s"))
        self.scale_combo.currentIndexChanged.connect(self._on_scale_changed)
        form.addRow("Timebase scale:", self.scale_combo)
        self.controls.append(self.scale_combo)

        self.delay_spin = QDoubleSpinBox()
        self.delay_spin.setRange(-100.0, 100.0)
        self.delay_spin.setDecimals(9)
        self.delay_spin.setSingleStep(1e-6)
        self.delay_spin.setSuffix(" s")
        self.delay_spin.setKeyboardTracking(False)
        self.delay_spin.setToolTip(
            "Arrows/mouse wheel step by 1 us. For a very different "
            "magnitude, just type the exact value and press Enter."
        )
        self.delay_spin.valueChanged.connect(self._on_delay_changed)
        form.addRow("Trigger position delay:", self.delay_spin)
        self.controls.append(self.delay_spin)

        self.reference_combo = QComboBox()
        self.reference_combo.addItems(["CENTer", "TRIG"])
        self.reference_combo.currentTextChanged.connect(self._on_reference_changed)
        form.addRow("Horizontal reference:", self.reference_combo)
        self.controls.append(self.reference_combo)

        self.record_length_combo = QComboBox()
        for length in (1000, 10000, 100000, 1000000, 10000000):
            self.record_length_combo.addItem(f"{length:,}", length)
        self.record_length_combo.currentIndexChanged.connect(self._on_record_length_changed)
        form.addRow("Record length:", self.record_length_combo)
        self.controls.append(self.record_length_combo)

        self.setLayout(form)

    @staticmethod
    def _paired(widget: QWidget, button: QPushButton) -> QWidget:
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(widget, 1)
        row.addWidget(button)
        return container

    def set_controls_enabled(self, enabled: bool) -> None:
        for control in self.controls:
            control.setEnabled(enabled)

    def _on_scale_changed(self) -> None:
        value = self.scale_combo.currentData()
        self.run_action("Horizontal scale", lambda: self.controller.set_horizontal_scale(value))

    def _on_delay_changed(self, value: float) -> None:
        self.run_action("Horizontal delay", lambda: self.controller.set_horizontal_delay(value))

    def _on_reference_changed(self, value: str) -> None:
        self.run_action(
            "Horizontal reference mode",
            lambda: self.controller.set_horizontal_reference_mode(value),
        )

    def _on_record_length_changed(self) -> None:
        length = self.record_length_combo.currentData()
        self.run_action(
            "Record length",
            lambda: self.controller.set_horizontal_record_length(length),
        )


class SaveNativePanel(QWidget):
    """Controls for the scope's own Save Command Subsystem (2.22) -- the
    same mechanism as the front-panel Copy button. Unlike the CH<x> tabs'
    "Waveform CSV" buttons (which go through :CURVe?/:WAVFrm? and are
    capped at screen resolution, ~900 points, confirmed empirically),
    this can export the full configured record with a time column, for
    one or all channels together, to external (USB) or internal storage.
    """

    def __init__(
            self,
            controller: ScpiInstrumentController,
            run_action: Callable[[str, Callable[[], str]], None],
    ) -> None:
        super().__init__()
        self.controller = controller
        self.run_action = run_action
        self.controls: List[QWidget] = []

        form = QFormLayout()

        note = QLabel(
            "Triggers the scope's own Save function (same as the "
            "front-panel Copy button). This is not limited to screen "
            "resolution the way the per-channel \"Waveform CSV\" buttons "
            "are, and can export the full acquired record."
        )
        note.setWordWrap(True)
        form.addRow(note)

        self.format_combo = QComboBox()
        self.format_combo.addItems(["CSV", "ZIP", "MATlab"])
        form.addRow("File format:", self.format_combo)
        self.controls.append(self.format_combo)

        self.source_combo = QComboBox()
        self.source_combo.addItem("All channels", "ALL")
        for channel in range(1, CHANNEL_COUNT + 1):
            self.source_combo.addItem(f"CH{channel} only", f"CH{channel}")
        form.addRow("Source:", self.source_combo)
        self.controls.append(self.source_combo)

        self.path_combo = QComboBox()
        self.path_combo.addItem("External (USB drive)", "EXTERNal")
        self.path_combo.addItem("Internal storage", "INTERNal")
        form.addRow("Save path:", self.path_combo)
        self.controls.append(self.path_combo)

        self.filename_field = QLineEdit("waveform_export")
        form.addRow("File name:", self.filename_field)
        self.controls.append(self.filename_field)

        save_button = QPushButton("Save Waveform (Native, Full Record)")
        save_button.clicked.connect(self._on_save)
        form.addRow(save_button)
        self.controls.append(save_button)

        self.setLayout(form)

    def set_controls_enabled(self, enabled: bool) -> None:
        for control in self.controls:
            control.setEnabled(enabled)

    def _on_save(self) -> None:
        filename = self.filename_field.text().strip()
        if not filename:
            QMessageBox.warning(self, "Missing Name", "Enter a file name first.")
            return
        file_format = self.format_combo.currentText()
        source = self.source_combo.currentData()
        path = self.path_combo.currentData()
        self.run_action(
            "Save Waveform (Native)",
            lambda: self.controller.save_waveform_native(
                file_format, path, source, filename
            ),
        )


class FftPlotWindow(QWidget):
    """A separate, resizable window showing an interactive multi-channel
    plot -- either the FFT (frequency domain) or the raw scope-style
    trace (voltage vs time) of every channel found in the loaded CSV.

    Uses matplotlib's own Qt navigation toolbar (embedded via
    FigureCanvasQTAgg/NavigationToolbar2QT) for pan/zoom -- the magnifying
    glass tool draws a rectangle to zoom into any frequency (or time)
    range, and the pan/home/back-forward buttons work the normal
    matplotlib way. A "View" selector switches every subplot at once
    between the FFT and the time-domain trace; a "Log scale (dB)"
    checkbox (FFT view only) switches the magnitude axis between linear
    and dB, since a small peak sitting right next to a big one is often
    invisible on a linear scale.

    This window does not talk to the instrument at all -- it is handed
    ready-made (label, times, voltages) tuples, one per channel, and
    computes/redraws from them, so it works equally well on a
    single-channel CSV or on a native-Save export containing all 4
    channels together.
    """

    def __init__(
            self,
            channels: List[Tuple[str, List[float], List[float]]],
            source_description: str,
    ) -> None:
        super().__init__()
        self.channels = channels
        self._fft_cache: List[Optional[Tuple[List[float], List[float]]]] = [
            None for _ in channels
        ]

        self.setWindowTitle(f"Waveform -- {source_description}")
        channel_count = len(channels)
        width = 950 if channel_count <= 1 else 1150
        height = 680 if channel_count <= 1 else 800
        self.resize(width, height)

        from matplotlib.backends.backend_qt5agg import (
            FigureCanvasQTAgg,
            NavigationToolbar2QT,
        )
        from matplotlib.figure import Figure

        self.figure = Figure(figsize=(9, 5))
        rows, columns = self._grid_shape(channel_count)
        self.axes_list = [
            self.figure.add_subplot(rows, columns, index + 1)
            for index in range(channel_count)
        ]
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.toolbar = NavigationToolbar2QT(self.canvas, self)

        self.view_combo = QComboBox()
        self.view_combo.addItem("FFT (Frequency Domain)", "fft")
        self.view_combo.addItem("Time Domain (Voltage vs Time)", "time")
        self.view_combo.currentIndexChanged.connect(self._redraw)

        self.log_checkbox = QCheckBox(
            "Log scale (dB) -- shows low amplitudes, FFT view only"
        )
        self.log_checkbox.stateChanged.connect(self._redraw)

        controls_bar = QHBoxLayout()
        controls_bar.addWidget(QLabel("View:"))
        controls_bar.addWidget(self.view_combo)
        controls_bar.addWidget(self.log_checkbox)
        controls_bar.addStretch(1)

        self.description_label = QLabel(source_description)
        self.description_label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(self.description_label)
        layout.addLayout(controls_bar)
        layout.addWidget(self.toolbar)
        layout.addWidget(self.canvas, 1)

        self._source_description = source_description
        self._redraw()

    @staticmethod
    def _grid_shape(channel_count: int) -> Tuple[int, int]:
        """Subplot grid size: 1x1 for a single channel, 2x1 for two, and
        2x2 for three or four -- matching how the native Save subsystem's
        CSV usually arrives (Time + up to 4 channel columns)."""
        if channel_count <= 1:
            return 1, 1
        if channel_count == 2:
            return 2, 1
        return 2, 2

    def _channel_fft(self, index: int) -> Tuple[List[float], List[float]]:
        cached = self._fft_cache[index]
        if cached is not None:
            return cached
        _, times, voltages = self.channels[index]
        frequencies, magnitude, _ = _compute_fft(times, voltages)
        self._fft_cache[index] = (frequencies, magnitude)
        return frequencies, magnitude

    def _redraw(self) -> None:
        view_mode = self.view_combo.currentData()
        show_log = self.log_checkbox.isChecked()
        self.log_checkbox.setEnabled(view_mode == "fft")

        for index, axes in enumerate(self.axes_list):
            label, times, voltages = self.channels[index]
            axes.clear()

            if view_mode == "fft":
                try:
                    frequencies, magnitude = self._channel_fft(index)
                except Exception as error:
                    axes.text(
                        0.5, 0.5, str(error),
                        ha="center", va="center", wrap=True,
                        transform=axes.transAxes,
                    )
                    axes.set_title(label)
                    continue

                if show_log:
                    import numpy as np

                    magnitude_db = 20 * np.log10(np.maximum(magnitude, 1e-12))
                    axes.plot(frequencies, magnitude_db)
                    axes.set_ylabel("Magnitude (dB)")
                else:
                    axes.plot(frequencies, magnitude)
                    axes.set_ylabel("Magnitude (V)")
                axes.set_xlabel("Frequency (Hz)")
            else:
                axes.plot(times, voltages)
                axes.set_xlabel("Time (s)")
                axes.set_ylabel("Voltage (V)")

            axes.set_title(label)
            axes.grid(True, alpha=0.3)

        self.figure.tight_layout()
        self.canvas.draw()


class InstrumentControlWindow(QWidget):
    """Main control window: a quick-access top bar, per-topic tabs, and a
    shared output box that shows the result of the most recent action.
    """

    def __init__(
            self,
            controller: ScpiInstrumentController,
            resource_address: str,
    ) -> None:
        super().__init__()
        self.controller = controller
        self.resource_address = resource_address
        self.all_control_groups: List[
            Union[ChannelPanel, TriggerPanel, HorizontalPanel, SaveNativePanel]
        ] = []
        self.top_bar_buttons: List[QPushButton] = []

        self.setWindowTitle("ADS824A Toolkit - Instrument Control")
        # Tall enough that all 4 channel panels (2x2 grid) are visible on
        # startup without the user having to stretch the window down --
        # see the stretch factors and output_box height cap below, which
        # are the other half of this fix. 1180 was measured against the
        # actual rendered channel grid height (4 panels, 2x2, plus grid
        # spacing) so it fits with no scrolling needed on a normal
        # desktop monitor; the QScrollArea around the grid is still there
        # as a fallback for smaller screens.
        self.resize(1360, 1180)

        self.connection_label = QLabel(f"Connected: {resource_address}")

        # Quick-access top bar: the handful of actions used constantly while
        # operating the scope, kept out of the tabs so they are always
        # reachable without switching tabs first.
        top_bar = QHBoxLayout()
        top_bar_specs = [
            ("Reconnect", self._on_reconnect),
            ("Query *IDN?", self._on_query_idn),
            ("Autoset", self._on_autoset),
            ("Run", self._on_run),
            ("Stop", self._on_stop),
            ("Single", self._on_single),
            ("Force Trigger", self._on_force_trigger),
            ("Save Screen PNG", self._on_save_screen_png),
            ("FFT from CSV...", self._on_fft_from_csv),
        ]
        for label, handler in top_bar_specs:
            button = QPushButton(label)
            button.clicked.connect(handler)
            top_bar.addWidget(button)
            self.top_bar_buttons.append(button)

        self.tabs = QTabWidget()

        # All 4 channels are shown together in a 2x2 grid on one tab,
        # instead of one tab per channel, so comparing/adjusting channels
        # doesn't require switching tabs back and forth. The grid sits in
        # a scroll area so it still works on smaller screens.
        channels_tab = QWidget()
        channels_layout = QGridLayout(channels_tab)
        for index, channel in enumerate(range(1, CHANNEL_COUNT + 1)):
            panel = ChannelPanel(channel, controller, self._run_action)
            row, column = divmod(index, 2)
            channels_layout.addWidget(panel, row, column)
            self.all_control_groups.append(panel)

        channels_scroll = QScrollArea()
        channels_scroll.setWidgetResizable(True)
        channels_scroll.setWidget(channels_tab)
        self.tabs.addTab(channels_scroll, "Channels")

        trigger_panel = TriggerPanel(controller, self._run_action)
        self.tabs.addTab(trigger_panel, "Trigger")
        self.all_control_groups.append(trigger_panel)

        horizontal_panel = HorizontalPanel(controller, self._run_action)
        self.tabs.addTab(horizontal_panel, "Horizontal")
        self.all_control_groups.append(horizontal_panel)

        save_native_panel = SaveNativePanel(controller, self._run_action)
        self.tabs.addTab(save_native_panel, "Save (Native)")
        self.all_control_groups.append(save_native_panel)

        self.output_box = QPlainTextEdit()
        self.output_box.setReadOnly(True)
        self.output_box.setPlaceholderText("Command results will appear here.")
        self.output_box.setMaximumBlockCount(500)
        # Kept compact and given no stretch share so the tabs above (in
        # particular the 2x2 channel grid) get essentially all of the
        # window's vertical space -- previously this had an equal stretch
        # factor to self.tabs, which forced a 50/50 split and hid 2 of
        # the 4 channel panels until the window was manually resized.
        self.output_box.setMaximumHeight(160)

        layout = QVBoxLayout(self)
        layout.addWidget(self.connection_label)
        layout.addLayout(top_bar)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(QLabel("Output"))
        layout.addWidget(self.output_box)

    # ------------------------------------------------------------------
    # Top bar handlers
    # ------------------------------------------------------------------

    def _on_reconnect(self) -> None:
        self._run_action(
            "Reconnect",
            lambda: self.controller.reconnect(self.resource_address),
        )

    def _on_query_idn(self) -> None:
        self._run_action("Query *IDN?", self.controller.query_idn)

    def _on_autoset(self) -> None:
        self._run_action("Autoset", self.controller.autoset)

    def _on_run(self) -> None:
        self._run_action("Run", self.controller.run_acquisition)

    def _on_stop(self) -> None:
        self._run_action("Stop", self.controller.stop_acquisition)

    def _on_single(self) -> None:
        self._run_action("Single", self.controller.single_acquisition)

    def _on_force_trigger(self) -> None:
        self._run_action("Force Trigger", self.controller.force_trigger)

    def _on_save_screen_png(self) -> None:
        self._run_action("Save Screen PNG", self.controller.save_screen_image_png)

    def _on_fft_from_csv(self) -> None:
        """Open a CSV and show it in an interactive FftPlotWindow, with a
        View selector to switch between the FFT and a scope-style
        time-domain trace. This never touches the instrument -- it works
        even while a live action is unavailable, and on any CSV shaped
        like Time + one-or-more voltage columns, not just ones this demo
        produced. In particular this is what reads the native Save
        subsystem's CSV (Time + up to 4 channel columns together),
        showing all 4 channels at once as separate subplots.
        """

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose a waveform CSV",
            str(self.controller.output_directory),
            "CSV files (*.csv);;All files (*.*)",
        )
        if not file_path:
            return

        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            channels = _load_waveform_csv(Path(file_path))
        except Exception as error:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Load Failed", self._format_error(error))
            return
        QApplication.restoreOverrideCursor()

        point_count = len(channels[0][1])
        description = (
            f"{Path(file_path).name} -- {len(channels)} channel(s), "
            f"{point_count} points"
        )
        # Keep a reference so the window isn't garbage-collected the
        # instant this method returns; each call replaces the previous
        # one, which is fine since this button opens one plot at a time.
        self.fft_window = FftPlotWindow(channels, description)
        self.fft_window.show()

    # ------------------------------------------------------------------
    # Shared action runner
    # ------------------------------------------------------------------

    def _run_action(
            self,
            title: str,
            action: Callable[[], str],
    ) -> None:
        """Execute a SCPI action and append its result to the output box.

        Every control across the top bar and every tab funnels through here,
        so results are reported consistently and the whole window is briefly
        disabled while a command is in flight (important for the LAN link,
        where a slow or stalled exchange would otherwise let the user fire
        off overlapping commands on the same connection).
        """

        self._set_all_controls_enabled(False)
        QApplication.setOverrideCursor(Qt.WaitCursor)

        try:
            result = action()
        except Exception as error:
            self.output_box.appendPlainText(f"[{title}] {self._format_error(error)}")
        else:
            self.output_box.appendPlainText(f"[{title}] {result}")
        finally:
            QApplication.restoreOverrideCursor()
            self._set_all_controls_enabled(True)

    def _set_all_controls_enabled(self, enabled: bool) -> None:
        for button in self.top_bar_buttons:
            button.setEnabled(enabled)
        for group in self.all_control_groups:
            group.set_controls_enabled(enabled)

    @staticmethod
    def _format_error(error: Exception) -> str:
        """Return a short error string that is useful to end users."""

        return f"{type(error).__name__}: {error}"

    def closeEvent(self, event) -> None:
        """Close the VISA session when the control window is closed."""

        self.controller.close()
        event.accept()


def main() -> int:
    """Create the Qt application and show the instrument selection window."""

    app = QApplication(sys.argv)
    controller = ScpiInstrumentController()
    app.aboutToQuit.connect(controller.close)

    selection_window = InstrumentSelectionWindow(controller)
    selection_window.show()

    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
