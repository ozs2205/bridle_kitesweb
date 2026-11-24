"""
Session manager + CLI wrapper.

One `Session` = one `kites-cli --repl` subprocess + one private temp workspace.
`SessionRegistry` creates, looks up, reaps and tears sessions down.

REPL protocol facts this module relies on (taken from the KITES sources):
  * The REPL prints the prompt "kites> " (no trailing newline) and flushes it
    every time it is ready for the next command; a command is "done" when the
    prompt appears again.
  * `load`, `run`, `step`, `reset`, `add_breakpoint`, ... print NOTHING on
    success.
  * The REPL is a single-threaded line loop: while `run` is executing it cannot
    read further commands (so `stop` cannot interrupt it). A timed-out run is
    therefore handled by killing and restarting the REPL process.
"""

import asyncio
import os
import shutil
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Optional, Tuple

from config import (
    CMD_TIMEOUT,
    KITES_CLI_PATH,
    MAX_OUTPUT_BYTES,
    MAX_SESSIONS,
    PROMPT_BYTES,
    REAP_INTERVAL,
    SESSION_TTL,
    STARTUP_TIMEOUT,
    STDERR_KEEP_BYTES,
    STDERR_SETTLE,
    WORKSPACE_ROOT,
)
from errors import BridleError, OutputLimitExceeded, ReplCrashed
from parsers import parse_error_lines


