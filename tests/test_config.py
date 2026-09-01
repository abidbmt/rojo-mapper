from pathlib import Path

import pytest

import rojo_mapper.config as config_module
from rojo_mapper.config import load_config, validate_cloud_places
from rojo_mapper.diagnostics import ExpectedFailure
from rojo_mapper.portable import (
    PathProblem,
    PortableGlob,
    contained_directory,
    normalize_relative,
    validate_segment,
)


def write_config(root: Path, text: str) -> None:
    (root / "rojo-mapper.toml").write_text(text, encoding="utf-8")


def kinds(error: ExpectedFailure) -> set[str]:
    return {diagnostic.kind for diagnostic in error.diagnostics}


def test_minimal_config(tmp_path: Path) -> None:
    write_config(tmp_path, "schema = 1\n")
    config = load_config(tmp_path)
    assert config.project_name == tmp_path.name
    assert config.ignore == ()
    assert config.static == ()
    assert config.universe_id is None


def test_config_missing_and_invalid_toml(tmp_path: Path) -> None:
    with pytest.raises(ExpectedFailure) as missing:
        load_config(tmp_path)
    assert kinds(missing.value) == {"config.missing"}
    write_config(tmp_path, "not valid = [")
    with pytest.raises(ExpectedFailure) as invalid:
        load_config(tmp_path)
    assert kinds(invalid.value) == {"config.invalid_toml"}


def test_unknown_and_wrong_fields_are_collected(tmp_path: Path) -> None:
    write_config(tmp_path, 'schema = "1"\nunknown = true\n')
    with pytest.raises(ExpectedFailure) as captured:
        load_config(tmp_path)
    assert kinds(captured.value) == {"config.invalid_value", "config.unknown_field"}


@pytest.mark.parametrize(
    "pattern",
    [
        "",
        "!Source/**",
        "/Source/**",
        "Source/**/",
        "Source/?",
        "Source/[ab]",
        "Source\\**",
        "Source/**x",
        "Source//x",
        "../Source",
        "Source/CON",
        "Source/a:b",
    ],
)
def test_unsupported_ignore_patterns(tmp_path: Path, pattern: str) -> None:
    write_config(tmp_path, f"schema = 1\nignore = [{pattern!r}]\n".replace("'", '"'))
    with pytest.raises(ExpectedFailure) as captured:
        load_config(tmp_path)
    assert kinds(captured.value) == {"config.ignore_unsupported"}


def test_glob_star_and_double_star_parity() -> None:
    pattern = PortableGlob.parse("Source/**/*.spec.luau")
    assert pattern.matches("Source/a.spec.luau")
    assert pattern.matches("Source/Game/a.spec.luau")
    assert not pattern.matches("Source/Game/a.luau")
    assert PortableGlob.parse("Source/*/Shared/*.luau").matches("Source/Core/Shared/A.luau")
    assert not PortableGlob.parse("Source/*/Shared/*.luau").matches("Source/A/B/Shared/A.luau")


def test_static_and_cloud_config(tmp_path: Path) -> None:
    (tmp_path / "Packages").mkdir()
    write_config(
        tmp_path,
        """schema = 1
ignore = ["Source/**/*.spec.luau"]
[static]
Packages = "ReplicatedStorage.Packages"
[cloud]
universe_id = 99
[cloud.places]
Common = 100
Main = 101
""",
    )
    config = load_config(tmp_path)
    assert config.static[0].source == "Packages"
    assert config.static[0].target == ("ReplicatedStorage", "Packages")
    assert config.universe_id == 99
    assert config.cloud_places == {"Common": 100, "Main": 101}
    assert validate_cloud_places(config, ("Main",)) == []
    assert validate_cloud_places(config, ("Other",))[0].kind == "config.cloud_place_unknown"


def test_static_source_and_target_must_be_valid(tmp_path: Path) -> None:
    (tmp_path / "Source" / "Places" / "Main" / "Static").mkdir(parents=True)
    write_config(
        tmp_path,
        'schema = 1\n[static]\n"Source/Places/Main/Static" = "ReplicatedStorage..Bad"\nMissing = "ReplicatedStorage.X"\n',
    )
    with pytest.raises(ExpectedFailure) as captured:
        load_config(tmp_path)
    assert kinds(captured.value) == {"config.static_source_invalid"}


