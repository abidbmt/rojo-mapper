from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from rojo_mapper.config import Config
from rojo_mapper.diagnostics import Diagnostic, Phase, sorted_diagnostics
from rojo_mapper.discovery import CONTEXT_TARGETS, DiscoveredFile, Layout, LogicalRoot, scan_root
from rojo_mapper.formats import ArtifactFamily
from rojo_mapper.portable import portable_key, validate_segment


class Ownership(StrEnum):
    FOLDER = "folder"
    SOURCE = "source"
    STATIC = "static"
    INIT = "init"


@dataclass(frozen=True, slots=True)
class MappingEntry:
    target: tuple[str, ...]
    source: str
    ownership: Ownership


@dataclass(frozen=True, slots=True)
class MappingResult:
    target: str
    entries: tuple[MappingEntry, ...]
    diagnostics: tuple[Diagnostic, ...]

    @property
    def success(self) -> bool:
        return not self.diagnostics


def build_mapping(config: Config, layout: Layout, target: str) -> MappingResult:
    entries = [
        MappingEntry(mount.target, mount.source, Ownership.STATIC) for mount in config.static
    ]
    diagnostics: list[Diagnostic] = []
    for logical_root in layout.roots_for(target):
        _map_root(config, logical_root, entries, diagnostics)
    diagnostics.extend(_validate_entries(entries))
    if diagnostics:
        return MappingResult(target, (), tuple(sorted_diagnostics(diagnostics)))
    return MappingResult(
        target,
        tuple(sorted(entries, key=lambda item: (item.target, item.ownership, item.source))),
        (),
    )


def _map_root(
    config: Config,
    logical_root: LogicalRoot,
    entries: list[MappingEntry],
    diagnostics: list[Diagnostic],
) -> None:
    scan = scan_root(config, logical_root)
    diagnostics.extend(scan.diagnostics)
    init_groups: dict[tuple[str, ...], list[DiscoveredFile]] = {}
    for file in scan.files:
        if file.artifact.init:
            init_groups.setdefault(file.root_relative[:-1], []).append(file)
        elif file.artifact.family == ArtifactFamily.COMPOSITION:
            diagnostics.append(
                Diagnostic(
                    "source.unsupported_composition",
                    "dynamic Rojo metadata/project composition requires an opaque [static] mount",
                    Phase.SOURCE,
                    path=file.project_relative,
                )
            )

    for parent, init_files in sorted(init_groups.items()):
        if len(init_files) > 1:
            diagnostics.append(
                Diagnostic(
                    "mapping.multiple_init",
                    "an init directory may contain only one recognized init variant",
                    Phase.MAPPING,
                    path=_project_relative_directory(logical_root, parent),
                    sources=tuple(sorted(file.project_relative for file in init_files)),
                )
            )

    init_owners: dict[tuple[str, ...], DiscoveredFile] = {
        parent: files[0] for parent, files in init_groups.items() if len(files) == 1
    }
    invalid_init_owners: set[tuple[str, ...]] = set()
    for parent, init_file in init_owners.items():
        descendants = [
            file
            for file in scan.files
            if file is not init_file
            and (
                file.root_relative[:-1] == parent or _is_descendant(file.root_relative[:-1], parent)
            )
        ]
        transformed = any(_requires_mapper_transform(file) for file in descendants)
        init_path = init_file.path.parent.resolve(strict=False)
        separate_static = any(init_path in mount.source_path.parents for mount in config.static)
        if transformed or separate_static:
            reason = (
                "a descendant requires mapper naming"
                if transformed
                else "a descendant is separately static-owned"
            )
            diagnostics.append(
                Diagnostic(
                    "mapping.init_transform_conflict",
                    f"init directory cannot be opaque because {reason}",
                    Phase.MAPPING,
                    path=_project_relative_directory(logical_root, parent),
                    sources=tuple(sorted(file.project_relative for file in descendants)),
                )
            )
            invalid_init_owners.add(parent)

    for directory in scan.directories:
        owner = _nearest_init_owner(directory.root_relative, init_owners)
        if owner is not None:
            continue
        target_path = _directory_target(
            logical_root, directory.root_relative, directory.project_relative, diagnostics
        )
        if target_path is not None:
            entries.append(MappingEntry(target_path, directory.project_relative, Ownership.FOLDER))

    for file in scan.files:
        if file.artifact.family == ArtifactFamily.COMPOSITION:
            continue
        parent = file.root_relative[:-1]
        nearest_owner = _nearest_init_owner(parent, init_owners)
        if file.artifact.init:
            outer_owner = next(
                (
                    owner
                    for owner in init_owners
                    if owner != parent and _is_descendant(parent, owner)
                ),
                None,
            )
            if outer_owner is not None:
                _validate_context(parent, file.project_relative, diagnostics)
                continue
            if parent in invalid_init_owners or len(init_groups[parent]) != 1:
                continue
            target_path = _init_target(logical_root, parent, file.project_relative, diagnostics)
            if target_path is not None:
                entries.append(
                    MappingEntry(
                        target_path,
                        _project_relative_directory(logical_root, parent),
                        Ownership.INIT,
                    )
                )
            continue
        if nearest_owner is not None:
            _validate_context(parent, file.project_relative, diagnostics)
            continue
        target_path = _file_target(logical_root, file, diagnostics)
        if target_path is not None:
            entries.append(MappingEntry(target_path, file.project_relative, Ownership.SOURCE))


