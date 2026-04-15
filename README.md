# DCJ11 / PDP-11 Tools

This repository contains PDP-11 and DCJ11-related tools, assembly sources, and a desktop monitor GUI for talking to an ODT console over a serial connection.

## GUI Overview

The GUI lives in [GUI/gui.py](GUI/gui.py) and provides a PySide6 desktop front end for a PDP-11 style ODT monitor.

### Features

- connect to a target over a serial port
- select the serial device from the GUI
- view and refresh CPU registers
- decode the PSW into readable flag and mode information
- read and write memory words
- display memory as address, word, ASCII, and PDP-11 info labels
- load program files into target memory
- optionally skip post-load verification from the Preferences menu
- set and remove breakpoints
- issue Go and Proceed commands
- view a live log of serial traffic and GUI actions

### Supported loader inputs

The program loader can read these formats:

- plain text / octal text
- assembler listing files such as .lst
- raw binary files such as .bin
- PDP-11 absolute loader style files such as .lda, .ptp, and .abs

## Install

### 1. Create a Python virtual environment

Run from the repository root or from the [GUI](GUI) folder:

```bash
cd GUI
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install PySide6 pyserial
```

## Run

From the [GUI](GUI) folder:

```bash
source .venv/bin/activate
python gui.py
```

Or from the repository root:

```bash
GUI/.venv/bin/python GUI/gui.py
```

## Using the GUI

1. Start the application.
2. Use the Connection menu or toolbar to connect to the target.
3. Select the correct serial device if needed.
4. Read a program file with the Program Loader panel.
5. Load the program to target memory.
6. Use the memory, register, breakpoint, and execution controls to inspect or run the system.

## Notes

- The default serial device is set to /dev/ttyUSB0.
- The GUI expects a working ODT-compatible serial monitor on the target.
- If program loading is slow or your target monitor is limited, you can enable Skip Verify After Load from the Preferences menu.
- PDP11 compiler MACRO11 https://github.com/j-hoppe/MACRO11
- OBJ2BIN https://github.com/AK6DN/obj2bin

## Known Issues
- GUI may/may not run in a maximized mode.
- Loadtimes for large programs (>1K words) can take a while.