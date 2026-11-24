# Bridle & KitesWeb

**Bridle** is a REST API (FastAPI) that gives programs access to the **KITES** RISC-V simulator.
It runs `kites-cli --repl` as a headless subprocess, one per session, and turns the REPL's text
output into JSON. **KitesWeb** is the browser frontend built on Bridle, and `testing/` holds an
automated 50-test verification suite that runs against the same API.

```
Bridle/
├── README.md
├── requirements.txt
├── backend/                 Bridle API
│   ├── main.py              app setup, CORS, lifespan, static mount, entry point
│   ├── config.py            environment variables and constants
│   ├── errors.py            BridleError and internal exceptions
│   ├── parsers.py           REPL text -> JSON (regs, info, run summary, memory specs)
│   ├── session.py           Session (one kites-cli process) + SessionRegistry
│   ├── models.py            request validation (LoadRequest, ExecuteRequest)
│   ├── routes.py            HTTP endpoints
│   └── handlers.py          uniform JSON error responses
├── frontend/                KitesWeb (served by Bridle at /)
│   ├── index.html
│   ├── app.js
│   └── style.css
└── testing/
    ├── run_tests.py         automated test runner (standard library only)
    └── riscv_50_suite.json  test cases
```

---

## 1. Prerequisites

| Tool | Version | Used for |
|------|---------|----------|
| Qt | 6.5.3, MinGW 64-bit kit | building and running `kites-cli` |
| MinGW-w64 | 11.2 (`mingw1120_64`, ships with the Qt installer) | compiler for `kites-cli` |
| CMake | 3.16 or newer | building `kites-cli` |
| Python | 3.9 or newer | Bridle and the test runner |

