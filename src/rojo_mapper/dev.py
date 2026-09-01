from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import psutil
from rich.console import Console
from watchfiles import Change

from rojo_mapper.diagnostics import Diagnostic, ExpectedFailure, Phase, render_human
from rojo_mapper.service import candidate_for, load_workspace, select_single_target
from rojo_mapper.watcher import (
    MANIFEST_NAME,
    cancel_task,
    watch_structural_changes,
    watched_paths,
    write_candidate,
)

ROJO_PORT = 34872
ROJO_ENDPOINT = f"localhost:{ROJO_PORT}"
_VERSION_PATTERN = re.compile(r"(?P<version>\d+\.\d+\.\d+)")


@dataclass(slots=True)
class _ProcessToken:
    expected_stop: bool = False


class RojoSupervisor:
    def __init__(self, root: Path, console: Console) -> None:
        self.root = root
        self.console = console
        self.process: asyncio.subprocess.Process | None = None
        self._token: _ProcessToken | None = None
        self._log_task: asyncio.Task[object] | None = None
        self._monitor_task: asyncio.Task[object] | None = None
        self.failure: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._closing = False

    async def start(self) -> None:
        if await _port_is_open(ROJO_PORT):
            raise ExpectedFailure(
                [
                    Diagnostic(
                        "rojo.port_in_use",
                        f"default Rojo endpoint {ROJO_ENDPOINT} is already in use",
                        Phase.ROJO,
                    )
                ]
            )
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        try:
            process = await asyncio.create_subprocess_exec(
                "rojo",
                "serve",
                MANIFEST_NAME,
                cwd=self.root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                creationflags=creationflags,
                start_new_session=os.name != "nt",
            )
        except OSError as error:
            raise ExpectedFailure(
                [Diagnostic("rojo.failed", str(error), Phase.ROJO, path=MANIFEST_NAME)]
            ) from error
        self.process = process
        token = _ProcessToken()
        self._token = token
        self._log_task = asyncio.create_task(self._pump_output(process))
        self._monitor_task = asyncio.create_task(self._monitor(process, token))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.returncode is not None:
                break
            if await _port_is_open(ROJO_PORT):
                self.console.print(f"[green]Rojo ready[/green] at {ROJO_ENDPOINT}")
                return
            await asyncio.sleep(0.05)
        token.expected_stop = True
        await self.stop()
        raise ExpectedFailure(
            [
                Diagnostic(
                    "rojo.failed",
                    "Rojo exited or did not open its default endpoint within 10 seconds",
                    Phase.ROJO,
                    path=MANIFEST_NAME,
                )
            ]
        )

    async def stop(self) -> None:
        process = self.process
        token = self._token
        if process is None:
            return
        if token is not None:
            token.expected_stop = True
        descendants = _process_tree(process.pid)
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                if os.name == "nt":
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    os.killpg(process.pid, signal.SIGINT)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=3)
        await asyncio.to_thread(_terminate_remaining, descendants)
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
        if self._log_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._log_task
        if self._monitor_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._monitor_task
        self.process = None
        self._token = None
        if not await _wait_port_closed(ROJO_PORT, timeout=5):
            raise ExpectedFailure(
                [
                    Diagnostic(
                        "rojo.port_still_open",
                        f"default Rojo endpoint {ROJO_ENDPOINT} did not close",
                        Phase.ROJO,
                    )
                ]
            )

    async def close(self) -> None:
        self._closing = True
        await self.stop()

    async def _pump_output(self, process: asyncio.subprocess.Process) -> None:
        if process.stdout is None:
            return
        while line := await process.stdout.readline():
            self.console.print(f"[dim]rojo[/dim] {line.decode(errors='replace').rstrip()}")

    async def _monitor(
        self,
        process: asyncio.subprocess.Process,
        token: _ProcessToken,
    ) -> None:
        return_code = await process.wait()
        if not token.expected_stop and not self._closing and not self.failure.done():
            self.failure.set_exception(
                ExpectedFailure(
                    [
                        Diagnostic(
                            "rojo.failed",
                            f"Rojo exited unexpectedly with status {return_code}",
                            Phase.ROJO,
                            path=MANIFEST_NAME,
                        )
                    ]
                )
            )


async def check_rojo_version() -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            "rojo",
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
    except OSError as error:
        raise ExpectedFailure([Diagnostic("rojo.failed", str(error), Phase.ROJO)]) from error
    text = output.decode(errors="replace").strip()
    match = _VERSION_PATTERN.search(text)
    if process.returncode != 0 or match is None:
        raise ExpectedFailure(
            [Diagnostic("rojo.failed", f"unable to determine Rojo version: {text}", Phase.ROJO)]
        )
    version = match.group("version")
    parts = tuple(int(part) for part in version.split("."))
    if version != "7.6.1" and not ((7, 7, 1) <= parts < (7, 8, 0)):
        raise ExpectedFailure(
            [
                Diagnostic(
                    "rojo.unsupported_version",
                    "supported Rojo versions are exactly 7.6.1 or >=7.7.1,<7.8; 7.7.0 is broken",
                    Phase.ROJO,
                    details={"installed": version},
                )
            ]
        )
    return version