def _directory_target(
    logical_root: LogicalRoot,
    parts: tuple[str, ...],
    path: str,
    diagnostics: list[Diagnostic],
) -> tuple[str, ...] | None:
    indexes = [index for index, part in enumerate(parts) if part in CONTEXT_TARGETS]
    if not indexes:
        return None
    context_index = _validate_context(parts, path, diagnostics)
    if context_index is None:
        return None
    context = parts[context_index]
    remaining = (*parts[:context_index], *parts[context_index + 1 :])
    return (*CONTEXT_TARGETS[context], logical_root.runtime_layer, *remaining)


def _init_target(
    logical_root: LogicalRoot,
    parent: tuple[str, ...],
    path: str,
    diagnostics: list[Diagnostic],
) -> tuple[str, ...] | None:
    context_index = _validate_context(parent, path, diagnostics)
    if context_index is None:
        return None
    context = parent[context_index]
    remaining = (*parent[:context_index], *parent[context_index + 1 :])
    return (*CONTEXT_TARGETS[context], logical_root.runtime_layer, *remaining)


def _file_target(
    logical_root: LogicalRoot,
    file: DiscoveredFile,
    diagnostics: list[Diagnostic],
) -> tuple[str, ...] | None:
    directories = file.root_relative[:-1]
    context_index = _validate_context(directories, file.project_relative, diagnostics)
    if context_index is None:
        return None
    context = directories[context_index]
    remaining = (*directories[:context_index], *directories[context_index + 1 :])
    name = file.artifact.logical_stem
    if _is_exposed_direct_module(directories, context_index, file):
        name = f"{directories[context_index - 1]}{name}"
    problem = validate_segment(name)
    if problem is not None:
        diagnostics.append(
            Diagnostic(
                "target.nonportable_name", problem.message, Phase.TARGET, path=file.project_relative
            )
        )
        return None
    return (*CONTEXT_TARGETS[context], logical_root.runtime_layer, *remaining, name)


def _validate_context(
    directories: tuple[str, ...],
    path: str,
    diagnostics: list[Diagnostic],
) -> int | None:
    indexes = [index for index, part in enumerate(directories) if part in CONTEXT_TARGETS]
    if not indexes:
        diagnostics.append(
            Diagnostic(
                "source.missing_context",
                "recognized dynamic artifact must contain exactly one context segment",
                Phase.SOURCE,
                path=path,
            )
        )
        return None
    if len(indexes) > 1:
        diagnostics.append(
            Diagnostic(
                "source.multiple_contexts",
                "recognized dynamic artifact contains multiple reserved context segments",
                Phase.SOURCE,
                path=path,
            )
        )
        return None
    return indexes[0]