class Session:
    """One isolated `kites-cli --repl` subprocess with its own workspace."""

    def __init__(self, session_id: str, workspace: Path) -> None:
        self.id = session_id
        self.workspace = workspace
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.lock = asyncio.Lock()
        self.closed = False
        self.loaded = False
        self.vm: Optional[str] = None
        self.breakpoints: set = set()
        self.restarts = 0
        self.restart_note = ""
        self.last_active = time.monotonic()
        self._stderr_buf = bytearray()
        self._stderr_task: Optional["asyncio.Task[None]"] = None

    # ----- lifecycle ------------------------------------------------------- #
    def touch(self) -> None:
        self.last_active = time.monotonic()

    async def start(self) -> None:
        await self._spawn()

    async def _spawn(self) -> None:
        exe = shutil.which(KITES_CLI_PATH)
        if exe is None and Path(KITES_CLI_PATH).is_file():
            exe = KITES_CLI_PATH
        if exe is None:
            raise BridleError(
                500,
                "SPAWN_FAILED",
                f"kites-cli executable '{KITES_CLI_PATH}' not found; set KITES_CLI_PATH",
            )
        # Child-process environment: make the Qt / MinGW runtime DLLs resolvable and
        # run Qt headless (no display needed).
        env = os.environ.copy()
        env["PATH"] = os.pathsep.join(
            [
                r"C:\Qt\6.5.3\mingw_64\bin",
                r"C:\Qt\Tools\mingw1120_64\bin",
                env.get("PATH", ""),
            ]
        )
        env["QT_QPA_PLATFORM"] = "offscreen"
        try:
            self.proc = await asyncio.create_subprocess_exec(
                exe,
                "--repl",
                cwd=str(self.workspace),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise BridleError(500, "SPAWN_FAILED", f"could not start kites-cli: {exc}")

        self._stderr_buf = bytearray()
        self._stderr_task = asyncio.create_task(self._pump_stderr(self.proc))
        try:
            await self._read_until_prompt(STARTUP_TIMEOUT)  # banner + first prompt
        except (asyncio.TimeoutError, ReplCrashed, OutputLimitExceeded) as exc:
            await asyncio.sleep(STDERR_SETTLE)
            tail = self._take_stderr().strip()[-500:]
            await self._kill_proc()
            raise BridleError(
                500,
                "SPAWN_FAILED",
                "kites-cli did not reach its prompt"
                + (f" ({type(exc).__name__})" if str(exc) == "" else f" ({exc})")
                + (f": {tail}" if tail else ""),
            )
        await asyncio.sleep(STDERR_SETTLE)
        self._take_stderr()  # discard start-up noise (e.g. Qt warnings)
        self.loaded = False
        self.vm = None
        self.breakpoints.clear()

    async def _pump_stderr(self, proc: "asyncio.subprocess.Process") -> None:
        """Continuously collect stderr so the pipe never fills and blocks the REPL."""
        try:
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    return
                self._stderr_buf.extend(chunk)
                if len(self._stderr_buf) > STDERR_KEEP_BYTES:
                    del self._stderr_buf[: len(self._stderr_buf) - STDERR_KEEP_BYTES]
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    def _take_stderr(self) -> str:
        text = bytes(self._stderr_buf).decode("utf-8", errors="replace")
        self._stderr_buf.clear()
        return text.replace("\r\n", "\n")

    async def _kill_proc(self) -> None:
        task, self._stderr_task = self._stderr_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        proc = self.proc
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (asyncio.TimeoutError, Exception):
                pass

    async def _restart(self, reason: str) -> None:
        """Crash recovery: replace the REPL with a fresh one (state is lost)."""
        await self._kill_proc()
        self.restarts += 1
        self.restart_note = f"The simulator process was restarted ({reason})."
        self.loaded = False
        self.vm = None
        self.breakpoints.clear()
        if not self.closed:
            await self._spawn()

    async def ensure_alive(self) -> None:
        if self.proc is None or self.proc.returncode is not None:
            await self._restart("it had exited")

    async def close(self) -> None:
        """Graceful 'exit', then terminate/kill, then delete the workspace."""
        self.closed = True
        proc = self.proc
        if proc is not None and proc.returncode is None:
            try:
                proc.stdin.write(b"exit\n")
                await proc.stdin.drain()
                proc.stdin.close()
                await asyncio.wait_for(proc.wait(), timeout=2)
            except Exception:
                pass
        await self._kill_proc()
        await asyncio.to_thread(shutil.rmtree, str(self.workspace), True)

    # ----- exclusive access ------------------------------------------------ #
    @asynccontextmanager
    async def exclusive(self):
        """Serialize access: one command at a time, never queue behind a long run."""
        if self.closed:
            raise BridleError(404, "SESSION_NOT_FOUND", "session was deleted")
        if self.lock.locked():
            raise BridleError(
                409, "SESSION_BUSY", "another request is still running on this session"
            )
        async with self.lock:
            self.touch()
            await self.ensure_alive()
            try:
                yield self
            finally:
                self.touch()

    def require_loaded(self) -> None:
        if not self.loaded:
            message = "no program loaded; call POST /sessions/{id}/load first"
            if self.restart_note:
                message = f"{self.restart_note} {message}"
            raise BridleError(409, "NOT_LOADED", message)

    # ----- CLI wrapper ----------------------------------------------------- #
    async def _read_until_prompt(self, timeout: float) -> str:
        proc = self.proc
        assert proc is not None and proc.stdout is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        buf = bytearray()
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            chunk = await asyncio.wait_for(proc.stdout.read(65536), timeout=remaining)
            if not chunk:
                raise ReplCrashed()
            buf.extend(chunk)
            if len(buf) > MAX_OUTPUT_BYTES + len(PROMPT_BYTES):
                raise OutputLimitExceeded()
            if buf.endswith(PROMPT_BYTES):
                break
        text = bytes(buf[: -len(PROMPT_BYTES)]).decode("utf-8", errors="replace")
        return text.replace("\r\n", "\n")

    async def command(self, cmd: str, timeout: float) -> Tuple[str, str]:
        """Send one REPL command; return (stdout, stderr) produced by it."""
        if "\n" in cmd or "\r" in cmd:
            raise BridleError(400, "INVALID_REQUEST", "command must be a single line")
        proc = self.proc
        if proc is None or proc.stdin is None or proc.returncode is not None:
            await self._restart("it had exited")
            raise BridleError(
                500, "REPL_CRASHED", f"{self.restart_note} Load a program again."
            )
        self._stderr_buf.clear()  # drop anything left over from earlier commands
        try:
            proc.stdin.write((cmd + "\n").encode("utf-8"))
            await proc.stdin.drain()
            stdout = await self._read_until_prompt(timeout)
        except asyncio.TimeoutError:
            await self._restart(f"'{cmd.split()[0]}' exceeded {timeout:g}s")
            raise BridleError(
                408,
                "TIMEOUT",
                f"'{cmd.split()[0]}' did not finish within {timeout:g}s. The simulator was "
                "restarted; all state was lost, load the program again.",
            )
        except OutputLimitExceeded:
            await self._restart(f"'{cmd.split()[0]}' produced too much output")
            raise BridleError(
                500,
                "OUTPUT_LIMIT",
                f"'{cmd.split()[0]}' produced more than {MAX_OUTPUT_BYTES} bytes of output. "
                "The simulator was restarted; load the program again.",
            )
        except (ReplCrashed, BrokenPipeError, ConnectionResetError):
            if self.closed:
                raise BridleError(404, "SESSION_NOT_FOUND", "session was deleted")
            try:
                exit_code = await asyncio.wait_for(proc.wait(), timeout=2)
            except (asyncio.TimeoutError, Exception):
                exit_code = None
            await asyncio.sleep(STDERR_SETTLE)
            tail = self._take_stderr().strip()[-500:]
            await self._restart(f"it crashed with exit code {exit_code}")
            raise BridleError(
                500,
                "REPL_CRASHED",
                f"The simulator crashed while running '{cmd.split()[0]}' "
                f"(exit code {exit_code}); it was restarted, load the program again."
                + (f" stderr: {tail}" if tail else ""),
            )
        await asyncio.sleep(STDERR_SETTLE)  # let the stderr pump deliver trailing text
        return stdout, self._take_stderr()

    @staticmethod
    def raise_if_rejected(stderr: str, what: str) -> None:
        messages = parse_error_lines(stderr)
        if messages:
            raise BridleError(
                400, "COMMAND_FAILED", f"{what} failed: " + "; ".join(messages)
            )

    async def sync_breakpoints(self, desired: set) -> None:
        for line in sorted(self.breakpoints - desired):
            _, err = await self.command(f"remove_breakpoint {line}", CMD_TIMEOUT)
            self.raise_if_rejected(err, "remove_breakpoint")
            self.breakpoints.discard(line)
        for line in sorted(desired - self.breakpoints):
            _, err = await self.command(f"add_breakpoint {line}", CMD_TIMEOUT)
            self.raise_if_rejected(err, "add_breakpoint")
            self.breakpoints.add(line)


class SessionRegistry:
    """Owns all sessions; creates, looks up, reaps and tears them down."""

    def __init__(self) -> None:
        self.sessions: Dict[str, Session] = {}
        self._pending = 0

    async def create(self) -> Session:
        if len(self.sessions) + self._pending >= MAX_SESSIONS:
            raise BridleError(
                429, "TOO_MANY_SESSIONS", f"session limit reached ({MAX_SESSIONS})"
            )
        self._pending += 1
        workspace: Optional[Path] = None
        try:
            session_id = str(uuid.uuid4())
            workspace = Path(
                tempfile.mkdtemp(prefix=f"bridle_{session_id[:8]}_", dir=WORKSPACE_ROOT)
            )
            session = Session(session_id, workspace)
            await session.start()
            self.sessions[session_id] = session
            return session
        except BaseException:
            if workspace is not None:
                shutil.rmtree(str(workspace), ignore_errors=True)
            raise
        finally:
            self._pending -= 1

    def get(self, raw_id: str) -> Session:
        try:
            key = str(uuid.UUID(raw_id))
        except (ValueError, AttributeError, TypeError):
            raise BridleError(404, "SESSION_NOT_FOUND", "unknown or malformed session id")
        session = self.sessions.get(key)
        if session is None or session.closed:
            raise BridleError(404, "SESSION_NOT_FOUND", f"no such session: {key}")
        return session

    async def delete(self, raw_id: str) -> str:
        session = self.get(raw_id)
        self.sessions.pop(session.id, None)
        await session.close()
        return session.id

    async def reap(self) -> None:
        now = time.monotonic()
        for session in list(self.sessions.values()):
            if not session.lock.locked() and now - session.last_active > SESSION_TTL:
                self.sessions.pop(session.id, None)
                await session.close()

    async def reaper(self) -> None:
        while True:
            await asyncio.sleep(REAP_INTERVAL)
            try:
                await self.reap()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # a failed sweep must never kill the reaper

    async def shutdown(self) -> None:
        sessions = list(self.sessions.values())
        self.sessions.clear()
        await asyncio.gather(*(s.close() for s in sessions), return_exceptions=True)


# The single, process-wide registry shared by the routes and the app lifespan.
registry = SessionRegistry()
