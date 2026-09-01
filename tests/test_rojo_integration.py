import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from rojo_mapper.service import candidate_for, load_workspace
from rojo_mapper.watcher import write_candidate

pytestmark = pytest.mark.integration


def require_rojo() -> str:
    executable = shutil.which("rojo")
    if executable is None:
        pytest.skip("locked Rojo executable is unavailable")
    return executable


def wait_until(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition did not become true before timeout")


def create_static_project(root: Path) -> None:
    dynamic = root / "Source" / "Core" / "Shared"
    dynamic.mkdir(parents=True)
    (dynamic / "Dynamic.luau").write_text("return {}", encoding="utf-8")
    packages = root / "Packages"
    packages.mkdir()
    (packages / "Keep.luau").write_text("return {}", encoding="utf-8")
    (packages / "Drop.spec.luau").write_text("return {}", encoding="utf-8")
    (root / "rojo-mapper.toml").write_text(
        'schema = 1\nignore = ["Packages/**/*.spec.luau"]\n[static]\nPackages = "ReplicatedStorage.Packages"\n',
        encoding="utf-8",
    )


def test_rojo_build_obeys_static_ignore_parity(tmp_path: Path) -> None:
    rojo = require_rojo()
    create_static_project(tmp_path)
    workspace = load_workspace(tmp_path)
    write_candidate(workspace.config, candidate_for(workspace, "Common"))
    output = tmp_path / "built.rbxlx"
    completed = subprocess.run(
        [rojo, "build", "default.project.json", "-o", str(output)],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    built = output.read_text(encoding="utf-8")
    assert "Keep" in built
    assert "Dynamic" in built
    assert "Drop" not in built


def test_delegated_sourcemap_watch_observes_atomic_manifest(tmp_path: Path) -> None:
    rojo = require_rojo()
    source = tmp_path / "Source" / "Core" / "Shared"
    source.mkdir(parents=True)
    (source / "Before.luau").write_text("return {}", encoding="utf-8")
    (tmp_path / "rojo-mapper.toml").write_text("schema = 1\n", encoding="utf-8")
    workspace = load_workspace(tmp_path)
    write_candidate(workspace.config, candidate_for(workspace, "Common"))
    sourcemap = tmp_path / "sourcemap.json"
    process = subprocess.Popen(
        [
            rojo,
            "sourcemap",
            "default.project.json",
            "--output",
            "sourcemap.json",
            "--include-non-scripts",
            "--watch",
        ],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        wait_until(lambda: sourcemap.exists() and "Before" in sourcemap.read_text(encoding="utf-8"))
        (source / "After.luau").write_text("return {}", encoding="utf-8")
        next_workspace = load_workspace(tmp_path)
        assert write_candidate(next_workspace.config, candidate_for(next_workspace, "Common"))
        wait_until(lambda: "After" in sourcemap.read_text(encoding="utf-8"))
        stable = sourcemap.stat().st_mtime_ns
        time.sleep(0.4)
        assert sourcemap.stat().st_mtime_ns == stable
        data = json.loads(sourcemap.read_text(encoding="utf-8"))
        assert data["name"] == tmp_path.name + " - Common"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
