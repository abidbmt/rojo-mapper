import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.request import urlopen

import psutil
import pytest

pytestmark = pytest.mark.integration
PORT = 34872


def wait_until(predicate, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition did not become true before timeout")


def port_open() -> bool:
    with socket.socket() as connection:
        connection.settimeout(0.1)
        return connection.connect_ex(("127.0.0.1", PORT)) == 0


def manifest_place_id(root: Path) -> int | None:
    path = root / "default.project.json"
    if not path.exists():
        return -1
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return -1
    values = data.get("servePlaceIds")
    return values[0] if values else None


def server_place_ids() -> tuple[int, ...] | None:
    try:
        with urlopen(f"http://127.0.0.1:{PORT}/api/rojo", timeout=0.5) as response:
            data = json.load(response)
    except OSError:
        return None
    return tuple(data.get("expectedPlaceIds") or ())


def rojo_child(parent_pid: int) -> psutil.Process | None:
    try:
        children = psutil.Process(parent_pid).children(recursive=True)
    except psutil.Error:
        return None
    return next((child for child in children if "rojo" in child.name().casefold()), None)


def stop_tree(process: subprocess.Popen[str]) -> None:
    try:
        root = psutil.Process(process.pid)
        tree = [*root.children(recursive=True), root]
    except psutil.Error:
        tree = []
    if process.poll() is None:
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(process.pid, signal.SIGINT)
            process.wait(timeout=2)
        except OSError, subprocess.TimeoutExpired:
            pass
    for item in tree:
        try:
            if item.is_running():
                item.kill()
        except psutil.Error:
            pass
    if process.poll() is None:
        process.wait(timeout=5)
    wait_until(lambda: not port_open(), timeout=10)


def configure(root: Path, target: str, place_id: int | None) -> None:
    text = "schema = 1\n"
    if place_id is not None:
        text += f"[cloud]\nuniverse_id = 1\n[cloud.places]\n{target} = {place_id}\n"
    (root / "rojo-mapper.toml").write_text(text, encoding="utf-8")


@pytest.mark.parametrize(("target", "has_place"), [("Main", True), ("Common", False)])
def test_live_dev_restart_order_and_cleanup(tmp_path: Path, target: str, has_place: bool) -> None:
    if shutil.which("rojo") is None:
        pytest.skip("locked Rojo executable is unavailable")
    if port_open():
        pytest.skip("default Rojo port is already in use")
    if has_place:
        source = tmp_path / "Source" / "Places" / "Main" / "Server"
    else:
        source = tmp_path / "Source" / "Core" / "Server"
    source.mkdir(parents=True)
    (source / "Initial.luau").write_text("return {}", encoding="utf-8")
    configure(tmp_path, target, 100)
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        [sys.executable, "-m", "rojo_mapper", "dev", target],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
        creationflags=creationflags,
        start_new_session=os.name != "nt",
    )
    output: list[str] = []

    def collect() -> None:
        assert process.stdout is not None
        output.extend(process.stdout)

    reader = threading.Thread(target=collect, daemon=True)
    reader.start()
    try:
        wait_until(
            lambda: (
                process.poll() is not None
                or (
                    port_open()
                    and manifest_place_id(tmp_path) == 100
                    and "Watching structural paths" in "".join(output)
                )
            )
        )
        assert process.poll() is None, "".join(output)
        old_child = rojo_child(process.pid)
        assert old_child is not None
        old_pid = old_child.pid
        assert server_place_ids() == (100,)

        if target == "Main":
            (source / "Added.luau").write_text("return {}", encoding="utf-8")
            wait_until(
                lambda: "Added" in (tmp_path / "default.project.json").read_text(encoding="utf-8")
            )
            assert rojo_child(process.pid).pid == old_pid

            before = (tmp_path / "default.project.json").read_bytes()
            (tmp_path / "rojo-mapper.toml").write_text("invalid = [", encoding="utf-8")
            wait_until(lambda: "config.invalid_toml" in "".join(output))
            assert process.poll() is None
            assert (tmp_path / "default.project.json").read_bytes() == before

        configure(tmp_path, target, 200)
        observed_unsafe_pair = False
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and manifest_place_id(tmp_path) != 200:
            if psutil.pid_exists(old_pid) and manifest_place_id(tmp_path) == 200:
                observed_unsafe_pair = True
            time.sleep(0.005)
        assert manifest_place_id(tmp_path) == 200
        assert not observed_unsafe_pair
        wait_until(lambda: (child := rojo_child(process.pid)) is not None and child.pid != old_pid)
        wait_until(lambda: port_open())
        wait_until(lambda: server_place_ids() == (200,))
        wait_until(lambda: "dev.rojo_restarted_reconnect_required" in "".join(output))
        assert "automatic reconnection is not expected" in "".join(output)

        if target == "Common":
            second_pid = rojo_child(process.pid).pid
            time.sleep(0.3)
            configure(tmp_path, target, None)
            wait_until(lambda: manifest_place_id(tmp_path) is None)
            wait_until(
                lambda: (child := rojo_child(process.pid)) is not None and child.pid != second_pid
            )
            wait_until(lambda: server_place_ids() == ())
    finally:
        stop_tree(process)
        reader.join(timeout=2)
    assert process.poll() is not None
    assert rojo_child(process.pid) is None