On Windows, install Qt with the **Qt Online Installer** and select **Qt 6.5.3 → MinGW 64-bit** plus
**Developer and Designer Tools → MinGW 11.2.0 64-bit**. Bridle expects the default install
locations `C:\Qt\6.5.3\mingw_64` and `C:\Qt\Tools\mingw1120_64` (see [Troubleshooting](#7-troubleshooting)
if yours differ).

## 2. Build `kites-cli`

> **Check against the KITES repository:** the repository URL, the CMake target name and the folder
> the binary ends up in below are placeholders. Replace them with the values from the KITES build files.

```powershell
# Windows (PowerShell), from the KITES source folder
git clone <https://github.com/hghit-harshit/Kites> kites
cd kites

$env:PATH = "C:\Qt\Tools\mingw1120_64\bin;C:\Qt\6.5.3\mingw_64\bin;$env:PATH"
cmake -S . -B build -G "MinGW Makefiles" -DCMAKE_PREFIX_PATH="C:/Qt/6.5.3/mingw_64" -DCMAKE_BUILD_TYPE=Release
cmake --build build --target kites-cli -j
```

```bash
# Linux / macOS (Qt 6 installed system-wide or via the Qt installer)
git clone <https://github.com/hghit-harshit/Kites> kites && cd kites
cmake -S . -B build -DCMAKE_PREFIX_PATH=/path/to/Qt/6.5.3/<kit> -DCMAKE_BUILD_TYPE=Release
cmake --build build --target kites-cli -j
```

Check that the binary works before going further:

```powershell
.\build\kites-cli.exe --repl
# you should see a banner and the prompt "kites> "; type exit to leave
```

## 3. Install Bridle

```powershell
cd Bridle
python -m venv .venv
.venv\Scripts\activate          # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
```

## 4. Run the server

Tell Bridle where `kites-cli` is, then start it **from inside `backend/`**. The modules import each
other by name, so the working directory matters.

```powershell
$env:KITES_CLI_PATH = "C:\path\to\kites\build\kites-cli.exe"   # Linux/macOS: export KITES_CLI_PATH=...
cd backend
python main.py                       # or: uvicorn main:app --host 127.0.0.1 --port 8000
```

- **KitesWeb:** open <http://127.0.0.1:8000/>
- **Interactive API docs (Swagger):** <http://127.0.0.1:8000/docs>

If `kites-cli` is on your `PATH`, you can skip `KITES_CLI_PATH`.

### Configuration

All of these are optional environment variables.

| Variable | Default | Meaning |
|----------|---------|---------|
| `KITES_CLI_PATH` | `kites-cli` | path or name of the simulator binary |
| `BRIDLE_HOST` / `BRIDLE_PORT` | `127.0.0.1` / `8000` | bind address for `python main.py` |
| `BRIDLE_EXEC_TIMEOUT` | `30` | seconds allowed for `run` / `step` / `run_debug` |
| `BRIDLE_CMD_TIMEOUT` | `10` | seconds allowed for any other REPL command |
| `BRIDLE_STARTUP_TIMEOUT` | `15` | seconds allowed for `kites-cli` to reach its prompt |
| `BRIDLE_SESSION_TTL` | `900` | idle seconds before a session is closed automatically |
| `BRIDLE_MAX_SESSIONS` | `20` | maximum concurrent sessions (one `kites-cli` process each) |
| `BRIDLE_MAX_SOURCE_BYTES` | `262144` | maximum assembly source size |
| `BRIDLE_MAX_OUTPUT_BYTES` | `2097152` | maximum output captured per command |
| `BRIDLE_WORKSPACE_ROOT` | system temp dir | parent folder for per-session workspaces |
| `BRIDLE_CORS_ORIGINS` | `*` | comma-separated browser origins allowed to call the API |

### API at a glance

| Method & path | Body / query | Returns |
|---------------|--------------|---------|
| `POST /sessions` | none | `{session_id}`: starts a private `kites-cli` process |
| `POST /sessions/{id}/load` | `{"source": "...", "vm": "single-cycle"}` | `{status: "loaded"}` or `422 ASSEMBLY_ERROR` with line numbers |
| `POST /sessions/{id}/execute` | `{"mode": "run" \| "step" \| "run_debug", "count": 1, "breakpoints": [..]}` | status, exit code, program output, errors |
| `POST /sessions/{id}/reset` | none | returns the program to its just-loaded state |
| `GET /sessions/{id}/state` | `?memory=<hex_addr>:<rows>` (repeatable, 8-byte rows) | PC, registers, stats, optional memory |
| `DELETE /sessions/{id}` | none | stops the process and deletes the workspace |

Every error has the shape `{"error": {"code": "...", "message": "...", "details": {...}}}`.
Register values are 64-bit, as `0x` + 16 lower-case hex digits: −1 is `0xffffffffffffffff`.

## 5. Run the test suite

With the server running, open a second terminal:

```powershell
cd testing
python run_tests.py riscv_50_suite.json
```

| Option | Default | Meaning |
|--------|---------|---------|
| `--api URL` | `http://127.0.0.1:8000` | Bridle base URL |
| `--vm NAME` | `single-cycle` | `single-cycle`, `rvss`, `rv5-nh-nf`, `rv5-h-nf`, `rv5-nh-f`, `rv5-h-f` |
| `--mode run\|step` | `run` | `run` runs to completion; `step` executes exactly `instruction_count_executed` instructions (plus the setup code) |
| `--only ID [ID ...]` | all | run only these test IDs, e.g. `--only RV15 RV33` |
| `--report FILE` | none | also write a JSON report |
| `-v` | off | show run status and simulator errors for each test |
| `--mem-big-endian` | off | read each 8-byte memory row as big-endian |

Output is one `[PASS]` / `[FAIL]` line per test, with each mismatch listed under failing tests,
then a summary. The exit code is `0` when everything passes and `1` otherwise, so it can gate CI.

```
[PASS] RV01 ADD basic
[FAIL] RV15 SRL vs SRA
         x3: expected 0x0000000008000000, got 0x0ffffffff8000000

49/50 passed, 1 failed
```

### How a test runs

1. One session is created for the whole run.
2. For each test, the runner builds the source: **setup code** (only if the test has initial
   memory or registers) followed by the test's `assembly` lines. It sends this to `/load`, which
   resets the simulator.
3. `/execute` runs the program.
4. `/state` reads the registers and, if the test checks memory, the smallest 8-byte-aligned region
   covering the expected addresses.
5. Every register in `expected_registers` (plus `x0`, which must be 0) and every byte in
   `expected_memory_bytes` is compared.

## 6. Adding test cases

Add an object to the `tests` array in `riscv_50_suite.json`, or create a new suite file with the
same shape and pass it to `run_tests.py`.

```json
{
  "id": "RV51",
  "category": "Arithmetic/logical",
  "name": "SLTU unsigned compare",
  "purpose": "x3=1; x4=0",
  "initial_registers": {},
  "initial_memory_bytes": {},
  "assembly": [
    "addi x1, x0, -1",
    "addi x2, x0, 1",
    "sltu x3, x2, x1",
    "sltu x4, x1, x2"
  ],
  "instruction_count_executed": 4,
  "expected_registers": {
    "x1": "0xFFFFFFFF",
    "x2": "0x00000001",
    "x3": "0x00000001",
    "x4": "0x00000000"
  },
  "expected_memory_bytes": {},
  "expected_memory_note": "Unlisted memory bytes are unchanged from the initial memory image.",
  "expected_register_note": "x0 must remain 0x00000000; unlisted registers retain their initial values."
}
```

Then run only the new test to check it:

```powershell
python run_tests.py riscv_50_suite.json --only RV51 -v
```

### Field reference

| Field | Required | Format and rules |
|-------|----------|------------------|
| `id` | yes | unique string; used by `--only` and the report |
| `name`, `category`, `purpose` | yes | free text, for humans; the runner prints `id` and `name` |
| `assembly` | yes | list of source lines. Labels may share a line: `"loop: addi x1, x1, 1"` |
| `expected_registers` | yes | keys **must be `x0`–`x31`** (not ABI names like `t0`). Values are hex strings, see below |
| `expected_memory_bytes` | yes (may be `{}`) | `"0x<address>": "0x<byte>"`, one entry per byte. Addresses of any width (`0x100`, `0x0000000000000100`) |
| `initial_memory_bytes` | yes (may be `{}`) | `"0x<address>": <decimal 0–255>` |
| `initial_registers` | yes (may be `{}`) | `"xN": <decimal integer>` |
| `instruction_count_executed` | yes | instructions the test program executes; used only with `--mode step` |
| `*_note` fields | no | documentation only |

The top-level `test_count` field is not read by the runner. Update it by hand when you add tests.

### Writing expected register values

Registers are 64-bit (RV64), and the runner normalises expected values before comparing:

- **Up to 8 hex digits** → treated as a 32-bit value and **sign-extended** to 64 bits:
  `"0xFFFFFFF3"` → `0xfffffffffffffff3`, `"0x00000101"` → `0x0000000000000101`.
  This is right for any result that RV64 sign-extends anyway (`addi`, `lui`, `lw`, ...).
- **More than 8 hex digits** → used exactly as written as a full 64-bit value:
  `"0x0FFFFFFFF8000000"`. Use this when the 64-bit result is **not** the sign extension of its
  low 32 bits, e.g. `srl` of a negative value, `ld`, or anything that sets bits 32–63 directly.

### Rules and pitfalls

- **Don't use `x30` or `x31` in tests that have initial memory.** The setup code uses them as
  scratch registers (and zeroes them afterwards); the runner rejects such a test with an error.
- **Setup code shifts addresses.** When `initial_memory_bytes` or `initial_registers` is non-empty,
  the setup code is placed *before* your program, so your code no longer starts at address 0.
  Anything that depends on the PC (the link value written by `jal`/`jalr`, `auipc`) will be off by
  the setup size. Keep PC-dependent tests free of initial state; RV23 is an example.
- **Keep data away from the code.** Setup code takes about 2 instructions (8 bytes) per initial byte.
  Put data at `0x100` or higher and keep the setup + program comfortably below that address.
- **Programs don't need to exit.** In `run` mode a program simply stops after its last instruction;
  pass/fail depends only on the final registers and memory. Use `-v` to see any simulator message.
- **Get the access width right.** `sb`/`sh`/`sw`/`sd` write 1/2/4/8 bytes and
  `lb`/`lh`/`lw`/`ld` read 1/2/4/8 bytes, little-endian. Prefer aligned addresses, since misaligned
  accesses may trap on some cores.
- **Make `instruction_count_executed` exact** if you want the test to pass in `--mode step`:
  count instructions actually executed (loop bodies × iterations), not lines of source.

## 7. Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `SPAWN_FAILED: kites-cli executable '...' not found` | Set `KITES_CLI_PATH` to the full path of the binary. |
| `SPAWN_FAILED: kites-cli did not reach its prompt` | The binary starts but cannot load its Qt/MinGW libraries. On Windows, Bridle adds `C:\Qt\6.5.3\mingw_64\bin` and `C:\Qt\Tools\mingw1120_64\bin` to `PATH` for the child process. If Qt is installed elsewhere, add your Qt `bin` folders to `PATH` before starting Bridle (or run `windeployqt` next to `kites-cli.exe`). On Linux, make sure Qt's libraries are found (e.g. `LD_LIBRARY_PATH`). |
| `ModuleNotFoundError: No module named 'config'` | Start the server from inside `backend/`. |
| `Cannot reach Bridle at ...` in the test runner | The server isn't running, or it's on another host/port; pass `--api`. |
| `409 SESSION_BUSY` | Another request is still running on that session; requests on one session are not queued. |
| `408 TIMEOUT` | The program ran longer than `BRIDLE_EXEC_TIMEOUT` (usually an infinite loop). The session's simulator was restarted, so load the program again. |
| `429 TOO_MANY_SESSIONS` | Delete unused sessions or raise `BRIDLE_MAX_SESSIONS`. |
| Memory tests show byte-reversed values | Run the tests with `--mem-big-endian`. |
| Two sessions see each other's memory dumps (macOS only) | On macOS, kites-cli writes its dump files to one shared folder (`~/Library/Application Support/Kites/vm_state`). Run memory-heavy tests one session at a time. |
