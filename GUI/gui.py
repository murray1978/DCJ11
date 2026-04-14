"""DCJ11 / PDP-11 ODT monitor GUI.

This module provides a functional PySide6 desktop tool with a backend interface
for a serial ODT transport.
"""

from __future__ import annotations

import re
import string
import sys
import time
from pathlib import Path
from abc import ABC, abstractmethod
from contextlib import contextmanager, nullcontext
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QFontDatabase, QIcon
from PySide6.QtWidgets import (
	QApplication,
	QComboBox,
	QDockWidget,
	QFileDialog,
	QFormLayout,
	QHBoxLayout,
	QInputDialog,
	QLabel,
	QLineEdit,
	QMainWindow,
	QMessageBox,
	QPlainTextEdit,
	QProgressDialog,
	QPushButton,
	QStatusBar,
	QTableWidget,
	QTableWidgetItem,
	QVBoxLayout,
	QWidget,
)


WORD_MASK = 0xFFFF
ADDRESS_MASK = 0xFFFF
MAX_MEMORY_WORDS = (ADDRESS_MASK + 1) // 2
DEFAULT_ODT_PORT = "/dev/ttyUSB0"
DEFAULT_ODT_BAUDRATE = 9600
DEFAULT_BASE_ADDRESS = 0o001000

REGISTER_ORDER = ["R0", "R1", "R2", "R3", "R4", "R5", "SP", "PC", "PSW"]


try:
	import serial  # type: ignore
	from serial.tools import list_ports  # type: ignore
except ImportError:
	serial = None
	list_ports = None


def format_octal(value: int, width: int = 6) -> str:
	"""Format value as zero-padded octal, PDP-11 style."""
	return f"{value & WORD_MASK:0{width}o}"


def format_hex(value: int, width: int = 4) -> str:
	"""Format value as zero-padded hexadecimal."""
	return f"{value & WORD_MASK:0{width}X}"


def parse_octal_input(text: str) -> int:
	"""Parse an octal number. Accepts optional 0o prefix."""
	cleaned = text.strip().lower()
	if cleaned.startswith("0o"):
		cleaned = cleaned[2:]
	if not cleaned:
		raise ValueError("Empty octal value")
	return int(cleaned, 8)


def parse_hex_input(text: str) -> int:
	"""Parse a hexadecimal number. Accepts optional 0x prefix."""
	cleaned = text.strip().lower()
	if cleaned.startswith("0x"):
		cleaned = cleaned[2:]
	if not cleaned:
		raise ValueError("Empty hexadecimal value")
	return int(cleaned, 16)


def parse_numeric_input(text: str, default_base: str) -> int:
	"""Parse user numeric input with explicit prefixes or a display-mode default."""
	stripped = text.strip().lower()
	if stripped.startswith("0x"):
		return parse_hex_input(stripped)
	if stripped.startswith("0o"):
		return parse_octal_input(stripped)
	if default_base == "hex":
		return parse_hex_input(stripped)
	return parse_octal_input(stripped)


def to_ascii_from_word(word: int) -> str:
	"""Render a 16-bit word as two ASCII bytes when printable."""
	high = (word >> 8) & 0xFF
	low = word & 0xFF
	chars = []
	for byte in (high, low):
		ch = chr(byte)
		chars.append(ch if ch in string.printable and ch not in "\r\n\t\x0b\x0c" else ".")
	return "".join(chars)


_PDP11_ADDRESS_INFO: dict[int, str] = {
	# Reset vectors
	0o000000: "Reset: initial SP",
	0o000002: "Reset: initial PC",
	# Trap vectors (PC / PS pairs)
	0o000004: "Trap vec: bus error PC",
	0o000006: "Trap vec: bus error PS",
	0o000010: "Trap vec: illegal instr PC",
	0o000012: "Trap vec: illegal instr PS",
	0o000014: "Trap vec: BPT PC",
	0o000016: "Trap vec: BPT PS",
	0o000020: "Trap vec: IOT PC",
	0o000022: "Trap vec: IOT PS",
	0o000024: "Trap vec: power fail PC",
	0o000026: "Trap vec: power fail PS",
	0o000030: "Trap vec: EMT PC",
	0o000032: "Trap vec: EMT PS",
	0o000034: "Trap vec: TRAP/T-bit PC",
	0o000036: "Trap vec: TRAP/T-bit PS",
	# Device interrupt vectors
	0o000060: "Int vec: console RX (DL11) PC",
	0o000062: "Int vec: console RX (DL11) PS",
	0o000064: "Int vec: console TX (DL11) PC",
	0o000066: "Int vec: console TX (DL11) PS",
	0o000100: "Int vec: KW11-L line clock PC",
	0o000102: "Int vec: KW11-L line clock PS",
	# I/O page registers (16-bit addresses)
	0o177546: "KW11-L clock status (LKS)",
	0o177560: "DL11 console RCSR (RX status)",
	0o177562: "DL11 console RBUF (RX buffer)",
	0o177564: "DL11 console XCSR (TX status)",
	0o177566: "DL11 console XBUF (TX buffer)",
	0o177170: "RX11/RX01 floppy RXCS",
	0o177172: "RX11/RX01 floppy RXDB",
	0o177400: "RK11/RK05 disk RKDS",
	0o177402: "RK11/RK05 disk RKER",
	0o177404: "RK11/RK05 disk RKCS",
	0o177406: "RK11/RK05 disk RKWC",
	0o177410: "RK11/RK05 disk RKBA",
	0o177412: "RK11/RK05 disk RKDA",
	0o177414: "RK11/RK05 disk RKDB",
	0o177570: "Switch register (SR)",
	0o177572: "Display register / MMR0",
	0o177574: "CPU error / MMR1",
	0o177576: "Stack limit / MMR2",
	0o177740: "MMR3",
	0o177746: "Cache control register (CCR)",
	0o177752: "CPU error register (CPUERR)",
	0o177754: "Microbreak register",
	0o177756: "Stack limit register (SL)",
	0o177760: "PIR (program interrupt request)",
	0o177764: "Memory system error (MSER)",
	0o177776: "PSW (processor status word)",
}

_PDP11_ADDRESS_RANGES: list[tuple[int, int, str]] = [
	(0o172440, 0o172456, "TC11/TU56 DECtape controller"),
	(0o172520, 0o172532, "TM11 magtape controller"),
	(0o174400, 0o174406, "RL11 RL01/RL02 disk controller"),
]


def to_info_from_address(address: int) -> str:
	"""Return a descriptive label for well-known PDP-11 addresses."""
	masked_address = address & ADDRESS_MASK
	info = _PDP11_ADDRESS_INFO.get(masked_address)
	if info is not None:
		return info
	for start, end, label in _PDP11_ADDRESS_RANGES:
		if start <= masked_address <= end:
			offset = masked_address - start
			return f"{label} +{offset:o}"
	return ""


def decode_psw(value: int) -> str:
	"""Decode PDP-11 PSW mode, priority, and condition flags."""
	mode_names = {
		0: "Kernel",
		1: "Supervisor",
		2: "Unused",
		3: "User",
	}
	current_mode = mode_names.get((value >> 14) & 0x3, "?")
	previous_mode = mode_names.get((value >> 12) & 0x3, "?")
	priority = (value >> 5) & 0x7
	trace = "T=1" if value & 0x10 else "T=0"
	flags = "".join(
		flag if value & bit else "-"
		for flag, bit in (("N", 0x8), ("Z", 0x4), ("V", 0x2), ("C", 0x1))
	)
	return f"CM={current_mode} PM={previous_mode} IPL={priority} {trace} {flags}"


