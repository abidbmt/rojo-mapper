from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from rojo_mapper.config import Config
from rojo_mapper.diagnostics import Diagnostic, Phase
from rojo_mapper.formats import Artifact, classify
from rojo_mapper.portable import normalize_segment, portable_key, validate_segment

CONTEXT_TARGETS: dict[str, tuple[str, str]] = {
    "Shared": ("ReplicatedStorage", "Shared"),
    "Server": ("ServerScriptService", "Server"),
    "Client": ("ReplicatedStorage", "Client"),
    "First": ("ReplicatedFirst", "First"),
}


@dataclass(frozen=True, slots=True)
class LogicalRoot:
    source_path: Path
    source_relative: str
    runtime_layer: str
    place: str | None


@dataclass(frozen=True, slots=True)
class Layout:
    common_roots: tuple[LogicalRoot, ...]
    place_roots: dict[str, LogicalRoot]

    @property
    def places(self) -> tuple[str, ...]:
        return tuple(sorted(self.place_roots))

    def roots_for(self, target: str) -> tuple[LogicalRoot, ...]:
        if target == "Common":
            return self.common_roots
        place_root = self.place_roots.get(target)
        if place_root is None:
            return self.common_roots
        return (*self.common_roots, place_root)


@dataclass(frozen=True, slots=True)
class DiscoveredDirectory:
    path: Path
    project_relative: str
    root_relative: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DiscoveredFile:
    path: Path
    project_relative: str
    root_relative: tuple[str, ...]
    artifact: Artifact


@dataclass(frozen=True, slots=True)
class RootScan:
    directories: tuple[DiscoveredDirectory, ...]
    files: tuple[DiscoveredFile, ...]
    diagnostics: tuple[Diagnostic, ...]


def inspect_layout(config: Config) -> tuple[Layout, list[Diagnostic]]:
    diagnostics: list[Diagnostic] = []
    source = config.root / "Source"
    common_roots: list[LogicalRoot] = []
    place_roots: dict[str, LogicalRoot] = {}
    if not source.is_dir():
        diagnostics.append(
            Diagnostic(
                "source.missing_root",
                "Source must be an existing directory",
                Phase.SOURCE,
                path="Source",
            )
        )
        return Layout((), {}), diagnostics
    if source.is_symlink():
        diagnostics.append(
            Diagnostic(
                "source.link_unsupported",
                "dynamic roots may not be links",
                Phase.SOURCE,
                path="Source",
            )
        )
        return Layout((), {}), diagnostics
    if _is_static_subtree(source, config):
        return Layout((), {}), diagnostics

    source_children = _directory_children(
        source,
        config.root,
        diagnostics,
        flat_kind="source.flat_root_unsupported",
    )
    root_keys: dict[str, str] = (
        {portable_key("Places"): "Source/Places"}
        if any(child.name == "Places" for child in source_children)
        else {}
    )
    for child in source_children:
        if child.name == "Places":
            continue
        relative = _relative(child, config.root)
        if _is_static_subtree(child, config):
            continue
        if _record_named_directory(child.name, relative, root_keys, diagnostics, "root"):
            common_roots.append(LogicalRoot(child, relative, normalize_segment(child.name), None))

    places_directory = source / "Places"
    if places_directory.exists():
        if not places_directory.is_dir() or places_directory.is_symlink():
            diagnostics.append(
                Diagnostic(
                    "source.places_invalid",
                    "Source/Places must be a real directory",
                    Phase.SOURCE,
                    path="Source/Places",
                )
            )
        else:
            place_keys: dict[str, str] = {portable_key("Common"): "Common"}
            for child in _directory_children(
                places_directory,
                config.root,
                diagnostics,
                flat_kind="source.flat_place_unsupported",
            ):
                relative = _relative(child, config.root)
                if _record_named_directory(child.name, relative, place_keys, diagnostics, "place"):
                    place_roots[normalize_segment(child.name)] = LogicalRoot(
                        child,
                        relative,
                        "Place",
                        normalize_segment(child.name),
                    )

    common_roots.sort(key=lambda item: item.runtime_layer)
    return Layout(tuple(common_roots), place_roots), diagnostics