def _is_exposed_direct_module(
    directories: tuple[str, ...],
    context_index: int,
    file: DiscoveredFile,
) -> bool:
    return (
        file.artifact.prefix_eligible
        and context_index == len(directories) - 1
        and context_index >= 2
        and "Features" in directories[: context_index - 1]
    )


def _requires_mapper_transform(file: DiscoveredFile) -> bool:
    directories = file.root_relative[:-1]
    indexes = [index for index, part in enumerate(directories) if part in CONTEXT_TARGETS]
    return len(indexes) == 1 and _is_exposed_direct_module(directories, indexes[0], file)


def _nearest_init_owner(
    parts: tuple[str, ...],
    owners: dict[tuple[str, ...], DiscoveredFile],
) -> tuple[str, ...] | None:
    matches = [owner for owner in owners if parts == owner or _is_descendant(parts, owner)]
    return max(matches, key=len) if matches else None


def _is_descendant(path: tuple[str, ...], ancestor: tuple[str, ...]) -> bool:
    return len(path) > len(ancestor) and path[: len(ancestor)] == ancestor


def _project_relative_directory(logical_root: LogicalRoot, parts: tuple[str, ...]) -> str:
    return "/".join((logical_root.source_relative, *parts))


def _validate_entries(entries: list[MappingEntry]) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    exact: dict[tuple[str, ...], MappingEntry] = {}
    folded: dict[tuple[str, ...], MappingEntry] = {}
    for entry in sorted(entries, key=lambda item: (item.target, item.source)):
        target_text = ".".join(entry.target)
        if any(segment.startswith("$") for segment in entry.target):
            diagnostics.append(
                Diagnostic(
                    "target.reserved_name",
                    "DataModel target segments beginning with $ are reserved by Rojo",
                    Phase.TARGET,
                    target=target_text,
                    sources=(entry.source,),
                )
            )
        if len(entry.target) == 1 and entry.ownership != Ownership.FOLDER:
            diagnostics.append(
                Diagnostic(
                    "target.service_ownership",
                    "a source mount may not replace a DataModel service node",
                    Phase.TARGET,
                    target=target_text,
                    sources=(entry.source,),
                )
            )
        key = tuple(portable_key(segment) for segment in entry.target)
        previous_folded = folded.get(key)
        if previous_folded is not None and previous_folded.target != entry.target:
            diagnostics.append(
                Diagnostic(
                    "target.case_collision",
                    "DataModel targets collide under Unicode case-folding",
                    Phase.TARGET,
                    target=target_text,
                    sources=tuple(sorted((previous_folded.source, entry.source))),
                )
            )
        else:
            folded[key] = entry
        previous = exact.get(entry.target)
        if previous is not None:
            if previous.ownership == Ownership.FOLDER and entry.ownership == Ownership.FOLDER:
                continue
            kind = (
                "target.static_overlap"
                if Ownership.STATIC in {previous.ownership, entry.ownership}
                else "target.duplicate"
            )
            diagnostics.append(
                Diagnostic(
                    kind,
                    "multiple sources own the same DataModel target",
                    Phase.TARGET,
                    target=target_text,
                    sources=tuple(sorted((previous.source, entry.source))),
                )
            )
        else:
            exact[entry.target] = entry

    opaque = [entry for entry in entries if entry.ownership != Ownership.FOLDER]
    for owner in opaque:
        for other in entries:
            if owner is other or not _is_descendant(other.target, owner.target):
                continue
            kind = (
                "target.static_overlap"
                if owner.ownership == Ownership.STATIC or other.ownership == Ownership.STATIC
                else "target.file_ancestor_conflict"
            )
            diagnostics.append(
                Diagnostic(
                    kind,
                    "source-owned target cannot also own descendant targets",
                    Phase.TARGET,
                    target=".".join(owner.target),
                    sources=tuple(sorted((owner.source, other.source))),
                )
            )
    return diagnostics
