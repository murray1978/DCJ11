## DCJ11 ODT GUI.py

## Registers
Cache Control Register 17777746/

## TODO for gui.py

- Stabilize ODT memory deposit handling and confirm the exact write sequence expected by the target monitor.
- Add clearer serial protocol diagnostics for failed write, go, and register operations.
- Distinguish general registers from special registers more clearly in the UI and menu structure.
- Expand PSW decoding to show bit-level help text and branch-condition interpretation.
- Add stronger verification and retry handling for program load, register writes, and breakpoint operations.
- Review progress dialogs so all long-running actions update consistently without nested popup conflicts.
- Add tests for loader parsing across .bin, .lst, .oct, .lda, .ptp, and .abs formats.
- Add tests for alternate .lst formats, invalid word-width handling, and base-address inference.
- Improve error reporting for unsupported monitor commands and monitor-specific command syntax differences.
- Document the required ODT command conventions used by this GUI, including GO, PROCEED, PSW, and memory write syntax.
- Indicate Memory areas that are reserved for I/O, Traps and interupts.
- Add Trap, Interupt test code
- Add Trap, Interupt runtime code.
- ~~Memory, ASCII column split into ASCII / INFO~~
- ~~add icon.png~~
- Have the ablity to scroll through memory even if memory not loaded, have an indication that that memory has not been read.
- Have the ability for the GUI to access memory above 65k.
- ~~Rename Memnory Viewer label to Memory Read~~
- tick box to have a verifiy memory for Program loader