"""
Parser layer: REPL text -> structured data.

REPL protocol facts this module relies on (taken from the KITES sources):
  * Failures are reported on stderr as "error: <message>" and run-time
    failures as "run error: <message> (line N)".
  * Program output arrives on stdout as "[Syscall output: <value>]" and exit as
    "Exited with exit code: <n>"; a breakpoint hit prints
    "paused at breakpoint, PC=0x...".
"""

import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import REG_BYTE_SWAP
from errors import BridleError

REG_SECTION_HEADERS = (
    ("General-purpose", "gpr"),
    ("Floating-point", "fpr"),
    ("Control and status", "csr"),
)
REG_LINE_RE = re.compile(r"^\s*([A-Za-z_][\w.\-]*)\s*=\s*0x([0-9A-Fa-f]+)\s*$")
INFO_LINE_RE = re.compile(r"^\s*([A-Za-z][A-Za-z ]*?)\s*:\s*(\S.*?)\s*$")
INFO_KEYS = {
    "Cycles": "cycles",
    "Instructions retired": "instructions_retired",
    "CPI": "cpi",
    "IPC": "ipc",
    "Stall cycles": "stall_cycles",
    "Branch mispredictions": "branch_mispredictions",
}
SYSCALL_RE = re.compile(r"\[Syscall output: (.*?)\](?=\n|\Z)", re.S)
EXIT_RE = re.compile(r"Exited with exit code:\s*(-?\d+)")
PAUSE_RE = re.compile(r"paused at breakpoint, PC=0x([0-9A-Fa-f]+)")
RUN_ERROR_RE = re.compile(r"^run error: (.*?)(?: \(line (\d+)\))?\s*$", re.M)
ERROR_LINE_RE = re.compile(r"^error: (.*?)\s*$", re.M)
LINE_IN_MESSAGE_RE = re.compile(r"\bline\s+(\d+)\b", re.I)
MEMORY_SPEC_RE = re.compile(r"^(?:0[xX])?([0-9A-Fa-f]{1,16}):(\d{1,4})$")


def _to_number(text: str) -> Optional[Any]:
    """Convert '12' -> 12, '1.5' -> 1.5; NaN/inf/garbage -> None (JSON-safe)."""
    try:
        return int(text)
    except ValueError:
        pass
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _normalize_reg_hex(digits: str) -> str:
    """Normalise the hex digits of one register value to '0x' + 16 digits.

    Default: the digits are used as printed (zero-padded on the left to 16).
    With BRIDLE_REG_BYTE_SWAP=1 the 16 digits are split into 8 byte pairs, the
    pair order is reversed and the result is re-joined (little-endian -> big-endian).
    """
    digits = digits.lower().zfill(16)
    if REG_BYTE_SWAP:
        pairs = [digits[i : i + 2] for i in range(0, len(digits), 2)]
        digits = "".join(reversed(pairs))
    return "0x" + digits


def parse_regs(text: str) -> Dict[str, Dict[str, str]]:
    """Parse `regs` output into {"gpr": {...}, "fpr": {...}, "csr": {...}}.

    Every value is returned as '0x' + 16 lower-case hex digits (see _normalize_reg_hex)."""
    sections: Dict[str, Dict[str, str]] = {"gpr": {}, "fpr": {}, "csr": {}}
    current: Optional[str] = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.endswith(":"):
            header = next(
                (key for prefix, key in REG_SECTION_HEADERS if stripped.startswith(prefix)),
                None,
            )
            if header:
                current = header
                continue
        match = REG_LINE_RE.match(line)
        if match and current:
            sections[current][match.group(1)] = _normalize_reg_hex(match.group(2))
    return sections


def parse_info(text: str) -> Tuple[Optional[int], Dict[str, Any]]:
    """Parse `info` output into (program_counter, statistics)."""
    pc: Optional[int] = None
    stats: Dict[str, Any] = {}
    for line in text.splitlines():
        match = INFO_LINE_RE.match(line)
        if not match:
            continue
        label, value = match.group(1).strip(), match.group(2).strip()
        if label == "PC":
            try:
                pc = int(value, 16)
            except ValueError:
                pc = None
        elif label in INFO_KEYS:
            stats[INFO_KEYS[label]] = _to_number(value)
    return pc, stats


def parse_error_lines(stderr: str) -> List[str]:
    """Messages from lines of the form 'error: <message>'."""
    return [m.strip() for m in ERROR_LINE_RE.findall(stderr)]


def summarize_execution(mode: str, stdout: str, stderr: str) -> Dict[str, Any]:
    """Turn the raw output of run / run_debug / step into a status summary."""
    program_output = [m.group(1) for m in SYSCALL_RE.finditer(stdout)]
    exit_match = EXIT_RE.search(stdout)
    pause_match = PAUSE_RE.search(stdout)
    run_error = RUN_ERROR_RE.search(stderr)
    other_errors = parse_error_lines(stderr)

    error: Optional[Dict[str, Any]] = None
    if run_error:
        error = {
            "code": "RUNTIME_ERROR",
            "message": run_error.group(1).strip(),
            "line": int(run_error.group(2)) if run_error.group(2) else None,
        }
    elif other_errors:
        error = {"code": "COMMAND_FAILED", "message": "; ".join(other_errors), "line": None}

    if error:
        status = "error"
    elif pause_match:
        status = "paused_at_breakpoint"
    elif exit_match:
        status = "finished"
    elif mode == "step":
        status = "stepped"
    else:
        status = "finished"

    return {
        "status": status,
        "mode": mode,
        "exit_code": int(exit_match.group(1)) if exit_match else None,
        "paused_pc": ("0x" + pause_match.group(1).lower().zfill(16)) if pause_match else None,
        "program_output": program_output,
        "error": error,
        "stdout": stdout,
        "stderr": stderr,
    }


def parse_memory_specs(specs: Optional[List[str]]) -> List[Tuple[str, int]]:
    """Validate '<hex_addr>:<rows>' strings (rows are 8-byte rows)."""
    if not specs:
        return []
    if len(specs) > 16:
        raise BridleError(400, "INVALID_REQUEST", "at most 16 memory regions per request")
    regions: List[Tuple[str, int]] = []
    for spec in specs:
        match = MEMORY_SPEC_RE.match(spec.strip())
        if not match:
            raise BridleError(
                400,
                "INVALID_REQUEST",
                f"bad memory spec '{spec}'; expected <hex_addr>:<rows>, e.g. 0x1000:4",
            )
        rows = int(match.group(2))
        if not 1 <= rows <= 1024:
            raise BridleError(400, "INVALID_REQUEST", "memory rows must be between 1 and 1024")
        regions.append((match.group(1), rows))
    return regions


def vm_state_dir(workspace: Path) -> Path:
    """Where kites-cli writes errors_dump.json / memory_dump.json (see CLI.md)."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Kites" / "vm_state"
    return workspace / "vm_state"


def _read_json_file(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)
