import json
from pathlib import Path

import pytest

import rojo_mapper.mapping as mapping_module
from rojo_mapper.config import load_config
from rojo_mapper.diagnostics import Diagnostic, Phase
from rojo_mapper.discovery import inspect_layout
from rojo_mapper.formats import ArtifactFamily, classify
from rojo_mapper.mapping import (
    MappingEntry,
    MappingResult,
    Ownership,
    _validate_entries,
    build_mapping,
)
from rojo_mapper.portable import PathProblem
from rojo_mapper.project import build_project


def project(tmp_path: Path, config: str = "schema = 1\n") -> Path:
    (tmp_path / "Source").mkdir()
    (tmp_path / "rojo-mapper.toml").write_text(config, encoding="utf-8")
    return tmp_path


def source(root: Path, relative: str, content: str = "return {}") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def mapping(root: Path, target: str = "Common"):
    config = load_config(root)
    layout, layout_diagnostics = inspect_layout(config)
    assert layout_diagnostics == []
    return config, layout, build_mapping(config, layout, target)


def targets(result) -> dict[str, MappingEntry]:
    return {".".join(entry.target): entry for entry in result.entries}


@pytest.mark.parametrize(
    ("filename", "family", "stem", "prefix", "init"),
    [
        ("A.luau", ArtifactFamily.SCRIPT, "A", True, False),
        ("A.server.lua", ArtifactFamily.SCRIPT, "A", True, False),
        ("A.client.luau", ArtifactFamily.SCRIPT, "A", True, False),
        ("A.plugin.luau", ArtifactFamily.SCRIPT, "A", True, False),
        ("init.luau", ArtifactFamily.SCRIPT, "init", True, True),
        ("A.json", ArtifactFamily.DATA, "A", True, False),
        ("A.jsonc", ArtifactFamily.DATA, "A", True, False),
        ("A.toml", ArtifactFamily.DATA, "A", True, False),
        ("A.yml", ArtifactFamily.DATA, "A", True, False),
        ("A.model.json", ArtifactFamily.MODEL, "A", False, False),
        ("A.model.jsonc", ArtifactFamily.MODEL, "A", False, False),
        ("A.rbxm", ArtifactFamily.MODEL, "A", False, False),
        ("A.rbxmx", ArtifactFamily.MODEL, "A", False, False),
        ("A.csv", ArtifactFamily.TEXT, "A", False, False),
        ("A.txt", ArtifactFamily.TEXT, "A", False, False),
        ("A.meta.json", ArtifactFamily.COMPOSITION, "A.meta.json", False, False),
    ],
)
def test_artifact_classification(filename, family, stem, prefix, init) -> None:
    artifact = classify(filename)
    assert artifact is not None
    assert (artifact.family, artifact.logical_stem, artifact.prefix_eligible, artifact.init) == (
        family,
        stem,
        prefix,
        init,
    )


def test_unknown_artifact_is_skipped() -> None:
    assert classify("README.md") is None
    assert classify("image.png") is None


def test_fixed_context_layers_places_and_feature_prefixes(tmp_path: Path) -> None:
    root = project(tmp_path)
    source(root, "Source/Core/Shared/Libraries/Signal.luau")
    source(root, "Source/Game/Libraries/Server/Store.luau")
    source(root, "Source/Game/First/Boot.client.luau")
    source(root, "Source/Game/Features/Combat/Shared/API.luau")
    source(root, "Source/Game/Features/Combat/Shared/ReadAPI.luau")
    source(root, "Source/Game/Features/Combat/Shared/Events.json", "{}")
    source(root, "Source/Game/Features/Combat/Shared/Internal/Codec.luau")
    source(root, "Source/Game/Features/Combat/Shared/Map.rbxm", "binary")
    source(root, "Source/Places/Main/Features/Runtime/Client/System.luau")
    config, layout, common = mapping(root)
    assert layout.places == ("Main",)
    common_targets = targets(common)
    assert "ReplicatedStorage.Shared.Core.Libraries.Signal" in common_targets
    assert "ServerScriptService.Server.Game.Libraries.Store" in common_targets
    assert "ReplicatedFirst.First.Game.Boot" in common_targets
    assert "ReplicatedStorage.Shared.Game.Features.Combat.CombatAPI" in common_targets
    assert "ReplicatedStorage.Shared.Game.Features.Combat.CombatReadAPI" in common_targets
    assert "ReplicatedStorage.Shared.Game.Features.Combat.CombatEvents" in common_targets
    assert "ReplicatedStorage.Shared.Game.Features.Combat.Internal.Codec" in common_targets
    assert "ReplicatedStorage.Shared.Game.Features.Combat.Map" in common_targets
    main = build_mapping(config, layout, "Main")
    assert "ReplicatedStorage.Client.Place.Features.Runtime.RuntimeSystem" in targets(main)