def scan_root(config: Config, logical_root: LogicalRoot) -> RootScan:
    directories: list[DiscoveredDirectory] = []
    files: list[DiscoveredFile] = []
    diagnostics: list[Diagnostic] = []
    source_keys: dict[str, str] = {}

    def visit(directory: Path, relative_parts: tuple[str, ...]) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: normalize_segment(item.name))
        except OSError as error:
            diagnostics.append(
                Diagnostic(
                    "source.read_failed",
                    str(error),
                    Phase.SOURCE,
                    path=_relative(directory, config.root),
                )
            )
            return
        for entry in entries:
            entry_path = Path(entry.path)
            root_parts = (*relative_parts, normalize_segment(entry.name))
            project_relative = _relative(entry_path, config.root)
            if entry.name != normalize_segment(entry.name):
                diagnostics.append(
                    Diagnostic(
                        "source.non_nfc_name",
                        "filesystem source names must use Unicode NFC normalization",
                        Phase.SOURCE,
                        path=project_relative,
                    )
                )
                continue
            problem = validate_segment(entry.name)
            if problem is not None:
                diagnostics.append(
                    Diagnostic(
                        "source.nonportable_name",
                        problem.message,
                        Phase.SOURCE,
                        path=project_relative,
                    )
                )
                continue
            source_key = portable_key("/".join(root_parts))
            previous = source_keys.get(source_key)
            if previous is not None and previous != project_relative:
                diagnostics.append(
                    Diagnostic(
                        "source.case_collision",
                        "filesystem sources collide under Unicode case-folding",
                        Phase.SOURCE,
                        sources=tuple(sorted((previous, project_relative))),
                    )
                )
                continue
            source_keys[source_key] = project_relative
            if _ignored(config, project_relative):
                continue
            try:
                if entry.is_symlink():
                    diagnostics.append(
                        Diagnostic(
                            "source.link_unsupported",
                            "links in dynamic roots are unsupported",
                            Phase.SOURCE,
                            path=project_relative,
                        )
                    )
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if _is_static_subtree(entry_path, config):
                        continue
                    directories.append(
                        DiscoveredDirectory(entry_path, project_relative, root_parts)
                    )
                    visit(entry_path, root_parts)
                elif entry.is_file(follow_symlinks=False):
                    artifact = classify(entry.name)
                    if artifact is not None:
                        files.append(
                            DiscoveredFile(entry_path, project_relative, root_parts, artifact)
                        )
            except OSError as error:
                diagnostics.append(
                    Diagnostic(
                        "source.read_failed", str(error), Phase.SOURCE, path=project_relative
                    )
                )

    visit(logical_root.source_path, ())
    return RootScan(tuple(directories), tuple(files), tuple(diagnostics))


def _directory_children(
    directory: Path,
    root: Path,
    diagnostics: list[Diagnostic],
    *,
    flat_kind: str | None = None,
) -> list[Path]:
    children: list[Path] = []
    try:
        entries = sorted(os.scandir(directory), key=lambda item: normalize_segment(item.name))
    except OSError as error:
        diagnostics.append(
            Diagnostic(
                "source.read_failed",
                str(error),
                Phase.SOURCE,
                path=_relative(directory, root),
            )
        )
        return children
    for entry in entries:
        path = Path(entry.path)
        try:
            if entry.is_symlink():
                diagnostics.append(
                    Diagnostic(
                        "source.link_unsupported",
                        "links in dynamic roots are unsupported",
                        Phase.SOURCE,
                        path=_relative(path, root),
                    )
                )
            elif entry.is_dir(follow_symlinks=False):
                children.append(path)
            elif (
                flat_kind is not None
                and entry.is_file(follow_symlinks=False)
                and classify(entry.name) is not None
            ):
                diagnostics.append(
                    Diagnostic(
                        flat_kind,
                        "recognized artifacts must live beneath a logical root directory",
                        Phase.SOURCE,
                        path=_relative(path, root),
                    )
                )
        except OSError as error:
            diagnostics.append(
                Diagnostic(
                    "source.read_failed",
                    str(error),
                    Phase.SOURCE,
                    path=_relative(path, root),
                )
            )
    return children


def _record_named_directory(
    name: str,
    relative: str,
    keys: dict[str, str],
    diagnostics: list[Diagnostic],
    category: str,
) -> bool:
    if name != normalize_segment(name):
        diagnostics.append(
            Diagnostic(
                f"source.{category}_non_nfc",
                f"{category} names must use Unicode NFC normalization",
                Phase.SOURCE,
                path=relative,
            )
        )
        return False
    problem = validate_segment(name)
    if problem is not None:
        diagnostics.append(
            Diagnostic(
                f"source.{category}_nonportable", problem.message, Phase.SOURCE, path=relative
            )
        )
        return False
    key = portable_key(name)
    previous = keys.get(key)
    if previous is not None:
        diagnostics.append(
            Diagnostic(
                f"source.{category}_case_collision",
                f"{category} names collide under Unicode case-folding",
                Phase.SOURCE,
                sources=tuple(sorted((previous, relative))),
            )
        )
        return False
    keys[key] = relative
    return True


def _relative(path: Path, root: Path) -> str:
    return "/".join(normalize_segment(part) for part in path.relative_to(root).parts)


def _ignored(config: Config, project_relative: str) -> bool:
    return any(pattern.matches(project_relative) for pattern in config.ignore)


def _is_static_subtree(path: Path, config: Config) -> bool:
    canonical = path.resolve(strict=False)
    return any(
        canonical == mount.source_path or mount.source_path in canonical.parents
        for mount in config.static
    )
