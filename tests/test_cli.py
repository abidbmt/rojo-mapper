import importlib
import json
import os
import runpy
import sys
import time
from pathlib import Path

from typer.testing import CliRunner

import rojo_mapper.cli as cli
from rojo_mapper.cli import app
from rojo_mapper.diagnostics import Diagnostic, ExpectedFailure, Phase

runner = CliRunner()


def setup_project(root: Path, places: tuple[str, ...] = ()) -> None:
    (root / "Source" / "Core" / "Shared").mkdir(parents=True)
    (root / "Source" / "Core" / "Shared" / "A.luau").write_text("return {}", encoding="utf-8")
    for place in places:
        path = root / "Source" / "Places" / place / "Server"
        path.mkdir(parents=True)
        (path / "Main.server.luau").write_text("print('ok')", encoding="utf-8")
    (root / "rojo-mapper.toml").write_text("schema = 1\n", encoding="utf-8")


def invoke(root: Path, arguments: list[str]):
    previous = Path.cwd()
    os.chdir(root)
    try:
        return runner.invoke(app, arguments)
    finally:
        os.chdir(previous)


def parse(result) -> dict:
    return json.loads(result.stdout)


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "0.1.1"


def test_generate_infers_exactly_one_place(tmp_path: Path) -> None:
    setup_project(tmp_path, ("Main",))
    result = invoke(tmp_path, ["generate", "--format", "json"])
    assert result.exit_code == 0
    payload = parse(result)
    assert payload == {
        "success": True,
        "command": "generate",
        "target": "Main",
        "changed": True,
        "manifest": "default.project.json",
        "diagnostics": [],
    }
    manifest = json.loads((tmp_path / "default.project.json").read_text(encoding="utf-8"))
    assert manifest["name"].endswith(" - Main")


def test_generate_requires_target_for_zero_or_multiple_places(tmp_path: Path) -> None:
    setup_project(tmp_path)
    result = invoke(tmp_path, ["generate", "--format", "json"])
    assert result.exit_code == 1
    assert parse(result)["diagnostics"][0]["kind"] == "target.required"
    assert result.stderr == ""

    other = tmp_path / "multiple"
    other.mkdir()
    setup_project(other, ("Main", "Place2"))
    result = invoke(other, ["generate", "--format", "json"])
    assert result.exit_code == 1
    assert parse(result)["diagnostics"][0]["details"]["available"] == [
        "Common",
        "Main",
        "Place2",
    ]


def test_generate_unknown_target_and_typer_misuse(tmp_path: Path) -> None:
    setup_project(tmp_path, ("Main",))
    unknown = invoke(tmp_path, ["generate", "main", "--format", "json"])
    assert unknown.exit_code == 1
    assert parse(unknown)["diagnostics"][0]["kind"] == "target.unknown"
    misuse = invoke(tmp_path, ["generate", "Main", "extra"])
    assert misuse.exit_code == 2


def test_validate_all_targets_without_write(tmp_path: Path) -> None:
    setup_project(tmp_path, ("Place2", "Main"))
    result = invoke(tmp_path, ["validate", "--format", "json"])
    assert result.exit_code == 0
    assert parse(result)["targets"] == ["Common", "Main", "Place2"]
    assert not (tmp_path / "default.project.json").exists()


def test_list_is_sorted_reports_owners_and_cloud(tmp_path: Path) -> None:
    setup_project(tmp_path, ("Place2", "Main"))
    (tmp_path / "rojo-mapper.toml").write_text(
        "schema = 1\n[cloud]\nuniverse_id = 9\n[cloud.places]\nCommon = 100\nMain = 111\nPlace2 = 222\n",
        encoding="utf-8",
    )
    result = invoke(tmp_path, ["list", "--format", "json"])
    assert result.exit_code == 0
    payload = parse(result)
    assert payload["targets"] == [
        {"name": "Common", "cloud_place_id": 100},
        {"name": "Main", "cloud_place_id": 111},
        {"name": "Place2", "cloud_place_id": 222},
    ]
    assert payload["manifest"] == {"path": "default.project.json", "owner": "rojo-mapper"}
    assert payload["sourcemap"] == {"path": "sourcemap.json", "owner": "Luau-LSP/Rojo"}
    assert not (tmp_path / "default.project.json").exists()


def test_generate_write_if_changed_preserves_timestamp(tmp_path: Path) -> None:
    setup_project(tmp_path, ("Main",))
    first = invoke(tmp_path, ["generate", "Main", "--format", "json"])
    assert first.exit_code == 0
    manifest = tmp_path / "default.project.json"
    timestamp = manifest.stat().st_mtime_ns
    time.sleep(0.02)
    second = invoke(tmp_path, ["generate", "Main", "--format", "json"])
    assert second.exit_code == 0
    assert parse(second)["changed"] is False
    assert manifest.stat().st_mtime_ns == timestamp