def test_context_errors_and_dynamic_composition(tmp_path: Path) -> None:
    root = project(tmp_path)
    source(root, "Source/Game/NoContext.luau")
    source(root, "Source/Game/Shared/Server/Two.luau")
    source(root, "Source/Game/Shared/Thing.meta.json", "{}")
    _, _, result = mapping(root)
    assert {item.kind for item in result.diagnostics} == {
        "source.missing_context",
        "source.multiple_contexts",
        "source.unsupported_composition",
    }


def test_empty_context_directories_are_preserved(tmp_path: Path) -> None:
    root = project(tmp_path)
    (root / "Source" / "Core" / "Shared" / "Empty").mkdir(parents=True)
    _, _, result = mapping(root)
    entry = targets(result)["ReplicatedStorage.Shared.Core.Empty"]
    assert entry.ownership == Ownership.FOLDER
    assert not any(item.kind == "source.missing_context" for item in result.diagnostics)


def test_safe_init_is_opaque_and_nested_init_is_delegated(tmp_path: Path) -> None:
    root = project(tmp_path)
    source(root, "Source/Core/Server/State/init.luau")
    source(root, "Source/Core/Server/State/Util.luau")
    source(root, "Source/Core/Server/State/Internal/Codec/init.luau")
    config, _, result = mapping(root)
    assert result.success
    entry = targets(result)["ServerScriptService.Server.Core.State"]
    assert entry == MappingEntry(
        ("ServerScriptService", "Server", "Core", "State"),
        "Source/Core/Server/State",
        Ownership.INIT,
    )
    candidate = build_project(config, result)
    state = candidate.data["tree"]["ServerScriptService"]["Server"]["Core"]["State"]
    assert state == {"$path": "Source/Core/Server/State"}


def test_init_transform_and_multiple_init_are_fatal(tmp_path: Path) -> None:
    root = project(tmp_path)
    source(root, "Source/Game/Features/Combat/Shared/init.luau")
    source(root, "Source/Game/Features/Combat/Shared/API.luau")
    source(root, "Source/Core/Server/State/init.luau")
    source(root, "Source/Core/Server/State/init.server.luau")
    _, _, result = mapping(root)
    assert {item.kind for item in result.diagnostics} == {
        "mapping.init_transform_conflict",
        "mapping.multiple_init",
    }


def test_collisions_are_rejected(tmp_path: Path) -> None:
    root = project(tmp_path)
    source(root, "Source/Core/Shared/A.luau")
    source(root, "Source/Core/Shared/A.json", "{}")
    source(root, "Source/Core/Shared/Node.rbxm", "binary")
    source(root, "Source/Core/Shared/Node/Child.luau")
    _, _, result = mapping(root)
    assert {item.kind for item in result.diagnostics} == {
        "target.duplicate",
        "target.file_ancestor_conflict",
    }
    artificial = [
        MappingEntry(("ReplicatedStorage", "A"), "one", Ownership.SOURCE),
        MappingEntry(("replicatedstorage", "a"), "two", Ownership.SOURCE),
    ]
    assert {item.kind for item in _validate_entries(artificial)} == {"target.case_collision"}