class BackendInterface(ABC):
	"""Backend adapter interface for monitor operations.

	A future serial ODT implementation should subclass this interface and map
	these methods to device transport commands.
	"""

	@abstractmethod
	def read_registers(self) -> dict[str, int]:
		raise NotImplementedError

	@abstractmethod
	def write_register(self, name: str, value: int) -> None:
		raise NotImplementedError

	@abstractmethod
	def read_memory(self, start: int, count: int) -> list[tuple[int, int]]:
		raise NotImplementedError

	@abstractmethod
	def write_memory(self, address: int, value: int) -> None:
		raise NotImplementedError

	@abstractmethod
	def read_io(self, address: int) -> int:
		raise NotImplementedError

	@abstractmethod
	def write_io(self, address: int, value: int) -> None:
		raise NotImplementedError

	@abstractmethod
	def set_breakpoint(self, address: int) -> None:
		raise NotImplementedError

	@abstractmethod
	def clear_breakpoint(self, address: int) -> None:
		raise NotImplementedError

	@abstractmethod
	def go(self, address: int | None = None) -> None:
		raise NotImplementedError

	@abstractmethod
	def proceed(self) -> None:
		raise NotImplementedError


class SerialODTBackend(BackendInterface):
	"""ODT backend over a serial line.

	The command syntax here follows common PDP-11 ODT monitor conventions.
	If a response is not parseable, the backend keeps the last cached value.
	"""

	def __init__(
		self,
		port: str = DEFAULT_ODT_PORT,
		baudrate: int = DEFAULT_ODT_BAUDRATE,
		timeout: float = 0.4,
		auto_connect: bool = False,
	) -> None:
		if serial is None:
			raise RuntimeError(
				"pyserial is required for serial ODT access. Install with: pip install pyserial"
			)

		self.port = port
		self.baudrate = baudrate
		self.timeout = timeout
		self.connection = None

		# Caches keep UI usable even when ODT returns terse/unparseable responses.
		self.registers: dict[str, int] = {reg: 0 for reg in REGISTER_ORDER}
		self.memory_cache: dict[int, int] = {}
		self.io_observer = None

		if auto_connect:
			self.connect()

	def connect(self) -> None:
		if self.connection is not None and self.connection.is_open:
			return

		self.connection = serial.Serial(
			port=self.port,
			baudrate=self.baudrate,
			bytesize=serial.EIGHTBITS,
			parity=serial.PARITY_NONE,
			stopbits=serial.STOPBITS_ONE,
			timeout=self.timeout,
			write_timeout=self.timeout,
		)

		# Give ODT a brief settle window, then clear any startup text.
		time.sleep(0.1)
		self._flush_buffers()

	def disconnect(self) -> None:
		if self.connection is not None and self.connection.is_open:
			self.connection.close()

	def is_connected(self) -> bool:
		return bool(self.connection is not None and self.connection.is_open)

	def set_port(self, port: str) -> None:
		if self.is_connected():
			self.disconnect()
		self.port = port

	def available_ports(self) -> list[str]:
		if list_ports is None:
			return []
		return [entry.device for entry in list_ports.comports()]

	def set_io_observer(self, observer) -> None:
		self.io_observer = observer

	def _flush_buffers(self) -> None:
		if self.connection is None:
			return
		self.connection.reset_input_buffer()
		self.connection.reset_output_buffer()

	def _read_serial_response(self, timeout: float, stop_on_prompt: bool = True) -> str:
		if self.connection is None or not self.connection.is_open:
			return ""

		chunks: list[bytes] = []
		deadline = time.monotonic() + timeout
		while time.monotonic() < deadline:
			chunk = self.connection.read(256)
			if chunk:
				chunks.append(chunk)
				if stop_on_prompt and b"@" in chunk:
					break
			else:
				# Continue until deadline; some ODT replies arrive in bursts.
				continue

		return b"".join(chunks).decode("ascii", errors="ignore")

	def _send_command(self, command: str) -> str:
		if self.connection is None or not self.connection.is_open:
			raise RuntimeError(f"Serial port is closed: {self.port}")

		wire = f"{command}\r".encode("ascii", errors="ignore")
		self.connection.write(wire)
		self.connection.flush()
		response = self._read_serial_response(timeout=max(self.timeout, 0.6), stop_on_prompt=True)
		if self.io_observer is not None:
			self.io_observer(command, response)
		return response

	def _parse_word_from_response(self, response: str) -> int | None:
		# Prefer explicit octal-like groups commonly printed by ODT.
		matches = re.findall(r"(?<![0-9A-Fa-f])([0-7]{1,6})(?![0-9A-Fa-f])", response)
		if matches:
			return int(matches[-1], 8) & WORD_MASK

		# Fallback: parse the last hex token if present.
		hex_matches = re.findall(r"(?<![0-9A-Fa-f])([0-9A-Fa-f]{1,4})(?![0-9A-Fa-f])", response)
		if hex_matches:
			return int(hex_matches[-1], 16) & WORD_MASK
		return None

	def _response_has_error(self, response: str) -> bool:
		"""ODT error replies typically include '?' markers."""
		return "?" in response

	def _response_summary(self, response: str, limit: int = 80) -> str:
		cleaned = response.replace("\r", "\\r").replace("\n", "\\n").strip()
		if not cleaned:
			return "<empty>"
		if len(cleaned) > limit:
			return cleaned[:limit] + "..."
		return cleaned

	def _deposit(self, selector: str, value: int, label: str) -> None:
		"""Perform interactive ODT deposit: selector/ then value<CR>."""
		if self.connection is None or not self.connection.is_open:
			raise RuntimeError(f"Serial port is closed: {self.port}")

		selector_wire = f"{selector}/".encode("ascii", errors="ignore")
		self.connection.write(selector_wire)
		self.connection.flush()

		phase1 = self._read_serial_response(timeout=max(self.timeout, 0.5), stop_on_prompt=False)

		value_wire = f"{value & WORD_MASK:o}\r".encode("ascii", errors="ignore")
		self.connection.write(value_wire)
		self.connection.flush()

		phase2 = self._read_serial_response(timeout=max(self.timeout, 0.8), stop_on_prompt=True)
		response = phase1 + phase2
		if self.io_observer is not None:
			self.io_observer(f"{selector}/<deposit>{value & WORD_MASK:o}", response)

		if self._response_has_error(response):
			raise RuntimeError(
				f"ODT rejected write for {label}: {self._response_summary(response)}"
			)

	def _register_token(self, name: str) -> str | None:
		"""Map UI register names to safe ODT register selectors."""
		mapping = {
			"R0": "r0",
			"R1": "r1",
			"R2": "r2",
			"R3": "r3",
			"R4": "r4",
			"R5": "r5",
			"SP": "r6",
			"PC": "r7",
			"PSW": "rs",
		}
		return mapping.get(name)

	def read_registers(self) -> dict[str, int]:
		for reg in REGISTER_ORDER:
			token = self._register_token(reg)
			if token is None:
				continue
			response = self._send_command(f"{token}/")
			parsed = self._parse_word_from_response(response)
			if parsed is not None:
				self.registers[reg] = parsed
		return dict(self.registers)

	def write_register(self, name: str, value: int) -> None:
		if name not in self.registers:
			raise KeyError(f"Unknown register: {name}")
		token = self._register_token(name)
		if token is None:
			raise NotImplementedError(f"Register write not supported: {name}")
		self._deposit(token, value, f"register {name}")
		self.registers[name] = value & WORD_MASK

	def read_memory(self, start: int, count: int) -> list[tuple[int, int]]:
		base = start & ADDRESS_MASK
		results: list[tuple[int, int]] = []
		for i in range(count):
			address = (base + (i * 2)) & ADDRESS_MASK
			response = self._send_command(f"{address:o}/")
			parsed = self._parse_word_from_response(response)
			if parsed is None:
				parsed = self.memory_cache.get(address, 0)
			self.memory_cache[address] = parsed
			results.append((address, parsed))
		return results

	def write_memory(self, address: int, value: int) -> None:
		addr = address & ADDRESS_MASK
		val = value & WORD_MASK

		last_verify: int | None = None
		for _attempt in range(2):
			self._deposit(f"{addr:o}", val, f"memory @{addr:o}")
			verify_response = self._send_command(f"{addr:o}/")
			verified = self._parse_word_from_response(verify_response)
			last_verify = verified
			if verified == val:
				self.memory_cache[addr] = val
				return

		actual_text = format_octal(last_verify) if last_verify is not None else "<unknown>"
		raise RuntimeError(
			f"ODT memory write failed @{addr:o}: expected {format_octal(val)} got {actual_text}"
		)

	def read_io(self, address: int) -> int:
		response = self._send_command(f"{address & ADDRESS_MASK:o}/")
		parsed = self._parse_word_from_response(response)
		if parsed is None:
			return self.memory_cache.get(address & ADDRESS_MASK, 0)
		self.memory_cache[address & ADDRESS_MASK] = parsed
		return parsed

	def write_io(self, address: int, value: int) -> None:
		self.write_memory(address, value)

	def set_breakpoint(self, address: int) -> None:
		self._send_command(f"b {address & ADDRESS_MASK:o}")

	def clear_breakpoint(self, address: int) -> None:
		self._send_command(f"n {address & ADDRESS_MASK:o}")

	def go(self, address: int | None = None) -> None:
		if address is None:
			self._send_command("G")
			return
		self._send_command(f"{address & ADDRESS_MASK:o}G")

	def proceed(self) -> None:
		self._send_command("P")