def test_validate_failure_is_deterministic_and_does_not_write(tmp_path: Path) -> None:
    setup_project(tmp_path)
    bad = tmp_path / "Source" / "Core" / "Missing.luau"
    bad.write_text("return {}", encoding="utf-8")
    result = invoke(tmp_path, ["validate", "--format", "json"])
    assert result.exit_code == 1
    assert parse(result)["diagnostics"][0]["kind"] == "source.missing_context"
    assert result.stderr == ""
    assert not (tmp_path / "default.project.json").exists()


def test_human_success_and_failure_output(tmp_path: Path) -> None:
    setup_project(tmp_path, ("Main",))
    generated = invoke(tmp_path, ["generate", "Main"])
    assert generated.exit_code == 0
    assert "Generated Main" in generated.stdout
    validated = invoke(tmp_path, ["validate"])
    assert validated.exit_code == 0
    assert "Valid" in validated.stdout
    listed = invoke(tmp_path, ["list"])
    assert listed.exit_code == 0
    assert "Targets" in listed.stdout
    assert "Sourcemap" in listed.stdout

    empty = tmp_path / "empty"
    empty.mkdir()
    setup_project(empty)
    failed = invoke(empty, ["generate"])
    assert failed.exit_code == 1
    assert "target.required" in failed.stdout


def test_internal_failure_json_and_debug_human(tmp_path: Path, monkeypatch) -> None:
    setup_project(tmp_path)

    def explode(_root: Path) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "load_workspace", explode)
    result = invoke(tmp_path, ["list", "--format", "json"])
    assert result.exit_code == 1
    assert parse(result)["diagnostics"][0]["kind"] == "internal.error"
    for command in ("generate", "validate"):
        command_result = invoke(tmp_path, [command, "--format", "json"])
        assert command_result.exit_code == 1
        assert parse(command_result)["diagnostics"][0]["kind"] == "internal.error"
    debug_json = invoke(tmp_path, ["--debug", "list", "--format", "json"])
    assert debug_json.exit_code == 1
    debug_payload = parse(debug_json)
    assert "Traceback" in debug_payload["debug"]["traceback"]
    assert debug_payload["debug"]["cwd"] == str(tmp_path)
    debug = invoke(tmp_path, ["--debug", "list"])
    assert debug.exit_code == 1
    assert "Traceback" in debug.stdout

    def expected_failure(_root: Path) -> None:
        raise ExpectedFailure([Diagnostic("config.missing", "missing", Phase.CONFIG)])

    monkeypatch.setattr(cli, "load_workspace", expected_failure)
    expected_list = invoke(tmp_path, ["list", "--format", "json"])
    assert expected_list.exit_code == 1
    assert parse(expected_list)["diagnostics"][0]["kind"] == "config.missing"


def test_dev_command_success_and_failures(tmp_path: Path, monkeypatch) -> None:
    setup_project(tmp_path)
    monkeypatch.setattr(cli, "run_dev", lambda _root, _target, _console: "Main")
    success = invoke(tmp_path, ["dev", "Main"])
    assert success.exit_code == 0
    assert "Stopped Main" in success.stdout
    monkeypatch.setattr(cli, "run_dev", lambda _root, _target, _console: "")
    quiet = invoke(tmp_path, ["dev", "Main"])
    assert quiet.exit_code == 0
    assert "Stopped" not in quiet.stdout

    def expected(_root, _target, _console):
        raise ExpectedFailure([Diagnostic("rojo.failed", "no rojo", Phase.ROJO)])

    monkeypatch.setattr(cli, "run_dev", expected)
    failure = invoke(tmp_path, ["dev", "Main"])
    assert failure.exit_code == 1
    assert "rojo.failed" in failure.stdout

    def unexpected(_root, _target, _console):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(cli, "run_dev", unexpected)
    internal = invoke(tmp_path, ["--debug", "dev", "Main"])
    assert internal.exit_code == 1
    assert "internal.error" in internal.stdout
    assert "Traceback" in internal.stdout


def test_main_invokes_typer_app(monkeypatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(cli, "app", lambda **kwargs: called.append(kwargs["prog_name"]))
    cli.main()
    assert called == ["rojo-mapper"]


def test_module_entrypoint_guard(monkeypatch) -> None:
    called: list[bool] = []
    monkeypatch.setattr(cli, "main", lambda: called.append(True))
    importlib.import_module("rojo_mapper.__main__")
    assert called == []
    sys.modules.pop("rojo_mapper.__main__", None)
    runpy.run_module("rojo_mapper.__main__", run_name="__main__")
    assert called == [True]
