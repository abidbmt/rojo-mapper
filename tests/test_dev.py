import asyncio
import contextlib
from pathlib import Path
from typing import ClassVar
from unittest.mock import AsyncMock

import pytest
from rich.console import Console

import rojo_mapper.dev as dev
import rojo_mapper.watcher as watcher
from rojo_mapper.config import load_config
from rojo_mapper.diagnostics import Diagnostic, ExpectedFailure, Phase
from rojo_mapper.service import candidate_for, load_workspace


def setup_project(root: Path, cloud_id: int | None = None) -> None:
    path = root / "Source" / "Places" / "Main" / "Server"
    path.mkdir(parents=True)
    (path / "Main.server.luau").write_text("print('ok')", encoding="utf-8")
    text = "schema = 1\n"
    if cloud_id is not None:
        text += f"[cloud]\nuniverse_id = 1\n[cloud.places]\nMain = {cloud_id}\n"
    (root / "rojo-mapper.toml").write_text(text, encoding="utf-8")


class FakeSupervisor:
    instances: ClassVar[list[FakeSupervisor]] = []
    fail_second_start = False
    fail_process = False

    def __init__(self, root: Path, console: Console) -> None:
        self.root = root
        self.console = console
        self.order: list[str] = []
        self.starts = 0
        self.failure = asyncio.get_running_loop().create_future()
        self.instances.append(self)

    async def start(self) -> None:
        self.starts += 1
        self.order.append(f"start:{self.starts}")
        if self.fail_second_start and self.starts == 2:
            raise ExpectedFailure([Diagnostic("rojo.failed", "start failed", Phase.ROJO)])
        if self.fail_process and self.starts == 1:
            self.failure.set_exception(
                ExpectedFailure([Diagnostic("rojo.failed", "exited", Phase.ROJO)])
            )

    async def stop(self) -> None:
        self.order.append("stop")

    async def close(self) -> None:
        self.order.append("close")


@pytest.fixture(autouse=True)
def reset_fake() -> None:
    FakeSupervisor.instances.clear()
    FakeSupervisor.fail_second_start = False
    FakeSupervisor.fail_process = False


def run_session(root: Path, monkeypatch, fake_watch) -> tuple[ExpectedFailure, str, FakeSupervisor]:
    monkeypatch.setattr(dev, "RojoSupervisor", FakeSupervisor)
    monkeypatch.setattr(dev, "check_rojo_version", AsyncMock(return_value="7.6.1"))

    async def confirming_watch(paths, callback, *, ready=None):
        replacement = await callback(set())
        if ready is not None:
            ready.set()
        await asyncio.sleep(0.05)
        await fake_watch(replacement or paths, callback)

    monkeypatch.setattr(dev, "watch_structural_changes", confirming_watch)
    output = Console(record=True, width=160)
    with pytest.raises(ExpectedFailure) as captured:
        asyncio.run(dev._run_dev(root, "Main", output))
    return captured.value, output.export_text(), FakeSupervisor.instances[0]


def test_atomic_write_and_unchanged_bytes(tmp_path: Path) -> None:
    setup_project(tmp_path)
    workspace = load_workspace(tmp_path)
    candidate = candidate_for(workspace, "Main")
    assert watcher.write_candidate(workspace.config, candidate)
    timestamp = (tmp_path / watcher.MANIFEST_NAME).stat().st_mtime_ns
    assert not watcher.write_candidate(workspace.config, candidate)
    assert (tmp_path / watcher.MANIFEST_NAME).stat().st_mtime_ns == timestamp
    assert not list(tmp_path.glob(".rojo-mapper-*.tmp"))


def test_atomic_replace_failure_is_structured(tmp_path: Path, monkeypatch) -> None:
    setup_project(tmp_path)
    workspace = load_workspace(tmp_path)
    candidate = candidate_for(workspace, "Main")

    def fail_replace(_source, _destination):
        raise OSError("denied")

    monkeypatch.setattr(watcher.os, "replace", fail_replace)
    with pytest.raises(ExpectedFailure) as captured:
        watcher.write_candidate(workspace.config, candidate)
    assert captured.value.diagnostics[0].kind == "filesystem.write_failed"
    assert not list(tmp_path.glob(".rojo-mapper-*.tmp"))