class MonitorController:
	"""Thin logic layer between GUI and backend transport."""

	def __init__(self, backend: BackendInterface) -> None:
		self.backend = backend

	def read_registers(self) -> dict[str, int]:
		return self.backend.read_registers()

	def write_register(self, name: str, value: int) -> None:
		self.backend.write_register(name, value)

	def read_memory(self, start: int, count: int) -> list[tuple[int, int]]:
		return self.backend.read_memory(start, count)

	def write_memory(self, address: int, value: int) -> None:
		self.backend.write_memory(address, value)

	def read_io(self, address: int) -> int:
		return self.backend.read_io(address)

	def write_io(self, address: int, value: int) -> None:
		self.backend.write_io(address, value)

	def set_breakpoint(self, address: int) -> None:
		self.backend.set_breakpoint(address)

	def clear_breakpoint(self, address: int) -> None:
		self.backend.clear_breakpoint(address)

	def go(self, address: int | None = None) -> None:
		self.backend.go(address)

	def proceed(self) -> None:
		self.backend.proceed()

	def is_serial_connected(self) -> bool:
		if isinstance(self.backend, SerialODTBackend):
			return self.backend.is_connected()
		return True

	def serial_connect(self) -> None:
		if isinstance(self.backend, SerialODTBackend):
			self.backend.connect()

	def serial_disconnect(self) -> None:
		if isinstance(self.backend, SerialODTBackend):
			self.backend.disconnect()

	def get_serial_port(self) -> str:
		if isinstance(self.backend, SerialODTBackend):
			return self.backend.port
		return DEFAULT_ODT_PORT

	def set_serial_port(self, port: str) -> None:
		if isinstance(self.backend, SerialODTBackend):
			self.backend.set_port(port)

	def available_serial_ports(self) -> list[str]:
		if isinstance(self.backend, SerialODTBackend):
			return self.backend.available_ports()
		return []

	def set_serial_io_observer(self, observer) -> None:
		if isinstance(self.backend, SerialODTBackend):
			self.backend.set_io_observer(observer)


