import os
from pathlib import Path

import pytest

import rojo_mapper.discovery as discovery
from rojo_mapper.config import load_config
from rojo_mapper.discovery import inspect_layout, scan_root
from rojo_mapper.portable import PathProblem


def configure(root: Path, text: str = "schema = 1\n") -> None:
    (root / "rojo-mapper.toml").write_text(text, encoding="utf-8")


def test_missing_source_and_invalid_places(tmp_path: Path) -> None:
    configure(tmp_path)
    config = load_config(tmp_path)
    layout, diagnostics = inspect_layout(config)
    assert layout.places == ()
    assert [item.kind for item in diagnostics] == ["source.missing_root"]

    (tmp_path / "Source").mkdir()
    (tmp_path / "Source" / "Places").write_text("not a directory", encoding="utf-8")
    layout, diagnostics = inspect_layout(config)
    assert layout.places == ()
    assert [item.kind for item in diagnostics] == ["source.places_invalid"]


def test_layout_read_failure(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "Source").mkdir()
    configure(tmp_path)
    config = load_config(tmp_path)

    def fail_scandir(_path):
        raise OSError("denied")

    monkeypatch.setattr(discovery.os, "scandir", fail_scandir)
    layout, diagnostics = inspect_layout(config)
    assert layout.common_roots == ()
    assert [item.kind for item in diagnostics] == ["source.read_failed"]


def test_root_and_place_name_validation(tmp_path: Path, monkeypatch) -> None:
    for path in (
        tmp_path / "Source" / "Core",
        tmp_path / "Source" / "Game",
        tmp_path / "Source" / "Places" / "Main",
    ):
        path.mkdir(parents=True, exist_ok=True)
    configure(tmp_path)
    config = load_config(tmp_path)
    monkeypatch.setattr(discovery, "portable_key", lambda _value: "same")
    layout, diagnostics = inspect_layout(config)
    assert layout.places == ()
    assert {item.kind for item in diagnostics} == {
        "source.root_case_collision",
        "source.place_case_collision",
    }

    monkeypatch.undo()
    monkeypatch.setattr(
        discovery,
        "validate_segment",
        lambda _name: PathProblem("bad", "nonportable"),
    )
    _, diagnostics = inspect_layout(config)
    assert {item.kind for item in diagnostics} == {
        "source.root_nonportable",
        "source.place_nonportable",
    }


def test_scan_read_failure_ignore_and_nonportable_name(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "Source" / "Core" / "Shared"
    source.mkdir(parents=True)
    (source / "A.luau").write_text("return {}", encoding="utf-8")
    configure(tmp_path, 'schema = 1\nignore = ["Source/Core/Shared/A.luau"]\n')
    config = load_config(tmp_path)
    layout, diagnostics = inspect_layout(config)
    assert diagnostics == []
    scan = scan_root(config, layout.common_roots[0])
    assert scan.files == ()

    original = discovery.os.scandir

    def selective_scandir(path):
        if Path(path) == layout.common_roots[0].source_path:
            raise OSError("blocked")
        return original(path)

    monkeypatch.setattr(discovery.os, "scandir", selective_scandir)
    scan = scan_root(config, layout.common_roots[0])
    assert [item.kind for item in scan.diagnostics] == ["source.read_failed"]

    monkeypatch.undo()
    monkeypatch.setattr(
        discovery,
        "validate_segment",
        lambda name: PathProblem("bad", "nonportable") if name == "A.luau" else None,
    )
    config = load_config(tmp_path)
    layout, _ = inspect_layout(config)
    scan = scan_root(config, layout.common_roots[0])
    assert [item.kind for item in scan.diagnostics] == ["source.nonportable_name"]


def test_dynamic_link_is_rejected_when_supported(tmp_path: Path) -> None:
    source = tmp_path / "Source" / "Core"
    source.mkdir(parents=True)
    target = tmp_path / "target"
    target.mkdir()
    configure(tmp_path)
    try:
        os.symlink(target, source / "Linked", target_is_directory=True)
    except OSError:
        pytest.skip("host does not permit symlink creation")
    config = load_config(tmp_path)
    layout, _ = inspect_layout(config)
    scan = scan_root(config, layout.common_roots[0])
    assert [item.kind for item in scan.diagnostics] == ["source.link_unsupported"]


def test_static_source_subtree_is_excluded(tmp_path: Path) -> None:
    static = tmp_path / "Source" / "Core" / "Shared" / "Generated"
    static.mkdir(parents=True)
    (static / "Owned.luau").write_text("return {}", encoding="utf-8")
    configure(
        tmp_path,
        'schema = 1\n[static]\n"Source/Core/Shared/Generated" = "ReplicatedStorage.Generated"\n',
    )
    config = load_config(tmp_path)
    layout, diagnostics = inspect_layout(config)
    assert diagnostics == []
    scan = scan_root(config, layout.common_roots[0])
    assert not any("Owned" in file.project_relative for file in scan.files)


def test_flat_artifacts_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "Source" / "Places").mkdir(parents=True)
    (tmp_path / "Source" / "Flat.luau").write_text("return {}", encoding="utf-8")
    (tmp_path / "Source" / "README.md").write_text("ignored", encoding="utf-8")
    (tmp_path / "Source" / "Places" / "Flat.server.luau").write_text(
        "print('bad')",
        encoding="utf-8",
    )
    configure(tmp_path)
    layout, diagnostics = inspect_layout(load_config(tmp_path))
    assert layout.common_roots == ()
    assert {item.kind for item in diagnostics} == {
        "source.flat_root_unsupported",
        "source.flat_place_unsupported",
    }


def test_static_source_can_own_entire_source_tree(tmp_path: Path) -> None:
    (tmp_path / "Source" / "Core" / "Shared").mkdir(parents=True)
    configure(tmp_path, 'schema = 1\n[static]\nSource = "ReplicatedStorage.Source"\n')
    layout, diagnostics = inspect_layout(load_config(tmp_path))
    assert diagnostics == []
    assert layout.common_roots == ()
    assert layout.places == ()


def test_non_nfc_root_and_file_names_are_rejected(tmp_path: Path) -> None:
    decomposed = "Cafe\u0301"
    (tmp_path / "Source" / decomposed).mkdir(parents=True)
    configure(tmp_path)
    layout, diagnostics = inspect_layout(load_config(tmp_path))
    assert layout.common_roots == ()
    assert [item.kind for item in diagnostics] == ["source.root_non_nfc"]

    root = tmp_path / "other"
    source = root / "Source" / "Core" / "Shared"
    source.mkdir(parents=True)
    (source / f"{decomposed}.luau").write_text("return {}", encoding="utf-8")
    configure(root)
    config = load_config(root)
    layout, diagnostics = inspect_layout(config)
    assert diagnostics == []
    scan = scan_root(config, layout.common_roots[0])
    assert [item.kind for item in scan.diagnostics] == ["source.non_nfc_name"]