def test_static_mount_is_common_and_sync_safe(tmp_path: Path) -> None:
    (tmp_path / "Packages").mkdir()
    root = project(
        tmp_path,
        'schema = 1\nignore = ["Source/**/*.spec.luau"]\n[static]\nPackages = "ReplicatedStorage.Packages"\n[cloud]\nuniverse_id = 9\n[cloud.places]\nCommon = 100\n',
    )
    source(root, "Source/Core/Shared/A.luau")
    source(root, "Source/Core/Shared/A.spec.luau")
    config, _, result = mapping(root)
    assert result.success
    candidate = build_project(config, result)
    assert candidate.data["servePlaceIds"] == [100]
    assert candidate.data["globIgnorePaths"] == ["Source/**/*.spec.luau"]
    tree = candidate.data["tree"]
    assert tree["$className"] == "DataModel"
    assert tree["ReplicatedStorage"]["Packages"] == {"$path": "Packages"}
    shared = tree["ReplicatedStorage"]["Shared"]
    assert shared["$className"] == "Folder"
    assert shared["$ignoreUnknownInstances"] is False
    assert "A.spec" not in json.dumps(candidate.data)
    assert candidate.encoded.endswith(b"\n")
    assert b"\r" not in candidate.encoded


def test_static_dynamic_and_static_static_overlaps(tmp_path: Path) -> None:
    (tmp_path / "Packages").mkdir()
    (tmp_path / "Assets").mkdir()
    root = project(
        tmp_path,
        'schema = 1\n[static]\nPackages = "ReplicatedStorage.Shared.Core"\nAssets = "ReplicatedStorage.Shared.Core.Assets"\n',
    )
    source(root, "Source/Core/Shared/A.luau")
    _, _, result = mapping(root)
    assert {item.kind for item in result.diagnostics} == {"target.static_overlap"}


def test_manifest_is_deterministic_and_target_specific(tmp_path: Path) -> None:
    root = project(tmp_path, "schema = 1\n[cloud]\nuniverse_id = 1\n[cloud.places]\nMain = 42\n")
    source(root, "Source/Core/Shared/Z.luau")
    source(root, "Source/Core/Shared/A.luau")
    source(root, "Source/Places/Main/Server/OnlyMain.luau")
    config, layout, common = mapping(root)
    common_candidate = build_project(config, common)
    assert "servePlaceIds" not in common_candidate.data
    assert "OnlyMain" not in common_candidate.encoded.decode()
    main_candidate = build_project(config, build_mapping(config, layout, "Main"))
    assert main_candidate.data["name"].endswith(" - Main")
    assert main_candidate.data["servePlaceIds"] == [42]
    assert "OnlyMain" in main_candidate.encoded.decode()
    assert (
        main_candidate.encoded
        == build_project(config, build_mapping(config, layout, "Main")).encoded
    )


def test_init_without_context_and_generated_nonportable_target(tmp_path: Path, monkeypatch) -> None:
    root = project(tmp_path)
    source(root, "Source/Core/State/init.luau")
    source(root, "Source/Core/Shared/A.luau")
    monkeypatch.setattr(
        mapping_module,
        "validate_segment",
        lambda name: PathProblem("bad", "generated name is invalid") if name == "A" else None,
    )
    _, _, result = mapping(root)
    assert {item.kind for item in result.diagnostics} == {
        "source.missing_context",
        "target.nonportable_name",
    }


def test_reserved_service_and_folder_collision_validation() -> None:
    entries = [
        MappingEntry(("$Reserved",), "reserved", Ownership.SOURCE),
        MappingEntry(("Workspace",), "service", Ownership.SOURCE),
        MappingEntry(("ReplicatedStorage", "Shared"), "one", Ownership.FOLDER),
        MappingEntry(("ReplicatedStorage", "Shared"), "two", Ownership.FOLDER),
    ]
    assert {item.kind for item in _validate_entries(entries)} == {
        "target.reserved_name",
        "target.service_ownership",
    }


def test_project_rejects_failed_mapping_and_handles_direct_service() -> None:
    failed = MappingResult(
        "Common",
        (),
        (Diagnostic("mapping.failed", "failed", Phase.MAPPING),),
    )
    with pytest.raises(ValueError, match="failed mapping"):
        build_project(None, failed)

    direct = MappingResult(
        "Common",
        (MappingEntry(("Workspace",), "World", Ownership.STATIC),),
        (),
    )
    config = type(
        "ConfigStub",
        (),
        {
            "project_name": "Example",
            "cloud_places": {},
            "ignore_sources": (),
        },
    )()
    candidate = build_project(config, direct)
    assert candidate.data["tree"]["Workspace"] == {
        "$className": "Workspace",
        "$path": "World",
    }