class MainWindow(QMainWindow):
	def __init__(self, controller: MonitorController) -> None:
		super().__init__()
		self.controller = controller
		self.display_mode = "octal"
		self.default_base_address = DEFAULT_BASE_ADDRESS
		self.memory_words: dict[int, int] = {}
		self.program_words: list[tuple[int, int]] = []
		self.breakpoints: set[int] = set()
		self.selected_serial_port = self.controller.get_serial_port()

		self.setWindowTitle(f"DCJ11 / PDP-11 ODT Monitor ({DEFAULT_ODT_PORT})")
		self.setWindowIcon(QIcon(str(Path(__file__).parent / "icon.png")))

		self.resize(1180, 740)
		self.fixed_font = QFontDatabase.systemFont(QFontDatabase.FixedFont)

		self._create_actions()
		self._create_menu_and_toolbar()
		self._create_status_bar()
		self._create_panels()
		self.controller.set_serial_io_observer(self._on_serial_io)

		if self.controller.is_serial_connected():
			self.refresh_all()
		else:
			self._log(f"Serial disconnected ({self.selected_serial_port})")
			self.statusBar().showMessage("Serial disconnected", 3000)
		self._update_connection_ui()

	def _create_actions(self) -> None:
		self.refresh_action = QAction("Refresh All", self)
		self.refresh_action.triggered.connect(self._on_refresh_all_triggered)

		self.exit_action = QAction("Exit", self)
		self.exit_action.triggered.connect(self.close)

		self.select_serial_action = QAction("Select Serial Device...", self)
		self.select_serial_action.triggered.connect(self.select_serial_device)

		self.toggle_connection_action = QAction("Connect", self)
		self.toggle_connection_action.triggered.connect(self.toggle_serial_connection)

		self.default_base_action = QAction("Set Default Base Address...", self)
		self.default_base_action.triggered.connect(self.set_default_base_address)

		self.show_registers_action = QAction("Special Registers", self)
		self.show_registers_action.triggered.connect(self.show_registers_dock)

		self.about_action = QAction("About", self)
		self.about_action.triggered.connect(self.show_about_dialog)

	def _create_menu_and_toolbar(self) -> None:
		file_menu = self.menuBar().addMenu("File")
		file_menu.addAction(self.refresh_action)
		file_menu.addSeparator()
		file_menu.addAction(self.exit_action)

		connection_menu = self.menuBar().addMenu("Connection")
		connection_menu.addAction(self.select_serial_action)
		connection_menu.addAction(self.toggle_connection_action)

		settings_menu = self.menuBar().addMenu("Settings")
		settings_menu.addAction(self.default_base_action)

		view_menu = self.menuBar().addMenu("View")
		view_menu.addAction(self.show_registers_action)

		help_menu = self.menuBar().addMenu("Help")
		help_menu.addAction(self.about_action)

		toolbar = self.addToolBar("Main")
		toolbar.addAction(self.refresh_action)
		toolbar.addAction(self.exit_action)
		toolbar.addSeparator()
		self.connect_button = QPushButton("Connect")
		self.connect_button.clicked.connect(self.toggle_serial_connection)
		toolbar.addWidget(self.connect_button)

		toolbar.addSeparator()
		toolbar.addWidget(QLabel("Display:"))
		self.display_mode_combo = QComboBox()
		self.display_mode_combo.addItems(["Octal", "Hex"])
		self.display_mode_combo.currentTextChanged.connect(self._on_display_mode_changed)
		toolbar.addWidget(self.display_mode_combo)

		toolbar.addSeparator()
		toolbar.addWidget(QLabel("PSW:"))
		self.psw_toolbar_label = QLabel("—")
		self.psw_toolbar_label.setFont(self.fixed_font)
		self.psw_toolbar_label.setMinimumWidth(260)
		toolbar.addWidget(self.psw_toolbar_label)

	def _update_connection_ui(self) -> None:
		connected = self.controller.is_serial_connected()
		self.connect_button.setText("Disconnect" if connected else "Connect")
		self.toggle_connection_action.setText("Disconnect" if connected else "Connect")
		state = "connected" if connected else "disconnected"
		self.setWindowTitle(f"DCJ11 / PDP-11 ODT ({self.selected_serial_port}, {state})")

	def _create_status_bar(self) -> None:
		self.setStatusBar(QStatusBar())
		self.statusBar().showMessage("Ready")

	def _fit_window_to_screen(self) -> None:
		screen = self.screen() or QApplication.primaryScreen()
		if screen is None:
			return
		available = screen.availableGeometry()
		width = min(self.width(), max(900, available.width() - 40))
		height = min(self.height(), max(620, available.height() - 40))
		self.resize(width, height)

	def _on_serial_io(self, tx: str, rx: str) -> None:
		tx_clean = tx.replace("\r", "\\r").replace("\n", "\\n").strip()
		rx_clean = rx.replace("\r", "\\r").replace("\n", "\\n").strip()
		if not tx_clean:
			tx_clean = "<empty>"
		if not rx_clean:
			rx_clean = "<no response>"
		if len(tx_clean) > 40:
			tx_clean = tx_clean[:40] + "..."
		if len(rx_clean) > 120:
			rx_clean = rx_clean[:120] + "..."
		self._log(f"Serial I/O | TX: {tx_clean} | RX: {rx_clean}")

	def _create_panels(self) -> None:
		self.memory_table = QTableWidget(MAX_MEMORY_WORDS, 4)
		self.memory_table.setHorizontalHeaderLabels(["Address", "Word", "ASCII", "INFO"])
		self.memory_table.setFont(self.fixed_font)
		self.memory_table.horizontalHeader().setStretchLastSection(True)
		self.memory_table.setAlternatingRowColors(True)
		self._initialize_memory_table()
		self.setCentralWidget(self.memory_table)

		self._create_register_dock()
		self._create_memory_controls_dock()
		self._create_program_dock()
		self._create_execution_dock()
		self._create_log_dock()
		self._arrange_docks()


	def _arrange_docks(self) -> None:
		# Left column: registers above execution control.
		self.splitDockWidget(self.register_dock, self.execution_dock, Qt.Vertical)

		# Right column: memory controls above program loader.
		self.splitDockWidget(self.memory_controls_dock, self.program_dock, Qt.Vertical)

		# Bias sizes so Program Loader remains visible and usable.
		self.resizeDocks(
			[self.memory_controls_dock, self.program_dock],
			[220, 460],
			Qt.Vertical,
		)
		self.resizeDocks(
			[self.register_dock, self.execution_dock],
			[280, 240],
			Qt.Vertical,
		)

	def _create_register_dock(self) -> None:
		self.register_dock = QDockWidget("Registers", self)
		self.register_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)

		wrapper = QWidget()
		layout = QVBoxLayout(wrapper)

		self.register_table = QTableWidget(len(REGISTER_ORDER), 3)
		self.register_table.setHorizontalHeaderLabels(["Register", "Value", "Decoded"])
		self.register_table.verticalHeader().setVisible(False)
		self.register_table.setFont(self.fixed_font)
		self.register_table.horizontalHeader().setStretchLastSection(True)

		for row, reg in enumerate(REGISTER_ORDER):
			name_item = QTableWidgetItem(reg)
			name_item.setFlags(name_item.flags() & ~Qt.ItemIsEditable)
			self.register_table.setItem(row, 0, name_item)
			self.register_table.setItem(row, 1, QTableWidgetItem(""))
			decoded_item = QTableWidgetItem("")
			decoded_item.setFlags(decoded_item.flags() & ~Qt.ItemIsEditable)
			self.register_table.setItem(row, 2, decoded_item)

		button_row = QHBoxLayout()
		refresh_btn = QPushButton("Refresh")
		refresh_btn.clicked.connect(self._on_refresh_registers_clicked)
		write_btn = QPushButton("Write Register")
		write_btn.clicked.connect(self.write_selected_or_all_registers)
		button_row.addWidget(refresh_btn)
		button_row.addWidget(write_btn)

		layout.addWidget(self.register_table)
		layout.addLayout(button_row)

		self.register_dock.setWidget(wrapper)
		self.addDockWidget(Qt.LeftDockWidgetArea, self.register_dock)

	def _create_memory_controls_dock(self) -> None:
		self.memory_controls_dock = QDockWidget("Memory Controls", self)
		self.memory_controls_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)

		wrapper = QWidget()
		outer = QVBoxLayout(wrapper)

		read_group = QWidget()
		read_layout = QFormLayout(read_group)
		self.mem_start_input = QLineEdit(format_octal(self.default_base_address))
		self.mem_length_input = QLineEdit("20")
		read_btn = QPushButton("Read Memory")
		read_btn.clicked.connect(self._on_read_memory_clicked)
		read_layout.addRow("Start Address", self.mem_start_input)
		read_layout.addRow("Length (words)", self.mem_length_input)
		read_layout.addRow(read_btn)

		write_group = QWidget()
		write_layout = QFormLayout(write_group)
		self.mem_write_address_input = QLineEdit("001000")
		self.mem_write_value_input = QLineEdit("000000")
		write_btn = QPushButton("Write Memory Word")
		write_btn.clicked.connect(self.write_memory_word)
		write_layout.addRow("Target Address", self.mem_write_address_input)
		write_layout.addRow("Value", self.mem_write_value_input)
		write_layout.addRow(write_btn)

		outer.addWidget(QLabel("Memory Read"))
		outer.addWidget(read_group)
		outer.addWidget(QLabel("Memory Write"))
		outer.addWidget(write_group)
		outer.addStretch(1)

		self.memory_controls_dock.setWidget(wrapper)
		self.addDockWidget(Qt.RightDockWidgetArea, self.memory_controls_dock)

	def _create_log_dock(self) -> None:
		self.log_dock = QDockWidget("Log / Console", self)
		self.log_dock.setAllowedAreas(Qt.BottomDockWidgetArea)

		self.log_output = QPlainTextEdit()
		self.log_output.setReadOnly(True)
		self.log_output.setFont(self.fixed_font)

		self.log_dock.setWidget(self.log_output)
		self.addDockWidget(Qt.BottomDockWidgetArea, self.log_dock)

	def _create_program_dock(self) -> None:
		self.program_dock = QDockWidget("Program Loader", self)
		self.program_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)

		wrapper = QWidget()
		layout = QVBoxLayout(wrapper)

		controls = QWidget()
		controls_layout = QFormLayout(controls)
		self.program_base_input = QLineEdit(format_octal(self.default_base_address))
		read_btn = QPushButton("Read Program File")
		read_btn.clicked.connect(self.read_program_file)
		load_btn = QPushButton("Load Program to Target")
		load_btn.clicked.connect(self.load_program_to_target)

		controls_layout.addRow("Default Base Address", self.program_base_input)
		controls_layout.addRow(read_btn)
		controls_layout.addRow(load_btn)

		self.program_table = QTableWidget(0, 4)
		self.program_table.setHorizontalHeaderLabels(["Address", "Word", "ASCII", "INFO"])
		self.program_table.setFont(self.fixed_font)
		self.program_table.horizontalHeader().setStretchLastSection(True)

		layout.addWidget(controls)
		layout.addWidget(self.program_table)

		self.program_dock.setWidget(wrapper)
		self.addDockWidget(Qt.RightDockWidgetArea, self.program_dock)

	def _create_execution_dock(self) -> None:
		self.execution_dock = QDockWidget("Execution Control", self)
		self.execution_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)

		wrapper = QWidget()
		layout = QVBoxLayout(wrapper)

		go_group = QWidget()
		go_layout = QFormLayout(go_group)
		self.go_address_input = QLineEdit(format_octal(self.default_base_address))
		go_btn = QPushButton("Go (g)")
		go_btn.clicked.connect(self.go_command)
		proceed_btn = QPushButton("Proceed (p)")
		proceed_btn.clicked.connect(self.proceed_command)
		go_layout.addRow("Go Address", self.go_address_input)
		go_layout.addRow(go_btn)
		go_layout.addRow(proceed_btn)

		bp_group = QWidget()
		bp_layout = QFormLayout(bp_group)
		self.breakpoint_input = QLineEdit("000100")
		bp_add_btn = QPushButton("Add Breakpoint")
		bp_add_btn.clicked.connect(self.add_breakpoint)
		bp_remove_btn = QPushButton("Remove Selected")
		bp_remove_btn.clicked.connect(self.remove_selected_breakpoints)
		bp_layout.addRow("Breakpoint Address", self.breakpoint_input)
		bp_layout.addRow(bp_add_btn)
		bp_layout.addRow(bp_remove_btn)

		self.breakpoint_table = QTableWidget(0, 1)
		self.breakpoint_table.setHorizontalHeaderLabels(["Breakpoints"])
		self.breakpoint_table.setFont(self.fixed_font)
		self.breakpoint_table.horizontalHeader().setStretchLastSection(True)

		layout.addWidget(go_group)
		layout.addWidget(bp_group)
		layout.addWidget(self.breakpoint_table)

		self.execution_dock.setWidget(wrapper)
		self.addDockWidget(Qt.LeftDockWidgetArea, self.execution_dock)

	def _on_display_mode_changed(self, mode_text: str) -> None:
		self.display_mode = "hex" if mode_text.lower() == "hex" else "octal"
		self._initialize_memory_table()
		self._refresh_memory_table_values()
		self._refresh_program_table()
		self._refresh_breakpoint_table()
		if self.controller.is_serial_connected():
			self.refresh_registers(show_progress=False)

	def show_registers_dock(self) -> None:
		self.register_dock.show()
		self.register_dock.raise_()
		self.register_dock.activateWindow()
		self._log("Registers panel opened from menu")

	def show_about_dialog(self) -> None:
		QMessageBox.about(
			self,
			"About DCJ11 / PDP-11 ODT Monitor",
			"DCJ11 / PDP-11 ODT Monitor\n\n"
			"PySide6 GUI for register, memory, program load, and serial ODT control.",
		)
		self._log("About dialog opened")

	def _on_refresh_all_triggered(self) -> None:
		self.refresh_all(show_progress=True)

	def _on_refresh_registers_clicked(self) -> None:
		self.refresh_registers(show_progress=True)

	def _on_read_memory_clicked(self) -> None:
		self.read_memory_range(show_progress=True)

	def _memory_row_for_address(self, address: int) -> int:
		return ((address & ADDRESS_MASK) >> 1) % MAX_MEMORY_WORDS

	def _ensure_memory_cell(self, row: int, col: int) -> QTableWidgetItem:
		item = self.memory_table.item(row, col)
		if item is None:
			item = QTableWidgetItem("")
			item.setFont(self.fixed_font)
			self.memory_table.setItem(row, col, item)
		return item

	def _set_memory_row(self, address: int, word: int | None = None) -> None:
		masked_address = address & ADDRESS_MASK
		row = self._memory_row_for_address(masked_address)
		address_item = self._ensure_memory_cell(row, 0)
		word_item = self._ensure_memory_cell(row, 1)
		ascii_item = self._ensure_memory_cell(row, 2)
		info_item = self._ensure_memory_cell(row, 3)

		address_item.setText(self._format_word(masked_address))
		if word is None:
			word_item.setText("")
			ascii_item.setText("")
		else:
			masked_word = word & WORD_MASK
			word_item.setText(self._format_word(masked_word))
			ascii_item.setText(to_ascii_from_word(masked_word))
		info_item.setText(to_info_from_address(masked_address))

	def _initialize_memory_table(self) -> None:
		self.memory_table.setUpdatesEnabled(False)
		try:
			for row in range(MAX_MEMORY_WORDS):
				self._set_memory_row(row * 2)
		finally:
			self.memory_table.setUpdatesEnabled(True)

	def _refresh_memory_table_values(self) -> None:
		for address, word in self.memory_words.items():
			self._set_memory_row(address, word)

	def _scroll_memory_to_address(self, address: int) -> None:
		row = self._memory_row_for_address(address)
		self.memory_table.scrollToItem(
			self._ensure_memory_cell(row, 0),
			QTableWidget.PositionAtCenter,
		)

	def _format_word(self, value: int) -> str:
		return format_hex(value) if self.display_mode == "hex" else format_octal(value)

	def _parse_input(self, text: str) -> int:
		return parse_numeric_input(text, self.display_mode)

	def _show_error(self, message: str) -> None:
		self.statusBar().showMessage(message, 4000)
		QMessageBox.warning(self, "Input Error", message)

	def _log(self, message: str) -> None:
		stamp = datetime.now().strftime("%H:%M:%S")
		self.log_output.appendPlainText(f"[{stamp}] {message}")

	def _ensure_connected(self, operation_name: str) -> bool:
		if self.controller.is_serial_connected():
			return True

		answer = QMessageBox.question(
			self,
			"Serial Disconnected",
			f"{operation_name} requires a serial connection. Connect now?",
			QMessageBox.Yes | QMessageBox.No,
			QMessageBox.Yes,
		)
		if answer != QMessageBox.Yes:
			self._log(f"{operation_name} cancelled: serial disconnected")
			self.statusBar().showMessage("Serial connection required", 3000)
			return False

		progress = QProgressDialog("Connecting serial...", "", 0, 1, self)
		progress.setWindowTitle("Working")
		progress.setCancelButton(None)
		progress.setWindowModality(Qt.WindowModal)
		progress.setMinimumDuration(0)
		progress.setValue(0)
		progress.show()
		QApplication.processEvents()

		try:
			self.controller.serial_connect()
			progress.setValue(1)
			QApplication.processEvents()
			self._log(f"Serial connected ({self.selected_serial_port})")
			self.statusBar().showMessage("Serial connected", 3000)
			return True
		except Exception as exc:
			self._show_error(f"Serial connect failed: {exc}")
			return False
		finally:
			progress.close()
			progress.deleteLater()
			self._update_connection_ui()

	@contextmanager
	def _button_progress(self, button_name: str):
		self._log(f"Button activated: {button_name}")
		progress = QProgressDialog(f"{button_name} in progress...", "", 0, 0, self)
		progress.setWindowTitle("Working")
		progress.setCancelButton(None)
		progress.setWindowModality(Qt.WindowModal)
		progress.setMinimumDuration(0)
		progress.show()
		QApplication.processEvents()
		try:
			yield progress
		finally:
			progress.close()
			progress.deleteLater()

	def _advance_progress(self, progress: QProgressDialog | None, value: int, label: str | None = None) -> None:
		if progress is None:
			return
		if label is not None:
			progress.setLabelText(label)
		progress.setValue(value)
		progress.repaint()
		QApplication.processEvents()

	def _verify_program_words(self) -> list[tuple[int, int, int]]:
		mismatches: list[tuple[int, int, int]] = []
		for address, expected in self.program_words:
			data = self.controller.read_memory(address, 1)
			if not data:
				mismatches.append((address & ADDRESS_MASK, expected & WORD_MASK, -1))
				continue
			actual = data[0][1] & WORD_MASK
			if actual != (expected & WORD_MASK):
				mismatches.append((address & ADDRESS_MASK, expected & WORD_MASK, actual))
		return mismatches

	def select_serial_device(self) -> None:
		with self._button_progress("Select Serial Device"):
			ports = self.controller.available_serial_ports()
			choices = ports if ports else [self.selected_serial_port]
			current_index = 0
			if self.selected_serial_port in choices:
				current_index = choices.index(self.selected_serial_port)

			selected, ok = QInputDialog.getItem(
				self,
				"Select Serial Device",
				"Serial device:",
				choices,
				current_index,
				True,
			)
			if not ok or not selected.strip():
				return

			self.selected_serial_port = selected.strip()
			was_connected = self.controller.is_serial_connected()
			try:
				self.controller.set_serial_port(self.selected_serial_port)
				if was_connected:
					self.controller.serial_connect()
				self._log(f"Serial device set to {self.selected_serial_port}")
				self.statusBar().showMessage(f"Serial device: {self.selected_serial_port}", 3000)
			except Exception as exc:
				self._show_error(f"Failed to set serial device: {exc}")
			finally:
				self._update_connection_ui()

	def set_default_base_address(self) -> None:
		with self._button_progress("Set Default Base Address"):
			current = format_octal(self.default_base_address)
			text, ok = QInputDialog.getText(
				self,
				"Set Default Base Address",
				"Default base (octal):",
				text=current,
			)
			if not ok:
				return

			try:
				new_base = parse_octal_input(text) & ADDRESS_MASK
			except ValueError as exc:
				self._show_error(f"Invalid default base address: {exc}")
				return

			self.default_base_address = new_base
			formatted = format_octal(new_base)
			self.mem_start_input.setText(formatted)
			self.program_base_input.setText(formatted)
			self.go_address_input.setText(formatted)
			self.statusBar().showMessage(f"Default base address set to {formatted}", 3000)
			self._log(f"Default base address set to {formatted}")

	def toggle_serial_connection(self) -> None:
		self._log("Button activated: Connect / Disconnect")
		if self.controller.is_serial_connected():
			progress = QProgressDialog("Disconnecting serial...", "", 0, 1, self)
			progress.setWindowTitle("Working")
			progress.setCancelButton(None)
			progress.setWindowModality(Qt.WindowModal)
			progress.setMinimumDuration(0)
			progress.setValue(0)
			progress.show()
			QApplication.processEvents()
			try:
				self.controller.serial_disconnect()
				progress.setValue(1)
				QApplication.processEvents()
				self._log("Serial disconnected")
				self.statusBar().showMessage("Serial disconnected", 3000)
			except Exception as exc:
				self._show_error(f"Serial connect/disconnect failed: {exc}")
			finally:
				progress.close()
				progress.deleteLater()
				self._update_connection_ui()
			return

		progress = QProgressDialog("Connecting and refreshing...", "", 0, 3, self)
		progress.setWindowTitle("Working")
		progress.setCancelButton(None)
		progress.setWindowModality(Qt.WindowModal)
		progress.setMinimumDuration(0)
		progress.setValue(0)
		progress.show()
		QApplication.processEvents()

		try:
			self.controller.serial_connect()
			progress.setLabelText("Connected, refreshing registers...")
			progress.setValue(1)
			QApplication.processEvents()

			self.refresh_registers(show_progress=False)
			progress.setLabelText("Refreshing memory view...")
			progress.setValue(2)
			QApplication.processEvents()

			self.read_memory_range(show_progress=False)
			progress.setValue(3)
			QApplication.processEvents()

			self._log(f"Serial connected ({self.selected_serial_port})")
			self.statusBar().showMessage("Serial connected", 3000)
		except Exception as exc:
			self._show_error(f"Serial connect/disconnect failed: {exc}")
		finally:
			progress.close()
			progress.deleteLater()
			self._update_connection_ui()

	def _refresh_program_table(self) -> None:
		self.program_table.setRowCount(0)
		for row, (address, word) in enumerate(self.program_words):
			self.program_table.insertRow(row)
			addr_item = QTableWidgetItem(self._format_word(address))
			word_item = QTableWidgetItem(self._format_word(word))
			ascii_item = QTableWidgetItem(to_ascii_from_word(word))
			info_item = QTableWidgetItem(to_info_from_address(address))
			for col, item in enumerate((addr_item, word_item, ascii_item, info_item)):
				item.setFont(self.fixed_font)
				self.program_table.setItem(row, col, item)

	def _refresh_breakpoint_table(self) -> None:
		self.breakpoint_table.setRowCount(0)
		for row, address in enumerate(sorted(self.breakpoints)):
			self.breakpoint_table.insertRow(row)
			item = QTableWidgetItem(self._format_word(address))
			item.setFont(self.fixed_font)
			self.breakpoint_table.setItem(row, 0, item)

	def _parse_program_text(self, text: str, default_base: int) -> list[tuple[int, int]]:
		entries: list[tuple[int, int]] = []
		current_address: int | None = default_base & ADDRESS_MASK

		for raw_line in text.splitlines():
			line = raw_line.split(";", 1)[0].split("#", 1)[0].strip()
			if not line:
				continue

			tokens = line.replace(",", " ").split()
			if not tokens:
				continue

			if tokens[0].endswith(":"):
				current_address = parse_numeric_input(tokens[0][:-1], "octal") & ADDRESS_MASK
				tokens = tokens[1:]
				if not tokens:
					continue

			numbers = [parse_numeric_input(token, "octal") for token in tokens]
			values = numbers
			if len(numbers) >= 2 and not tokens[0].startswith(("0x", "0o")) and len(tokens[0]) >= 5:
				current_address = numbers[0] & ADDRESS_MASK
				values = numbers[1:]

			if current_address is None:
				current_address = default_base & ADDRESS_MASK

			for word in values:
				entries.append((current_address, word & WORD_MASK))
				current_address = (current_address + 2) & ADDRESS_MASK

		return entries

	def _parse_program_binary(self, payload: bytes, default_base: int) -> list[tuple[int, int]]:
		entries: list[tuple[int, int]] = []
		address = default_base & ADDRESS_MASK

		# PDP-11 stores words little-endian: low byte first, then high byte.
		for offset in range(0, len(payload), 2):
			low = payload[offset]
			high = payload[offset + 1] if offset + 1 < len(payload) else 0
			word = ((high << 8) | low) & WORD_MASK
			entries.append((address, word))
			address = (address + 2) & ADDRESS_MASK

		return entries

	def _parse_octal_text_loader(self, text: str) -> list[tuple[int, int]]:
		"""Parse lines in the form: address octal_word [octal_word ...] ;comment"""
		entries: list[tuple[int, int]] = []

		for line_no, raw_line in enumerate(text.splitlines(), start=1):
			line = raw_line.split(";", 1)[0].strip()
			if not line:
				continue

			tokens = line.split()
			if len(tokens) < 2:
				raise ValueError(
					f"Octal text loader expects at least 2 tokens at line {line_no}"
				)

			base_address = parse_octal_input(tokens[0]) & ADDRESS_MASK
			for index, token in enumerate(tokens[1:]):
				word = parse_octal_input(token) & WORD_MASK
				address = (base_address + (index * 2)) & ADDRESS_MASK
				entries.append((address, word))

		return entries

	def _parse_lst_loader(self, text: str) -> list[tuple[int, int]]:
		"""Parse assembler list lines in the form: address opcode ;comment."""
		entries: list[tuple[int, int]] = []

		for line_no, raw_line in enumerate(text.splitlines(), start=1):
			line = raw_line.split(";", 1)[0].strip()
			if not line:
				continue

			match = re.match(r"^([0-7]{4,6})\s+([0-7]{1,8})\b", line)
			if not match:
				continue

			address = parse_octal_input(match.group(1)) & ADDRESS_MASK
			raw_word = parse_octal_input(match.group(2))
			if raw_word > WORD_MASK:
				raise ValueError(
					f"Listing opcode exceeds 16 bits at line {line_no}: {match.group(2)}"
				)
			word = raw_word & WORD_MASK
			entries.append((address, word))

		if not entries:
			raise ValueError("No address/opcode pairs found in listing file")

		for index in range(1, len(entries)):
			previous_address = entries[index - 1][0]
			current_address = entries[index][0]
			if current_address == previous_address:
				raise ValueError(f"Duplicate listing address at line {index + 1}")

		return entries

	def _parse_pdp11_absolute_binary(self, payload: bytes) -> tuple[list[tuple[int, int]], int | None]:
		"""Parse common PDP-11 absolute loader record streams.

		Record framing used here: 001,000 sync then little-endian count field.
		The count includes count/address/data/checksum bytes (sync not included).
		"""
		entries: list[tuple[int, int]] = []
		start_address: int | None = None
		cursor = 0

		while cursor + 6 <= len(payload):
			sync_at = payload.find(b"\x01\x00", cursor)
			if sync_at < 0:
				break

			if sync_at + 6 > len(payload):
				break

			rec_count = payload[sync_at + 2] | (payload[sync_at + 3] << 8)
			if rec_count < 5:
				cursor = sync_at + 2
				continue

			record_end = sync_at + 2 + rec_count
			if record_end > len(payload):
				break

			record = payload[sync_at + 2:record_end]
			load_address = record[2] | (record[3] << 8)
			data_bytes = record[4:-1]

			# Keep parsing even if checksum fails; many dumps omit/alter checksum bytes.
			checksum_ok = (sum(record) & 0xFF) == 0
			if not checksum_ok:
				self._log(
					f"Absolute record checksum mismatch near offset {sync_at:o}; continuing"
				)

			if data_bytes:
				address = load_address & ADDRESS_MASK
				for idx in range(0, len(data_bytes), 2):
					low = data_bytes[idx]
					high = data_bytes[idx + 1] if idx + 1 < len(data_bytes) else 0
					word = ((high << 8) | low) & WORD_MASK
					entries.append((address, word))
					address = (address + 2) & ADDRESS_MASK
			else:
				start_address = load_address & ADDRESS_MASK

			cursor = record_end

		return entries, start_address

	def read_program_file(self) -> None:
		with self._button_progress("Read Program File"):
			try:
				default_base = self._parse_input(self.program_base_input.text())
			except ValueError as exc:
				self._show_error(f"Invalid default program base address: {exc}")
				return

			path, _ = QFileDialog.getOpenFileName(
				self,
				"Read Program File",
				"",
				"Program Files (*.txt *.lst *.mem *.odt *.oct *.otl *.bin *.lda *.ptp *.abs);;All Files (*)",
			)
			if not path:
				return

			try:
				with open(path, "rb") as handle:
					raw = handle.read()

				suffix = Path(path).suffix.lower()
				if suffix in {".bin"}:
					self.program_words = self._parse_program_binary(raw, default_base)
				elif suffix in {".lst"}:
					content = raw.decode("utf-8")
					self.program_words = self._parse_lst_loader(content)
				elif suffix in {".lda", ".ptp", ".abs"}:
					self.program_words, start_address = self._parse_pdp11_absolute_binary(raw)
					if start_address is not None:
						self.go_address_input.setText(format_octal(start_address))
						self._log(f"Absolute loader start address {format_octal(start_address)}")
				elif suffix in {".oct", ".otl"}:
					content = raw.decode("utf-8")
					self.program_words = self._parse_octal_text_loader(content)
				else:
					try:
						content = raw.decode("utf-8")
					except UnicodeDecodeError:
						abs_words, start_address = self._parse_pdp11_absolute_binary(raw)
						if abs_words:
							self.program_words = abs_words
							if start_address is not None:
								self.go_address_input.setText(format_octal(start_address))
								self._log(f"Absolute loader start address {format_octal(start_address)}")
						else:
							# Unknown binary format fallback: map bytes as flat PDP-11 words.
							self.program_words = self._parse_program_binary(raw, default_base)
					else:
						try:
							self.program_words = self._parse_octal_text_loader(content)
						except ValueError:
							self.program_words = self._parse_program_text(content, default_base)

				if not self.program_words:
					raise ValueError("No program words found in file")
				first_address = self.program_words[0][0] & ADDRESS_MASK
				formatted_base = format_octal(first_address)
				self.program_base_input.setText(formatted_base)
				self.mem_start_input.setText(formatted_base)
				self._refresh_program_table()
				self.statusBar().showMessage(f"Loaded {len(self.program_words)} words from file", 3000)
				self._log(
					f"Read program file {path} with {len(self.program_words)} words "
					f"starting at {formatted_base}"
				)
			except (OSError, ValueError) as exc:
				self._show_error(f"Program file read failed: {exc}")

	def load_program_to_target(self) -> None:
		self._log("Button activated: Load Program to Target")
		if not self._ensure_connected("Load Program to Target"):
			return
		if not self.program_words:
			self._show_error("No program data loaded")
			return

		total_words = len(self.program_words)
		total_steps = total_words * 2
		progress = QProgressDialog("Loading and verifying program...", "", 0, total_steps, self)
		progress.setWindowTitle("Working")
		progress.setCancelButton(None)
		progress.setWindowModality(Qt.WindowModal)
		progress.setMinimumDuration(0)
		progress.show()
		QApplication.processEvents()

		try:
			for index, (address, word) in enumerate(self.program_words, start=1):
				self.controller.write_memory(address, word)
				progress.setLabelText(f"Writing word {index}/{total_words}...")
				progress.setValue(index)
				QApplication.processEvents()

			mismatches = []
			for index, (address, expected) in enumerate(self.program_words, start=1):
				actual = self.controller.read_memory(address, 1)[0][1] & WORD_MASK
				if actual != (expected & WORD_MASK):
					mismatches.append((address & ADDRESS_MASK, expected & WORD_MASK, actual))
				progress.setLabelText(f"Verifying word {index}/{total_words}...")
				progress.setValue(total_words + index)
				QApplication.processEvents()

			if mismatches:
				first_address, expected, actual = mismatches[0]
				self._log(
					f"Program verify failed at {self._format_word(first_address)} "
					f"expected {self._format_word(expected)} got {self._format_word(actual)}"
				)
				self._show_error(
					f"Program verify failed: {len(mismatches)} mismatch(es). "
					f"First at {self._format_word(first_address)}"
				)
				return

			self.statusBar().showMessage("Program loaded and verified", 3000)
			self._log(f"Loaded and verified {total_words} words to target memory")
			self.read_memory_range(show_progress=False)
		except Exception as exc:
			self._show_error(f"Program load failed: {exc}")
		finally:
			progress.close()
			progress.deleteLater()

	def add_breakpoint(self) -> None:
		if not self._ensure_connected("Add Breakpoint"):
			return
		with self._button_progress("Add Breakpoint"):
			try:
				address = self._parse_input(self.breakpoint_input.text())
				self.controller.set_breakpoint(address)
				self.breakpoints.add(address & ADDRESS_MASK)
				self._refresh_breakpoint_table()
				self.statusBar().showMessage("Breakpoint added", 2000)
				self._log(f"Set breakpoint at {self._format_word(address)}")
			except ValueError as exc:
				self._show_error(f"Invalid breakpoint address: {exc}")
			except Exception as exc:
				self._show_error(f"Set breakpoint failed: {exc}")

	def remove_selected_breakpoints(self) -> None:
		if not self._ensure_connected("Remove Selected Breakpoint(s)"):
			return
		with self._button_progress("Remove Selected Breakpoint(s)"):
			selected_rows = sorted({item.row() for item in self.breakpoint_table.selectedItems()})
			if not selected_rows:
				self._show_error("Select one or more breakpoints to remove")
				return

			try:
				for row in selected_rows:
					cell = self.breakpoint_table.item(row, 0)
					if cell is None:
						continue
					address = self._parse_input(cell.text())
					self.controller.clear_breakpoint(address)
					self.breakpoints.discard(address & ADDRESS_MASK)
				self._refresh_breakpoint_table()
				self.statusBar().showMessage("Breakpoint(s) removed", 2000)
				self._log("Removed selected breakpoint(s)")
			except Exception as exc:
				self._show_error(f"Clear breakpoint failed: {exc}")

	def go_command(self) -> None:
		if not self._ensure_connected("Go"):
			return
		with self._button_progress("Go (g)"):
			raw = self.go_address_input.text().strip()
			if not raw:
				raw = "000100"
				self.go_address_input.setText(raw)

			try:
				address = self._parse_input(raw)
				from_pc: int | None = None
				try:
					from_pc = self.controller.read_registers().get("PC")
				except Exception:
					from_pc = None
				self.controller.go(address)
				from_text = self._format_word(from_pc) if from_pc is not None else "<unknown>"
				self._log(f"Go command (G) from {from_text} to {self._format_word(address)}")
				self.statusBar().showMessage("Go command sent", 2000)
			except ValueError as exc:
				self._show_error(f"Invalid go address: {exc}")
			except Exception as exc:
				self._show_error(f"Go command failed: {exc}")

	def proceed_command(self) -> None:
		if not self._ensure_connected("Proceed"):
			return
		with self._button_progress("Proceed (p)"):
			try:
				self.controller.proceed()
				self._log("Proceed command (p)")
				self.statusBar().showMessage("Proceed command sent", 2000)
			except Exception as exc:
				self._show_error(f"Proceed command failed: {exc}")

	def refresh_all(self, show_progress: bool = True) -> None:
		if not self._ensure_connected("Refresh All"):
			return
		progress_ctx = self._button_progress("Refresh All") if show_progress else nullcontext()
		with progress_ctx:
			self.refresh_registers(show_progress=False)
			self.read_memory_range(show_progress=False)
			self.statusBar().showMessage("Refreshed all panels", 3000)

	def refresh_registers(self, show_progress: bool = True) -> None:
		if not self._ensure_connected("Refresh Registers"):
			return
		progress_ctx = self._button_progress("Refresh Registers") if show_progress else nullcontext()
		with progress_ctx:
			try:
				regs = self.controller.read_registers()
			except Exception as exc:
				self._show_error(f"Register read failed: {exc}")
				return
			for row, reg in enumerate(REGISTER_ORDER):
				value_item = self.register_table.item(row, 1)
				if value_item is None:
					value_item = QTableWidgetItem()
					self.register_table.setItem(row, 1, value_item)
				value_item.setText(self._format_word(regs.get(reg, 0)))
				value_item.setFont(self.fixed_font)
				decoded_item = self.register_table.item(row, 2)
				if decoded_item is None:
					decoded_item = QTableWidgetItem()
					decoded_item.setFlags(decoded_item.flags() & ~Qt.ItemIsEditable)
					self.register_table.setItem(row, 2, decoded_item)
				decoded_item.setFont(self.fixed_font)
				decoded = decode_psw(regs.get(reg, 0)) if reg == "PSW" else ""
				decoded_item.setText(decoded)
				if reg == "PSW":
					self.psw_toolbar_label.setText(decoded)
			self._log("Read registers")

	def write_selected_or_all_registers(self) -> None:
		if not self._ensure_connected("Write Register"):
			return
		with self._button_progress("Write Register"):
			selected_rows = sorted({item.row() for item in self.register_table.selectedItems()})
			rows = selected_rows if selected_rows else list(range(self.register_table.rowCount()))

			try:
				for row in rows:
					name = self.register_table.item(row, 0).text()
					raw_value = self.register_table.item(row, 1).text()
					parsed = self._parse_input(raw_value)
					self.controller.write_register(name, parsed)
					self._log(f"Write register {name} = {self._format_word(parsed)}")
				self.statusBar().showMessage("Register write complete", 3000)
				self.refresh_registers(show_progress=False)
			except (ValueError, KeyError, NotImplementedError) as exc:
				self._show_error(f"Register write failed: {exc}")

	def read_memory_range(self, show_progress: bool = True) -> None:
		if not self._ensure_connected("Read Memory"):
			return
		if show_progress:
			self._log("Button activated: Read Memory")
			progress = QProgressDialog("Reading memory...", "", 0, 1, self)
			progress.setWindowTitle("Working")
			progress.setCancelButton(None)
			progress.setWindowModality(Qt.WindowModal)
			progress.setMinimumDuration(0)
			progress.setAutoClose(False)
			progress.setAutoReset(False)
			progress.setValue(0)
			progress.show()
			progress.forceShow()
			QApplication.processEvents()
		else:
			progress = None

		try:
			try:
				start = self._parse_input(self.mem_start_input.text())
				count = self._parse_input(self.mem_length_input.text())
				if count <= 0:
					raise ValueError("Length must be greater than 0")
			except ValueError as exc:
				self._show_error(f"Invalid memory range: {exc}")
				return

			if progress is not None:
				progress.setMaximum(count + 2)
				self._advance_progress(progress, 1, "Reading target memory...")

			try:
				if show_progress:
					data: list[tuple[int, int]] = []
					for index in range(count):
						address = (start + (index * 2)) & ADDRESS_MASK
						chunk = self.controller.read_memory(address, 1)
						if chunk:
							data.append(chunk[0])
						self._advance_progress(
							progress,
							index + 2,
							f"Reading target memory... {index + 1}/{count}",
						)
				else:
					data = self.controller.read_memory(start, count)
			except Exception as exc:
				self._show_error(f"Memory read failed: {exc}")
				return

			self._advance_progress(progress, count + 1, "Updating memory table...")

			for address, word in data:
				masked_address = address & ADDRESS_MASK
				self.memory_words[masked_address] = word & WORD_MASK
				self._set_memory_row(masked_address, word)
			self._scroll_memory_to_address(start)

			self._log(
				"Read memory start="
				f"{self._format_word(start)} count={count}"
			)
			self._advance_progress(progress, count + 2, "Read complete")
		finally:
			if progress is not None:
				progress.close()
				progress.deleteLater()

	def write_memory_word(self) -> None:
		if not self._ensure_connected("Write Memory Word"):
			return
		self._log("Button activated: Write Memory Word")
		progress = QProgressDialog("Writing memory word...", "", 0, 4, self)
		progress.setWindowTitle("Working")
		progress.setCancelButton(None)
		progress.setWindowModality(Qt.WindowModal)
		progress.setMinimumDuration(0)
		progress.setAutoClose(False)
		progress.setAutoReset(False)
		progress.setValue(0)
		progress.show()
		progress.forceShow()
		QApplication.processEvents()

		try:
			try:
				address = self._parse_input(self.mem_write_address_input.text())
				value = self._parse_input(self.mem_write_value_input.text())
			except ValueError as exc:
				self._show_error(f"Invalid memory write input: {exc}")
				return

			self._advance_progress(progress, 1, "Writing word to target memory...")
			self.controller.write_memory(address, value)

			self._advance_progress(progress, 2, "Verifying written value...")
			verify = self.controller.read_memory(address, 1)
			actual = verify[0][1] & WORD_MASK if verify else None
			if actual != (value & WORD_MASK):
				raise RuntimeError(
					f"Memory write verify failed at {self._format_word(address)}: "
					f"expected {self._format_word(value)} got "
					f"{self._format_word(actual) if actual is not None else '<unknown>'}"
				)

			self._advance_progress(progress, 3, "Refreshing memory view...")
			self.memory_words[address & ADDRESS_MASK] = actual
			self._set_memory_row(address, actual)
			self._scroll_memory_to_address(address)
			self.statusBar().showMessage("Memory write successful", 3000)
			self._log(
				f"Write memory [{self._format_word(address)}] = "
				f"{self._format_word(value)}"
			)
			self._advance_progress(progress, 4, "Write complete")
		except Exception as exc:
			self._show_error(f"Memory write failed: {exc}")
		finally:
			progress.close()
			progress.deleteLater()

def main() -> int:
	app = QApplication(sys.argv)
	_icon = QIcon(str(Path(__file__).parent / "icon.png"))
	app.setWindowIcon(_icon)

	try:
		backend = SerialODTBackend(port=DEFAULT_ODT_PORT, auto_connect=False)
	except Exception as exc:
		QMessageBox.critical(
			None,
			"ODT Connection Error",
			f"Serial backend unavailable on {DEFAULT_ODT_PORT}:\n{exc}",
		)
		return 1
	controller = MonitorController(backend)

	window = MainWindow(controller)
	
	window.show()
	return app.exec()


if __name__ == "__main__":
	raise SystemExit(main())
