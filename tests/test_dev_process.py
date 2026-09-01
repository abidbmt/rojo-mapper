import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from rich.console import Console
from watchfiles import Change

import rojo_mapper.dev as dev
import rojo_mapper.watcher as watcher
from rojo_mapper.diagnostics import ExpectedFailure
from rojo_mapper.service import candidate_for, load_workspace


class FakeStream:
    def __init__(self, lines: list[bytes]) -> None:
        self.lines = lines

    async def readline(self) -> bytes:
        return self.lines.pop(0) if self.lines else b""


class FakeProcess:
    def __init__(self, *, stdout: FakeStream | None = None) -> None:
        self.pid = 987654321
        self.returncode: int | None = None
        self.stdout = stdout
        self.event = asyncio.Event()
        self.signals: list[int] = []
        self.killed = False

    async def wait(self) -> int:
        await self.event.wait()
        assert self.returncode is not None
        return self.returncode

    def send_signal(self, value: int) -> None:
        self.signals.append(value)
        self.returncode = 0
        self.event.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.event.set()


async def make_supervisor(tmp_path: Path) -> dev.RojoSupervisor:
    return dev.RojoSupervisor(tmp_path, Console(record=True))


def test_supervisor_rejects_busy_port(tmp_path: Path, monkeypatch) -> None:
    async def scenario() -> None:
        supervisor = await make_supervisor(tmp_path)
        monkeypatch.setattr(dev, "_port_is_open", AsyncMock(return_value=True))
        with pytest.raises(ExpectedFailure) as captured:
            await supervisor.start()
        assert captured.value.diagnostics[0].kind == "rojo.port_in_use"
        await supervisor.stop()

    asyncio.run(scenario())


def test_supervisor_spawn_failure(tmp_path: Path, monkeypatch) -> None:
    async def scenario() -> None:
        supervisor = await make_supervisor(tmp_path)
        monkeypatch.setattr(dev, "_port_is_open", AsyncMock(return_value=False))
        monkeypatch.setattr(
            dev.asyncio,
            "create_subprocess_exec",
            AsyncMock(side_effect=OSError("missing")),
        )
        with pytest.raises(ExpectedFailure) as captured:
            await supervisor.start()
        assert captured.value.diagnostics[0].kind == "rojo.failed"

    asyncio.run(scenario())


def test_supervisor_start_output_and_clean_stop(tmp_path: Path, monkeypatch) -> None:
    async def scenario() -> None:
        supervisor = await make_supervisor(tmp_path)
        process = FakeProcess(stdout=FakeStream([b"ready\n"]))
        monkeypatch.setattr(dev, "_port_is_open", AsyncMock(side_effect=[False, True, False]))
        monkeypatch.setattr(
            dev.asyncio,
            "create_subprocess_exec",
            AsyncMock(return_value=process),
        )
        monkeypatch.setattr(dev, "_process_tree", lambda _pid: [])
        await supervisor.start()
        assert "Rojo ready" in supervisor.console.export_text()
        await supervisor.stop()
        assert process.signals
        assert supervisor.process is None

    asyncio.run(scenario())


def test_supervisor_start_timeout(tmp_path: Path, monkeypatch) -> None:
    async def scenario() -> None:
        supervisor = await make_supervisor(tmp_path)
        process = FakeProcess()
        monkeypatch.setattr(dev, "_port_is_open", AsyncMock(return_value=False))
        monkeypatch.setattr(
            dev.asyncio,
            "create_subprocess_exec",
            AsyncMock(return_value=process),
        )
        monkeypatch.setattr(dev, "_process_tree", lambda _pid: [])
        monkeypatch.setattr(
            dev,
            "time",
            SimpleNamespace(monotonic=Mock(side_effect=[0, 11, 12, 20])),
        )
        with pytest.raises(ExpectedFailure) as captured:
            await supervisor.start()
        assert captured.value.diagnostics[0].kind == "rojo.failed"

    asyncio.run(scenario())


def test_version_command_failures(monkeypatch) -> None:
    async def os_failure() -> None:
        monkeypatch.setattr(
            dev.asyncio,
            "create_subprocess_exec",
            AsyncMock(side_effect=OSError("missing")),
        )
        with pytest.raises(ExpectedFailure) as captured:
            await dev.check_rojo_version()
        assert captured.value.diagnostics[0].kind == "rojo.failed"

    asyncio.run(os_failure())

    class Process:
        returncode = 1

        async def communicate(self):
            return b"bad output", b""

    monkeypatch.setattr(
        dev.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=Process()),
    )
    with pytest.raises(ExpectedFailure) as captured:
        asyncio.run(dev.check_rojo_version())
    assert captured.value.diagnostics[0].kind == "rojo.failed"


def test_run_dev_keyboard_interrupt(tmp_path: Path, monkeypatch) -> None:
    def interrupt(coroutine) -> None:
        coroutine.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(dev.asyncio, "run", interrupt)
    console = Console(record=True)
    assert dev.run_dev(tmp_path, "Main", console) == "Main"
    assert "stopped" in console.export_text().lower()


def test_port_helpers_and_missing_process() -> None:
    async def scenario() -> None:
        server = await asyncio.start_server(lambda _reader, writer: writer.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        assert await dev._port_is_open(port)
        server.close()
        await server.wait_closed()
        assert await dev._wait_port_closed(port, 1)

    asyncio.run(scenario())
    assert dev._process_tree(987654321) == []


def test_wait_port_closed_timeout(monkeypatch) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(dev, "_port_is_open", AsyncMock(return_value=True))
        monkeypatch.setattr(
            dev,
            "time",
            SimpleNamespace(monotonic=Mock(side_effect=[0, 2])),
        )
        assert not await dev._wait_port_closed(123, 1)

    asyncio.run(scenario())


def test_watch_iterator_reconfiguration_and_termination(tmp_path: Path, monkeypatch) -> None:
    calls = 0

    def fake_awatch(*_paths, **_kwargs):
        async def iterator():
            nonlocal calls
            calls += 1
            if calls == 1:
                yield {(Change.added, str(tmp_path / "file"))}

        return iterator()

    async def scenario() -> None:
        monkeypatch.setattr(watcher, "awatch", fake_awatch)

        async def callback(_changes):
            return (tmp_path / "next",)

        with pytest.raises(RuntimeError, match="terminated unexpectedly"):
            await watcher.watch_structural_changes((tmp_path,), callback)
        assert calls == 2

    asyncio.run(scenario())


def test_write_candidate_read_failure(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "Source" / "Places" / "Main" / "Server"
    source.mkdir(parents=True)
    (source / "A.luau").write_text("return {}", encoding="utf-8")
    (tmp_path / "rojo-mapper.toml").write_text("schema = 1\n", encoding="utf-8")
    workspace = load_workspace(tmp_path)
    candidate = candidate_for(workspace, "Main")
    (tmp_path / watcher.MANIFEST_NAME).write_text("old", encoding="utf-8")
    original = Path.read_bytes

    def fail_manifest(path: Path) -> bytes:
        if path.name == watcher.MANIFEST_NAME:
            raise OSError("unreadable")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", fail_manifest)
    with pytest.raises(ExpectedFailure) as captured:
        watcher.write_candidate(workspace.config, candidate)
    assert captured.value.diagnostics[0].kind == "filesystem.read_failed"