def run_dev(root: Path, requested: str | None, console: Console) -> str:
    try:
        return asyncio.run(_run_dev(root, requested, console))
    except KeyboardInterrupt:
        console.print("[yellow]Development session stopped[/yellow]")
        return requested or ""


async def _run_dev(root: Path, requested: str | None, console: Console) -> str:
    workspace = load_workspace(root)
    target = select_single_target(workspace, requested)
    candidate = candidate_for(workspace, target)
    write_candidate(workspace.config, candidate)
    version = await check_rojo_version()
    console.print(f"Using Rojo {version}")
    supervisor = RojoSupervisor(workspace.config.root, console)
    await supervisor.start()
    current_candidate = candidate
    watch_task: asyncio.Task[None] | None = None

    async def regenerate(_changes: set[tuple[Change, str]]) -> tuple[Path, ...] | None:
        nonlocal workspace, current_candidate
        try:
            next_workspace = load_workspace(root)
            next_target = select_single_target(next_workspace, target)
            next_candidate = candidate_for(next_workspace, next_target)
        except ExpectedFailure as error:
            render_human(console, error.diagnostics, title="Recoverable snapshot")
            return None
        next_paths = watched_paths(next_workspace.config)
        if next_candidate.fingerprint == current_candidate.fingerprint:
            changed = write_candidate(next_workspace.config, next_candidate)
            if changed:
                console.print("[green]Updated tree[/green] without restarting Rojo")
        else:
            try:
                await supervisor.stop()
                write_candidate(next_workspace.config, next_candidate)
                await supervisor.start()
            except Exception as error:
                with contextlib.suppress(Exception):
                    await supervisor.close()
                message = (
                    error.diagnostics[0].message
                    if isinstance(error, ExpectedFailure) and error.diagnostics
                    else str(error)
                )
                raise ExpectedFailure(
                    [
                        Diagnostic(
                            "dev.rojo_restart_failed",
                            f"metadata restart failed after the old session stopped: {message}",
                            Phase.DEV,
                        )
                    ]
                ) from error
            console.print(
                "[bold yellow]dev.rojo_restarted_reconnect_required:[/bold yellow] "
                f"Rojo session metadata changed. Reconnect Studio manually to {ROJO_ENDPOINT}; "
                "automatic reconnection is not expected."
            )
        workspace = next_workspace
        current_candidate = next_candidate
        return next_paths

    try:
        watch_task = asyncio.create_task(
            watch_structural_changes(watched_paths(workspace.config), regenerate)
        )
        await asyncio.sleep(0)
        console.print("[green]Watching structural paths[/green]")
        done, _ = await asyncio.wait(
            {watch_task, supervisor.failure},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if supervisor.failure in done:
            await supervisor.failure
        if watch_task in done:
            if watch_task.cancelled():
                raise ExpectedFailure(
                    [
                        Diagnostic(
                            "watcher.failed",
                            "structural watcher task was cancelled unexpectedly",
                            Phase.WATCHER,
                        )
                    ]
                )
            error = watch_task.exception()
            if isinstance(error, ExpectedFailure):
                raise error
            if error is not None:
                raise ExpectedFailure(
                    [Diagnostic("watcher.failed", str(error), Phase.WATCHER)]
                ) from error
            raise ExpectedFailure(
                [
                    Diagnostic(
                        "watcher.failed",
                        "structural watcher terminated unexpectedly",
                        Phase.WATCHER,
                    )
                ]
            )
    finally:
        cleanup_task = asyncio.create_task(_cleanup_dev(watch_task, supervisor))
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            if current_task is not None:
                current_task.uncancel()
            await cleanup_task
            raise
    return target


async def _cleanup_dev(
    watch_task: asyncio.Task[None] | None,
    supervisor: RojoSupervisor,
) -> None:
    if watch_task is not None and not watch_task.done():
        await cancel_task(watch_task)
    with contextlib.suppress(ExpectedFailure):
        await supervisor.close()


async def _port_is_open(port: int) -> bool:
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port),
            timeout=0.2,
        )
    except OSError, TimeoutError:
        return False
    writer.close()
    await writer.wait_closed()
    return True


async def _wait_port_closed(port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not await _port_is_open(port):
            return True
        await asyncio.sleep(0.05)
    return not await _port_is_open(port)


def _process_tree(pid: int) -> list[psutil.Process]:
    try:
        root = psutil.Process(pid)
        return [*root.children(recursive=True), root]
    except psutil.Error:
        return []


def _terminate_remaining(processes: list[psutil.Process]) -> None:
    alive = [process for process in processes if process.is_running()]
    for process in alive:
        with contextlib.suppress(psutil.Error):
            process.terminate()
    _, alive = psutil.wait_procs(alive, timeout=2)
    for process in alive:
        with contextlib.suppress(psutil.Error):
            process.kill()
    psutil.wait_procs(alive, timeout=2)
