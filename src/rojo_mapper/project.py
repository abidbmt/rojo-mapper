from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast

from rojo_mapper.config import Config
from rojo_mapper.mapping import MappingEntry, MappingResult, Ownership


@dataclass(frozen=True, slots=True)
class ProjectCandidate:
    target: str
    data: dict[str, Any]
    encoded: bytes
    fingerprint: tuple[object, ...]


def build_project(config: Config, mapping: MappingResult) -> ProjectCandidate:
    if not mapping.success:
        raise ValueError("cannot encode a failed mapping")
    tree: dict[str, Any] = {"$className": "DataModel"}
    for entry in mapping.entries:
        _insert_entry(tree, entry)
    ordered_tree = _order_node(tree)
    data: dict[str, Any] = {"name": f"{config.project_name} - {mapping.target}"}
    place_id = config.cloud_places.get(mapping.target)
    if place_id is not None:
        data["servePlaceIds"] = [place_id]
    data["emitLegacyScripts"] = False
    data["globIgnorePaths"] = list(config.ignore_sources)
    data["tree"] = ordered_tree
    encoded = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()
    return ProjectCandidate(mapping.target, data, encoded, session_fingerprint(data))


def session_fingerprint(project: dict[str, Any]) -> tuple[object, ...]:
    return (
        project.get("name"),
        tuple(project.get("servePlaceIds", ())),
        project.get("emitLegacyScripts"),
        tuple(project.get("globIgnorePaths", ())),
    )


def _insert_entry(tree: dict[str, Any], entry: MappingEntry) -> None:
    node = tree
    for index, segment in enumerate(entry.target):
        final = index == len(entry.target) - 1
        if index == 0:
            child = node.setdefault(segment, {"$className": segment})
            node = child
            if final:
                _apply_entry(node, entry)
            continue
        if final:
            child = node.setdefault(segment, {})
            _apply_entry(child, entry)
            continue
        child = node.setdefault(
            segment,
            {"$className": "Folder", "$ignoreUnknownInstances": False},
        )
        node = child


def _apply_entry(node: dict[str, Any], entry: MappingEntry) -> None:
    if entry.ownership == Ownership.FOLDER:
        node.setdefault("$className", "Folder")
        node.setdefault("$ignoreUnknownInstances", False)
        return
    node["$path"] = entry.source


def _order_node(node: dict[str, Any]) -> dict[str, Any]:
    ordered: dict[str, Any] = {}
    for metadata in ("$className", "$ignoreUnknownInstances", "$path"):
        if metadata in node:
            ordered[metadata] = node[metadata]
    for key in sorted(key for key in node if not key.startswith("$")):
        value = node[key]
        ordered[key] = (
            _order_node(cast("dict[str, Any]", value)) if isinstance(value, dict) else value
        )
    return ordered
