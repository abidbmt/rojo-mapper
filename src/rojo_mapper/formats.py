from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ArtifactFamily(StrEnum):
    SCRIPT = "script"
    DATA = "data"
    MODEL = "model"
    TEXT = "text"
    COMPOSITION = "composition"


@dataclass(frozen=True, slots=True)
class Artifact:
    family: ArtifactFamily
    logical_stem: str
    suffix: str
    prefix_eligible: bool
    init: bool = False


_SCRIPT_SUFFIXES = (
    ".server.luau",
    ".client.luau",
    ".plugin.luau",
    ".server.lua",
    ".client.lua",
    ".plugin.lua",
    ".luau",
    ".lua",
)
_DATA_SUFFIXES = (".jsonc", ".json", ".toml", ".yaml", ".yml")
_MODEL_SUFFIXES = (".model.jsonc", ".model.json", ".rbxmx", ".rbxm")
_TEXT_SUFFIXES = (".csv", ".txt")
_INIT_NAMES = {f"init{suffix}" for suffix in _SCRIPT_SUFFIXES}


def classify(filename: str) -> Artifact | None:
    lowered = filename.casefold()
    if _is_composition(lowered):
        return Artifact(ArtifactFamily.COMPOSITION, filename, "", False)
    for suffix in _MODEL_SUFFIXES:
        if lowered.endswith(suffix) and len(filename) > len(suffix):
            return Artifact(ArtifactFamily.MODEL, filename[: -len(suffix)], suffix, False)
    for suffix in _SCRIPT_SUFFIXES:
        if lowered.endswith(suffix) and len(filename) > len(suffix):
            stem = filename[: -len(suffix)]
            return Artifact(
                ArtifactFamily.SCRIPT,
                stem,
                suffix,
                True,
                init=lowered in _INIT_NAMES,
            )
    for suffix in _DATA_SUFFIXES:
        if lowered.endswith(suffix) and len(filename) > len(suffix):
            return Artifact(ArtifactFamily.DATA, filename[: -len(suffix)], suffix, True)
    for suffix in _TEXT_SUFFIXES:
        if lowered.endswith(suffix) and len(filename) > len(suffix):
            return Artifact(ArtifactFamily.TEXT, filename[: -len(suffix)], suffix, False)
    return None


def _is_composition(lowered: str) -> bool:
    return lowered.endswith((".meta.json", ".meta.jsonc", ".project.json", ".project.jsonc"))