def test_casefold_collisions_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "One").mkdir()
    (tmp_path / "Two").mkdir()
    write_config(
        tmp_path,
        'schema = 1\n[static]\nOne = "ReplicatedStorage.Packages"\nTwo = "replicatedstorage.packages"\n[cloud]\nuniverse_id = 1\n[cloud.places]\nMain = 1\nmain = 2\n',
    )
    with pytest.raises(ExpectedFailure) as captured:
        load_config(tmp_path)
    assert kinds(captured.value) == {
        "config.cloud_place_case_collision",
        "config.static_target_case_collision",
    }


@pytest.mark.parametrize("name", ["CON", "aux.txt", "bad.", "bad ", "a:b", "\x01"])
def test_nonportable_segments(name: str) -> None:
    assert validate_segment(name) is not None


def test_config_read_failure_is_structured(tmp_path: Path, monkeypatch) -> None:
    write_config(tmp_path, "schema = 1\n")

    def fail_read(_path: Path, *, encoding: str) -> str:
        raise OSError("unreadable")

    monkeypatch.setattr(Path, "read_text", fail_read)
    with pytest.raises(ExpectedFailure) as captured:
        load_config(tmp_path)
    assert kinds(captured.value) == {"config.read_failed"}


def test_project_name_and_cloud_values_are_validated(tmp_path: Path, monkeypatch) -> None:
    write_config(
        tmp_path,
        'schema = 1\n[cloud]\nuniverse_id = 1\n[cloud.places]\n"bad:name" = -1\n',
    )
    with pytest.raises(ExpectedFailure) as captured:
        load_config(tmp_path)
    assert kinds(captured.value) == {"config.cloud_place_invalid"}

    write_config(tmp_path, "schema = 1\n")
    monkeypatch.setattr(
        config_module,
        "validate_segment",
        lambda _name: PathProblem("bad", "project directory is nonportable"),
    )
    with pytest.raises(ExpectedFailure) as captured:
        load_config(tmp_path)
    assert kinds(captured.value) == {"config.project_name_nonportable"}


def test_static_source_casefold_and_target_validation(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "One").mkdir()
    (tmp_path / "Two").mkdir()
    write_config(
        tmp_path,
        'schema = 1\n[static]\nOne = "ReplicatedStorage.Good"\nTwo = "ReplicatedStorage.Other"\n',
    )
    monkeypatch.setattr(config_module, "portable_key", lambda _value: "same")
    with pytest.raises(ExpectedFailure) as captured:
        load_config(tmp_path)
    assert kinds(captured.value) == {"config.static_source_case_collision"}

    monkeypatch.undo()
    write_config(tmp_path, 'schema = 1\n[static]\nOne = "ReplicatedStorage..Bad"\n')
    with pytest.raises(ExpectedFailure) as captured:
        load_config(tmp_path)
    assert kinds(captured.value) == {"config.static_target_invalid"}


@pytest.mark.parametrize(
    "value",
    ["/absolute", "C:/drive", "../escape", "a//b", "a/./b", "a/CON"],
)
def test_relative_path_rejections(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_relative(value)


def test_contained_directory_rejects_root_and_files(tmp_path: Path) -> None:
    file = tmp_path / "file"
    file.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        contained_directory(tmp_path, ())
    with pytest.raises(ValueError):
        contained_directory(tmp_path, ("file",))


def test_nested_static_sources_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "Packages" / "Nested").mkdir(parents=True)
    write_config(
        tmp_path,
        'schema = 1\n[static]\nPackages = "ReplicatedStorage.Packages"\n'
        '"Packages/Nested" = "ServerStorage.Nested"\n',
    )
    with pytest.raises(ExpectedFailure) as captured:
        load_config(tmp_path)
    assert kinds(captured.value) == {"config.static_source_overlap"}


def test_cloud_and_static_targets_normalize_to_nfc(tmp_path: Path) -> None:
    (tmp_path / "Packages").mkdir()
    write_config(
        tmp_path,
        'schema = 1\n[static]\nPackages = "ReplicatedStorage.Cafe\u0301"\n'
        '[cloud]\nuniverse_id = 1\n[cloud.places]\n"Cafe\u0301" = 2\n',
    )
    config = load_config(tmp_path)
    assert config.static[0].target[-1] == "Café"
    assert config.cloud_places == {"Café": 2}
