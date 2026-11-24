"""HTTP endpoints. All routes live on one APIRouter that main.py includes."""

import asyncio
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query

from config import CMD_TIMEOUT, EXEC_TIMEOUT
from errors import BridleError
from models import ExecuteRequest, LoadRequest
from parsers import (
    LINE_IN_MESSAGE_RE,
    _read_json_file,
    parse_error_lines,
    parse_info,
    parse_memory_specs,
    parse_regs,
    summarize_execution,
    vm_state_dir,
)
from session import registry

router = APIRouter()


@router.post("/sessions", status_code=201)
async def create_session() -> Dict[str, str]:
    """Spawn a REPL subprocess in a dedicated temp workspace."""
    session = await registry.create()
    return {"session_id": session.id}


@router.post("/sessions/{session_id}/load")
async def load_program(session_id: str, body: LoadRequest) -> Dict[str, Any]:
    """Save the source to a temp file, then run `load <file> --vm <vm>`."""
    session = registry.get(session_id)
    async with session.exclusive():
        source_path = session.workspace / "program.s"
        if '"' in str(source_path):
            raise BridleError(500, "INTERNAL_ERROR", "workspace path contains a quote character")
        await asyncio.to_thread(source_path.write_bytes, body.source.encode("utf-8"))

        # kites-cli writes errors_dump.json on every assembly; remove any stale copy first.
        errors_file = vm_state_dir(session.workspace) / "errors_dump.json"
        try:
            errors_file.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

        # NOTE: the REPL switches VM *before* assembling, so a failed load leaves the
        # session's state undefined -> treat the session as "not loaded" until success.
        session.loaded = False
        out, err = await session.command(
            f'load "{source_path}" --vm {body.vm}', CMD_TIMEOUT
        )

        stderr_problems: List[Dict[str, Any]] = []
        for message in parse_error_lines(err):
            found = LINE_IN_MESSAGE_RE.search(message)
            stderr_problems.append(
                {"line": int(found.group(1)) if found else None, "message": message}
            )
        dump_problems: List[Dict[str, Any]] = []
        try:
            dumped = await asyncio.to_thread(_read_json_file, errors_file)
            if isinstance(dumped, dict) and isinstance(dumped.get("errors"), list):
                for item in dumped["errors"]:
                    if isinstance(item, dict):
                        dump_problems.append(
                            {"line": item.get("line"), "message": str(item.get("message", ""))}
                        )
        except (OSError, ValueError):
            pass  # file absent or unreadable: stderr is the source of truth
        # Prefer the structured dump (line + message); fall back to stderr text.
        problems = dump_problems or stderr_problems

        if problems:
            raise BridleError(
                422,
                "ASSEMBLY_ERROR",
                problems[0]["message"] or "assembly failed",
                details={"errors": problems},
            )

        session.loaded = True
        session.vm = body.vm
        session.restart_note = ""
        session.breakpoints.clear()
        # The REPL clears its own breakpoint list on load but does not push the empty
        # list to the VM; removing a (non-existent) line makes it do exactly that.
        await session.command("remove_breakpoint 0", CMD_TIMEOUT)
        return {"status": "loaded", "vm": body.vm}


@router.post("/sessions/{session_id}/reset")
async def reset_program(session_id: str) -> Dict[str, Any]:
    """Run `reset`: registers/memory/PC return to their just-loaded values."""
    session = registry.get(session_id)
    async with session.exclusive():
        session.require_loaded()
        _, err = await session.command("reset", CMD_TIMEOUT)
        session.raise_if_rejected(err, "reset")
        if session.breakpoints:
            # Idempotent re-push of the REPL's breakpoint list to the freshly reset VM.
            await session.command(f"add_breakpoint {min(session.breakpoints)}", CMD_TIMEOUT)
        return {"status": "reset", "vm": session.vm}


@router.post("/sessions/{session_id}/execute")
async def execute_program(session_id: str, body: ExecuteRequest) -> Dict[str, Any]:
    """Run / step / run-until-breakpoint and summarise what the REPL printed."""
    session = registry.get(session_id)
    async with session.exclusive():
        session.require_loaded()
        if body.breakpoints is not None:
            await session.sync_breakpoints(set(body.breakpoints))
        if body.mode == "run":
            cmd = "run"
        elif body.mode == "run_debug":
            cmd = "run_debug"
        else:
            cmd = f"step {body.count}"
        stdout, stderr = await session.command(cmd, EXEC_TIMEOUT)
        return summarize_execution(body.mode, stdout, stderr)


@router.get("/sessions/{session_id}/state")
async def get_state(
    session_id: str,
    memory: Optional[List[str]] = Query(
        default=None,
        description="repeatable '<hex_addr>:<rows>' (8-byte rows), e.g. memory=0x0:4",
    ),
) -> Dict[str, Any]:
    """Read-only snapshot built from `info`, `regs` (and optionally `dump_mem`)."""
    regions = parse_memory_specs(memory)
    session = registry.get(session_id)
    async with session.exclusive():
        session.require_loaded()

        info_out, info_err = await session.command("info", CMD_TIMEOUT)
        session.raise_if_rejected(info_err, "info")
        regs_out, regs_err = await session.command("regs", CMD_TIMEOUT)
        session.raise_if_rejected(regs_err, "regs")

        raw_output = f"{info_out.rstrip()}\n{regs_out.rstrip()}\n"
        pc, stats = parse_info(info_out)
        sections = parse_regs(regs_out)
        if pc is None or not sections["gpr"]:
            raise BridleError(
                500,
                "PARSE_ERROR",
                "could not parse the REPL's info/regs output (format changed?)",
                details={"raw_output": raw_output},
            )

        result: Dict[str, Any] = {
            "pc": f"0x{pc:016x}",
            "registers": sections["gpr"],
            "fp_registers": sections["fpr"],
            "csr_registers": sections["csr"],
            "stats": stats,
            "raw_output": raw_output,
        }

        if regions:
            dump_file = vm_state_dir(session.workspace) / "memory_dump.json"
            try:
                dump_file.unlink()
            except OSError:
                pass
            args = " ".join(f"{addr} {rows}" for addr, rows in regions)
            _, mem_err = await session.command(f"dump_mem {args}", CMD_TIMEOUT)
            memory_errors = parse_error_lines(mem_err)
            if memory_errors:
                result["memory"] = None
                result["memory_error"] = "; ".join(memory_errors)
            else:
                try:
                    result["memory"] = await asyncio.to_thread(_read_json_file, dump_file)
                except (OSError, ValueError) as exc:
                    result["memory"] = None
                    result["memory_error"] = f"memory_dump.json not readable: {exc}"
        return result


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str) -> Dict[str, str]:
    """Send `exit`, kill the process if needed, delete the workspace."""
    deleted_id = await registry.delete(session_id)
    return {"status": "deleted", "session_id": deleted_id}