def test_tree_only_change_keeps_rojo_process(tmp_path: Path, monkeypatch) -> None:
    setup_project(tmp_path)

    async def fake_watch(_paths, callback):
        added = tmp_path / "Source" / "Places" / "Main" / "Server" / "Added.luau"
        added.write_text("return {}", encoding="utf-8")
        await callback(set())
        raise RuntimeError("watch ended")

    error, output, supervisor = run_session(tmp_path, monkeypatch, fake_watch)
    assert error.diagnostics[0].kind == "watcher.failed"
    assert supervisor.order == ["start:1", "close"]
    assert "Updated tree without restarting Rojo" in output
    assert "Added" in (tmp_path / watcher.MANIFEST_NAME).read_text(encoding="utf-8")


def test_invalid_snapshot_is_recoverable(tmp_path: Path, monkeypatch) -> None:
    setup_project(tmp_path)

    async def fake_watch(_paths, callback):
        before = (tmp_path / watcher.MANIFEST_NAME).read_bytes()
        (tmp_path / "rojo-mapper.toml").write_text("invalid = [", encoding="utf-8")
        replacement = await callback(set())
        assert replacement is None
        assert (tmp_path / watcher.MANIFEST_NAME).read_bytes() == before
        raise RuntimeError("watch ended")

    error, output, supervisor = run_session(tmp_path, monkeypatch, fake_watch)
    assert error.diagnostics[0].kind == "watcher.failed"
    assert supervisor.order == ["start:1", "close"]
    assert "config.invalid_toml" in output


def test_metadata_restart_stops_before_commit_and_warns(tmp_path: Path, monkeypatch) -> None:
    setup_project(tmp_path, 100)
    writes: list[tuple[str, int | None]] = []
    real_write = watcher.write_candidate

    def recording_write(config, candidate):
        current = None
        manifest = tmp_path / watcher.MANIFEST_NAME
        if manifest.exists():
            current = (
                __import__("json")
                .loads(manifest.read_text(encoding="utf-8"))
                .get("servePlaceIds", [None])[0]
            )
        writes.append(("write", current))
        return real_write(config, candidate)

    monkeypatch.setattr(dev, "write_candidate", recording_write)

    async def fake_watch(_paths, callback):
        (tmp_path / "rojo-mapper.toml").write_text(
            "schema = 1\n[cloud]\nuniverse_id = 1\n[cloud.places]\nMain = 200\n",
            encoding="utf-8",
        )
        await callback(set())
        raise RuntimeError("watch ended")

    error, output, supervisor = run_session(tmp_path, monkeypatch, fake_watch)
    assert error.diagnostics[0].kind == "watcher.failed"
    assert supervisor.order == ["start:1", "stop", "start:2", "close"]
    assert writes == [("write", None), ("write", 100), ("write", 100)]
    assert "reconnect_required" in output
    assert "automatic reconnection is not expected" in output
    manifest = __import__("json").loads(
        (tmp_path / watcher.MANIFEST_NAME).read_text(encoding="utf-8")
    )
    assert manifest["servePlaceIds"] == [200]


def test_restart_start_failure_is_fatal_and_closes(tmp_path: Path, monkeypatch) -> None:
    setup_project(tmp_path, 100)
    FakeSupervisor.fail_second_start = True

    async def fake_watch(_paths, callback):
        (tmp_path / "rojo-mapper.toml").write_text(
            "schema = 1\n[cloud]\nuniverse_id = 1\n[cloud.places]\nMain = 200\n",
            encoding="utf-8",
        )
        await callback(set())

    error, _, supervisor = run_session(tmp_path, monkeypatch, fake_watch)
    assert error.diagnostics[0].kind == "dev.rojo_restart_failed"
    assert supervisor.order == ["start:1", "stop", "start:2", "close", "close"]


