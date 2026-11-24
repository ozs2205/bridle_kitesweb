#!/usr/bin/env python3
"""
Minimal automated test runner for the Bridle API (KITES RISC-V simulator).

Usage:
    python run_tests.py riscv_50_suite.json
    python run_tests.py suite.json --api http://127.0.0.1:8000 --only RV01 RV31 -v
    python run_tests.py suite.json --report results.json

Per test:  load (assembly + prologue) -> execute -> GET /state (+ memory) -> compare.

Format rules (Bridle output):
  * registers are 64-bit: "0x" + 16 lower-case hex digits.
    The suite's 32-bit expectations are sign-extended to 64 bits before comparing,
    e.g. "0xFFFFFFF3" -> "0xfffffffffffffff3",  "0x00000101" -> "0x0000000000000101".
  * memory addresses are compared as integers (0x00000100 == 0x0000000000000100).
  * initial_memory_bytes / initial_registers cannot be set through the API, so they
    are written by a small assembly prologue (li/sb using scratch regs x31/x30,
    which are zeroed again afterwards).

Only stdlib is used.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

ABI = ["zero", "ra", "sp", "gp", "tp", "t0", "t1", "t2", "s0", "s1",
       "a0", "a1", "a2", "a3", "a4", "a5", "a6", "a7", "s2", "s3",
       "s4", "s5", "s6", "s7", "s8", "s9", "s10", "s11", "t3", "t4", "t5", "t6"]
SCRATCH_VAL, SCRATCH_ADDR = "x31", "x30"


# ----------------------------------------------------------------- HTTP --- #
class ApiError(Exception):
    pass


def call(api, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(api + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=120) as res:
            text = res.read().decode()
            return json.loads(text) if text else None
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace")
        try:
            err = json.loads(text)["error"]
            msg = f"[{err['code']}] {err['message']}"
            if err.get("details"):
                msg += f" {json.dumps(err['details'])}"
        except Exception:
            msg = f"HTTP {e.code}: {text[:300]}"
        raise ApiError(msg) from None
    except urllib.error.URLError as e:
        raise ApiError(f"cannot reach {api}: {e.reason}") from None


# ------------------------------------------------------------ formatting --- #
def sext64(hex32: str) -> str:
    """'0xFFFFFFF3' -> '0xfffffffffffffff3' (32-bit expectation -> 64-bit register).
    Values written with more than 8 hex digits are taken as full 64-bit values."""
    digits = hex32.lower().removeprefix("0x").replace("_", "")
    if len(digits) > 8:
        return norm64(hex32)
    v = int(hex32, 16) & 0xFFFFFFFF
    if v & 0x80000000:
        v |= 0xFFFFFFFF00000000
    return f"0x{v:016x}"


def norm64(text: str) -> str:
    return f"0x{int(str(text), 16) & 0xFFFFFFFFFFFFFFFF:016x}"


def reg_value(regs: dict, i: int):
    """Bridle returns names as the REPL prints them (x5 or t0, s0 or fp ...)."""
    for name in (f"x{i}", ABI[i], "fp" if i == 8 else None):
        if name and name in regs:
            return norm64(regs[name])
    return None


# ------------------------------------------------------------- prologue --- #
def li_len(value: int) -> int:
    return 1 if -2048 <= value <= 2047 else 2


def build_prologue(test):
    """Assembly lines that recreate initial_registers / initial_memory_bytes."""
    lines, n = [], 0
    used = " ".join(test["assembly"]).replace(",", " ").split()
    if test["initial_memory_bytes"] and (SCRATCH_VAL in used or SCRATCH_ADDR in used):
        raise ValueError(f"{test['id']}: program uses scratch register {SCRATCH_VAL}/{SCRATCH_ADDR}")

    for addr_s, byte in test["initial_memory_bytes"].items():
        addr, byte = int(addr_s, 16), int(byte) & 0xFF
        lines.append(f"li {SCRATCH_VAL}, {byte}"); n += li_len(byte)
        if addr <= 2047:
            lines.append(f"sb {SCRATCH_VAL}, {addr}(x0)"); n += 1
        else:
            lines.append(f"li {SCRATCH_ADDR}, {addr}"); n += li_len(addr)
            lines.append(f"sb {SCRATCH_VAL}, 0({SCRATCH_ADDR})"); n += 1
    if test["initial_memory_bytes"]:
        lines += [f"li {SCRATCH_VAL}, 0", f"li {SCRATCH_ADDR}, 0"]; n += 2

    for reg, value in test["initial_registers"].items():
        value = int(value)
        lines.append(f"li {reg}, {value}"); n += li_len(value)
    return lines, n


# --------------------------------------------------------------- memory --- #
def _pairs(node, out):
    """Collect (address, value) pairs from whatever layout memory_dump.json uses."""
    if isinstance(node, list):
        for item in node:
            if isinstance(item, dict):
                a = next((k for k in item if "addr" in k.lower()), None)
                v = next((k for k in item if k != a and any(s in k.lower() for s in ("val", "data", "hex", "byte"))), None)
                if a and v and not isinstance(item[a], (dict, list)):
                    out.append((item[a], item[v])); continue
            _pairs(item, out)
    elif isinstance(node, dict):
        for k, v in node.items():
            try:
                int(str(k), 16)
                is_addr = True
            except ValueError:
                is_addr = False
            if is_addr and isinstance(v, (str, int, list)):
                out.append((k, v))
            else:
                _pairs(v, out)
    return out


def memory_bytes(dump, little_endian=True) -> dict:
    """{address(int): byte(int)} from the 8-byte rows of a memory dump."""
    result = {}
    for addr, value in _pairs(dump, []):
        base = int(addr, 16) if isinstance(addr, str) else int(addr)
        if isinstance(value, list):                                   # [b0, b1, ...]
            row = [int(b, 16) if isinstance(b, str) else int(b) for b in value]
        elif isinstance(value, str) and len(value.split()) > 1:       # "7b 00 00 ..."
            row = [int(b, 16) for b in value.split()]
        else:                                                         # one 64-bit number
            num = int(value, 16) if isinstance(value, str) else int(value)
            row = list(num.to_bytes(8, "little" if little_endian else "big"))
        for i, b in enumerate(row):
            result[base + i] = b & 0xFF
    return result


def memory_spec(expected_mem: dict):
    """One '<addr>:<rows>' region (8-byte rows) covering every expected address."""
    addrs = [int(a, 16) for a in expected_mem]
    start, end = min(addrs) & ~7, max(addrs)
    return f"0x{start:x}:{(end - start) // 8 + 1}"


# ------------------------------------------------------------- run test --- #
def run_test(api, sid, test, args):
    prologue, pro_len = build_prologue(test)
    source = "\n".join(prologue + test["assembly"]) + "\n"
    failures = []

    call(api, "POST", f"/sessions/{sid}/load", {"source": source, "vm": args.vm})
    body = {"mode": args.mode}
    if args.mode == "step":
        body["count"] = test["instruction_count_executed"] + pro_len
    exe = call(api, "POST", f"/sessions/{sid}/execute", body)

    query = ""
    if test["expected_memory_bytes"]:
        query = "?memory=" + memory_spec(test["expected_memory_bytes"])
    state = call(api, "GET", f"/sessions/{sid}/state{query}")
    regs = state["registers"]

    expected_regs = dict(test["expected_registers"])
    expected_regs.setdefault("x0", "0x00000000")          # x0 must always be 0
    for reg in sorted(expected_regs, key=lambda r: int(r[1:])):
        want = sext64(expected_regs[reg])
        got = reg_value(regs, int(reg[1:]))
        if got != want:
            failures.append(f"{reg}: expected {want}, got {got}")

    if test["expected_memory_bytes"]:
        if state.get("memory_error") or state.get("memory") is None:
            failures.append(f"memory: {state.get('memory_error', 'no memory returned')}")
        else:
            mem = memory_bytes(state["memory"], not args.mem_big_endian)
            for addr_s, byte_s in test["expected_memory_bytes"].items():
                addr, want = int(addr_s, 16), int(byte_s, 16)
                got = mem.get(addr)
                if got != want:
                    got_s = "missing" if got is None else f"0x{got:02x}"
                    failures.append(f"mem[0x{addr:016x}]: expected 0x{want:02x}, got {got_s}")

    return failures, exe, state


def main():
    p = argparse.ArgumentParser(description="Run a RISC-V JSON test suite against Bridle.")
    p.add_argument("suite", help="path to the test-suite JSON")
    p.add_argument("--api", default="http://127.0.0.1:8000")
    p.add_argument("--vm", default="single-cycle")
    p.add_argument("--mode", choices=["run", "step"], default="run",
                   help="run: run to completion; step: step exactly instruction_count_executed (+prologue)")
    p.add_argument("--mem-big-endian", action="store_true",
                   help="treat each 8-byte memory row value as big-endian (default: little-endian)")
    p.add_argument("--only", nargs="*", help="run only these test ids")
    p.add_argument("--report", help="write a JSON report to this file")
    p.add_argument("-v", "--verbose", action="store_true", help="print run status / program output too")
    args = p.parse_args()

    with open(args.suite, encoding="utf-8") as f:
        tests = json.load(f)["tests"]
    if args.only:
        tests = [t for t in tests if t["id"] in set(args.only)]

    sid = call(args.api, "POST", "/sessions")["session_id"]
    results, passed = [], 0
    try:
        for t in tests:
            label = f"{t['id']} {t['name']}"
            try:
                failures, exe, state = run_test(args.api, sid, t, args)
                note = f"run status={exe['status']}" + (f", error={exe['error']['message']}" if exe.get("error") else "")
            except (ApiError, ValueError, KeyError) as e:
                failures, note = [f"ERROR: {e}"], ""
                if "SESSION_NOT_FOUND" in str(e):          # reaped/crashed: start a new one
                    sid = call(args.api, "POST", "/sessions")["session_id"]
            ok = not failures
            passed += ok
            print(f"[{'PASS' if ok else 'FAIL'}] {label}")
            for line in failures:
                print(f"         {line}")
            if args.verbose and note:
                print(f"         ({note})")
            results.append({"id": t["id"], "name": t["name"], "passed": ok,
                            "failures": failures, "note": note})
    finally:
        try:
            call(args.api, "DELETE", f"/sessions/{sid}")
        except ApiError:
            pass

    total = len(tests)
    print(f"\n{passed}/{total} passed, {total - passed} failed")
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump({"passed": passed, "total": total, "results": results}, f, indent=2)
        print(f"report written to {args.report}")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
