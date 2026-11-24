"""
Configuration: environment variables, constants and lookup tables.

Configuration (environment variables, all optional)
---------------------------------------------------
    KITES_CLI_PATH          path/name of the kites-cli binary      (default: kites-cli)
    BRIDLE_WORKSPACE_ROOT   parent directory for session folders   (default: system temp dir)
    BRIDLE_EXEC_TIMEOUT     seconds allowed for run/step/run_debug (default: 30)
    BRIDLE_CMD_TIMEOUT      seconds allowed for any other command  (default: 10)
    BRIDLE_STARTUP_TIMEOUT  seconds allowed for REPL start-up      (default: 15)
    BRIDLE_SESSION_TTL      idle seconds before a session is reaped(default: 900)
    BRIDLE_MAX_SESSIONS     maximum concurrent sessions            (default: 20)
    BRIDLE_MAX_SOURCE_BYTES maximum assembly source size           (default: 262144)
    BRIDLE_MAX_OUTPUT_BYTES maximum output captured per command    (default: 2097152)
    BRIDLE_CORS_ORIGINS     comma-separated allowed browser origins (default: *)
    BRIDLE_REG_BYTE_SWAP    set to 1 to byte-reverse register values from `regs`
                            (only if your kites-cli really prints them byte-swapped)
    BRIDLE_HOST / BRIDLE_PORT  bind address when run via `python main.py`
"""

import os
from pathlib import Path
from typing import Dict


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


KITES_CLI_PATH = os.environ.get("KITES_CLI_PATH", "kites-cli")
WORKSPACE_ROOT = os.environ.get("BRIDLE_WORKSPACE_ROOT") or None
EXEC_TIMEOUT = _env_float("BRIDLE_EXEC_TIMEOUT", 30.0)
CMD_TIMEOUT = _env_float("BRIDLE_CMD_TIMEOUT", 10.0)
STARTUP_TIMEOUT = _env_float("BRIDLE_STARTUP_TIMEOUT", 15.0)
SESSION_TTL = _env_float("BRIDLE_SESSION_TTL", 900.0)
MAX_SESSIONS = _env_int("BRIDLE_MAX_SESSIONS", 20)
MAX_SOURCE_BYTES = _env_int("BRIDLE_MAX_SOURCE_BYTES", 262144)
MAX_OUTPUT_BYTES = _env_int("BRIDLE_MAX_OUTPUT_BYTES", 2097152)
CORS_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("BRIDLE_CORS_ORIGINS", "*").split(",")
    if origin.strip()
]
REG_BYTE_SWAP = 0 #os.environ.get("BRIDLE_REG_BYTE_SWAP", "").strip().lower() in (
    #"1",
    #"true",
    #"yes",
    #"on",
#)

PROMPT = "kites> "
PROMPT_BYTES = PROMPT.encode("ascii")
STDERR_SETTLE = 0.05  # seconds to let the stderr pump catch up after a prompt
REAP_INTERVAL = 30.0  # seconds between idle-session sweeps
STDERR_KEEP_BYTES = 1 << 20  # never keep more than 1 MiB of stderr per command

# Accepted VM names -> the names `kites-cli` understands.
VM_ALIASES: Dict[str, str] = {
    "single-cycle": "rvss",
    "rvss": "rvss",
    "rv5-nh-nf": "rv5-nh-nf",
    "rv5-h-nf": "rv5-h-nf",
    "rv5-nh-f": "rv5-nh-f",
    "rv5-h-f": "rv5-h-f",
}

# Bridle/backend/config.py -> Bridle/frontend
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