def test_restart_commit_failure_is_fatal_after_stop(tmp_path: Path, monkeypatch) -> None:
    setup_project(tmp_path, 100)
    calls = 0
    real_write = watcher.write_candidate

    def fail_third_write(config, candidate):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise ExpectedFailure(
                [Diagnostic("filesystem.write_failed", "denied", Phase.FILESYSTEM)]
            )
        return real_write(config, candidate)

    monkeypatch.setattr(dev, "write_candidate", fail_third_write)

    async def fake_watch(_paths, callback):
        (tmp_path / "rojo-mapper.toml").write_text(
            "schema = 1\n[cloud]\nuniverse_id = 1\n[cloud.places]\nMain = 200\n",
            encoding="utf-8",
        )
        await callback(set())

    error, _, supervisor = run_session(tmp_path, monkeypatch, fake_watch)
    assert error.diagnostics[0].kind == "dev.rojo_restart_failed"
    assert supervisor.order == ["start:1", "stop", "close", "close"]


def test_unexpected_rojo_exit_is_fatal(tmp_path: Path, monkeypatch) -> None:
    setup_project(tmp_path)
    FakeSupervisor.fail_process = True

    async def fake_watch(_paths, _callback):
        await asyncio.Future()

    error, _, supervisor = run_session(tmp_path, monkeypatch, fake_watch)
    assert error.diagnostics[0].kind == "rojo.failed"
    assert supervisor.order == ["start:1", "close"]


def test_watcher_cancellation_is_fatal(tmp_path: Path, monkeypatch) -> None:
    setup_project(tmp_path)

    async def fake_watch(_paths, _callback):
        raise asyncio.CancelledError

    error, _, _ = run_session(tmp_path, monkeypatch, fake_watch)
    assert error.diagnostics[0].kind == "watcher.failed"


def test_watcher_startup_failure_cancels_ready_wait(tmp_path: Path, monkeypatch) -> None:
    setup_project(tmp_path)
    monkeypatch.setattr(dev, "RojoSupervisor", FakeSupervisor)
    monkeypatch.setattr(dev, "check_rojo_version", AsyncMock(return_value="7.6.1"))

    async def fail_watch(_paths, _callback, *, ready=None):
        raise RuntimeError("startup failed")

    monkeypatch.setattr(dev, "watch_structural_changes", fail_watch)
    with pytest.raises(ExpectedFailure) as captured:
        asyncio.run(dev._run_dev(tmp_path, "Main", Console(record=True)))
    assert captured.value.diagnostics[0].kind == "watcher.failed"
    assert FakeSupervisor.instances[0].order == ["start:1", "close"]


def test_run_dev_cancellation_finishes_cleanup(tmp_path: Path, monkeypatch) -> None:
    setup_project(tmp_path)
    monkeypatch.setattr(dev, "RojoSupervisor", FakeSupervisor)
    monkeypatch.setattr(dev, "check_rojo_version", AsyncMock(return_value="7.6.1"))

    async def never_watch(_paths, _callback, *, ready=None):
        if ready is not None:
            ready.set()
        await asyncio.Future()

    monkeypatch.setattr(dev, "watch_structural_changes", never_watch)

    async def scenario() -> None:
        task = asyncio.create_task(dev._run_dev(tmp_path, "Main", Console(record=True)))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert FakeSupervisor.instances[0].order == ["start:1", "close"]


def test_clean_watcher_termination_is_fatal() -> None:
    async def scenario() -> None:
        async def complete() -> None:
            return

        task = asyncio.create_task(complete())
        await task
        with pytest.raises(ExpectedFailure) as captured:
            dev._raise_watch_failure(task)
        assert captured.value.diagnostics[0].kind == "watcher.failed"

    asyncio.run(scenario())


