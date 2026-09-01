import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rojo_mapper.cli import app
from rojo_mapper.diagnostics import Diagnostic, ExpectedFailure, Phase, sorted_diagnostics
from rojo_mapper.service import load_workspace


def test_diagnostics_sort_by_phase_kind_target_path_sources_message() -> None:
    diagnostics = [
        Diagnostic("target.z", "b", Phase.TARGET, target="B", sources=("z", "a")),
        Diagnostic("config.z", "c", Phase.CONFIG, path="b"),
        Diagnostic("config.a", "a", Phase.CONFIG, path="z"),
    ]
    ordered = sorted_diagnostics(diagnostics)
    assert [item.kind for item in ordered] == ["config.a", "config.z", "target.z"]
    assert ordered[2].to_dict()["sources"] == ["a", "z"]


def test_cloud_config_errors_precede_source_mapping(tmp_path: Path) -> None:
    (tmp_path / "Source" / "Core").mkdir(parents=True)
    (tmp_path / "Source" / "Core" / "Missing.luau").write_text("return {}", encoding="utf-8")
    (tmp_path / "rojo-mapper.toml").write_text(
        "schema = 1\n[cloud]\nuniverse_id = 1\n[cloud.places]\nUnknown = 2\n",
        encoding="utf-8",
    )
    with pytest.raises(ExpectedFailure) as captured:
        load_workspace(tmp_path)
    assert [item.kind for item in captured.value.diagnostics] == ["config.cloud_place_unknown"]


def test_json_expected_failure_is_one_stdout_document(tmp_path: Path) -> None:
    (tmp_path / "Source").mkdir()
    (tmp_path / "rojo-mapper.toml").write_text("unknown = true\n", encoding="utf-8")
    previous = Path.cwd()
    os.chdir(tmp_path)
    try:
        result = CliRunner().invoke(app, ["validate", "--format", "json"])
    finally:
        os.chdir(previous)
    payload = json.loads(result.stdout)
    assert result.exit_code == 1
    assert result.stderr == ""
    assert payload["success"] is False
    assert payload["command"] == "validate"
    assert payload["changed"] is False
    assert payload["diagnostics"][0]["kind"] == "config.invalid_value"