def test_watched_paths_reconfigure_static_roots(tmp_path: Path) -> None:
    setup_project(tmp_path)
    (tmp_path / "Packages").mkdir()
    (tmp_path / "rojo-mapper.toml").write_text(
        'schema = 1\n[static]\nPackages = "ReplicatedStorage.Packages"\n',
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    assert watcher.watched_paths(config) == tuple(
        sorted(
            (
                tmp_path / "Packages",
                tmp_path / "Source",
                tmp_path / "rojo-mapper.toml",
            ),
            key=lambda path: path.as_posix(),
        )
    )


def test_rojo_version_contract(monkeypatch) -> None:
    class Process:
        returncode = 0

        async def communicate(self):
            return (self.output, b"")

    async def run(version: str):
        process = Process()
        process.output = f"Rojo {version}".encode()
        monkeypatch.setattr(dev.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
        return await dev.check_rojo_version()

    assert asyncio.run(run("7.6.1")) == "7.6.1"
    assert asyncio.run(run("7.7.1")) == "7.7.1"
    with pytest.raises(ExpectedFailure) as captured:
        asyncio.run(run("7.7.0"))
    assert captured.value.diagnostics[0].kind == "rojo.unsupported_version"


def test_live_watchfiles_backend_observes_source_addition(tmp_path: Path) -> None:
    setup_project(tmp_path)

    async def scenario() -> None:
        config = load_config(tmp_path)
        observed = asyncio.Event()

        async def callback(changes):
            if any(path.endswith("Added.luau") for _change, path in changes):
                observed.set()
            return None

        task = asyncio.create_task(
            watcher.watch_structural_changes(watcher.watched_paths(config), callback)
        )
        await asyncio.sleep(0.3)
        added = tmp_path / "Source" / "Places" / "Main" / "Server" / "Added.luau"
        added.write_text("return {}", encoding="utf-8")
        await asyncio.wait_for(observed.wait(), timeout=5)
        await watcher.cancel_task(task)

    asyncio.run(scenario())


def test_main_loop_rojo_failure_after_ready(tmp_path: Path, monkeypatch) -> None:
    setup_project(tmp_path)
    monkeypatch.setattr(dev, "RojoSupervisor", FakeSupervisor)
    monkeypatch.setattr(dev, "check_rojo_version", AsyncMock(return_value="7.6.1"))

    async def steady_watch(_paths, callback, *, ready=None):
        replacement = await callback(set())
        assert replacement is None or replacement is not None
        if ready is not None:
            ready.set()
        await asyncio.sleep(0.05)
        supervisor = FakeSupervisor.instances[0]
        supervisor.failure.set_exception(
            ExpectedFailure([Diagnostic("rojo.failed", "late exit", Phase.ROJO)])
        )
        await asyncio.Future()

    monkeypatch.setattr(dev, "watch_structural_changes", steady_watch)
    with pytest.raises(ExpectedFailure) as captured:
        asyncio.run(dev._run_dev(tmp_path, "Main", Console(record=True)))
    assert captured.value.diagnostics[0].kind == "rojo.failed"


def test_startup_cancel_abandons_ready_wait(tmp_path: Path, monkeypatch) -> None:
    setup_project(tmp_path)
    monkeypatch.setattr(dev, "RojoSupervisor", FakeSupervisor)
    monkeypatch.setattr(dev, "check_rojo_version", AsyncMock(return_value="7.6.1"))

    async def silent_watch(_paths, _callback, *, ready=None):
        await asyncio.Future()

    monkeypatch.setattr(dev, "watch_structural_changes", silent_watch)

    async def scenario() -> None:
        task = asyncio.create_task(dev._run_dev(tmp_path, "Main", Console(record=True)))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert FakeSupervisor.instances[0].order == ["start:1", "close"]


def test_cleanup_cancel_still_closes_supervisor(tmp_path: Path, monkeypatch) -> None:
    setup_project(tmp_path)
    monkeypatch.setattr(dev, "RojoSupervisor", FakeSupervisor)
    monkeypatch.setattr(dev, "check_rojo_version", AsyncMock(return_value="7.6.1"))
    entered_cleanup = asyncio.Event()
    real_cleanup = dev._cleanup_dev

    async def slow_cleanup(watch_task, supervisor) -> None:
        entered_cleanup.set()
        await asyncio.sleep(0.5)
        await real_cleanup(watch_task, supervisor)

    monkeypatch.setattr(dev, "_cleanup_dev", slow_cleanup)

    async def steady_watch(_paths, _callback, *, ready=None):
        if ready is not None:
            ready.set()
        await asyncio.Future()

    monkeypatch.setattr(dev, "watch_structural_changes", steady_watch)

    async def driver() -> None:
        outer = asyncio.create_task(dev._run_dev(tmp_path, "Main", Console(record=True)))
        await asyncio.sleep(0.2)
        outer.cancel()
        await asyncio.wait_for(entered_cleanup.wait(), timeout=5)
        outer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await outer

    asyncio.run(driver())
    assert FakeSupervisor.instances[0].order == ["start:1", "close"]
