from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

from rojo_mapper.diagnostics import Diagnostic, ExpectedFailure, Phase
from rojo_mapper.portable import (
    PortableGlob,
    contained_directory,
    normalize_relative,
    normalize_segment,
    portable_key,
    validate_segment,
)

CONFIG_NAME = "rojo-mapper.toml"


class CloudModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    universe_id: StrictInt = Field(gt=0)
    places: dict[str, StrictInt] = Field(default_factory=dict)


class MapperModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1] = Field(alias="schema")
    ignore: list[str] = Field(default_factory=list)
    static: dict[str, str] = Field(default_factory=dict)
    cloud: CloudModel | None = None


@dataclass(frozen=True, slots=True)
class StaticMount:
    source: str
    source_path: Path
    target: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Config:
    root: Path
    project_name: str
    ignore: tuple[PortableGlob, ...]
    static: tuple[StaticMount, ...]
    universe_id: int | None
    cloud_places: dict[str, int]

    @property
    def ignore_sources(self) -> tuple[str, ...]:
        return tuple(pattern.source for pattern in self.ignore)


def load_config(root: Path) -> Config:
    canonical_root = root.resolve(strict=True)
    path = canonical_root / CONFIG_NAME
    diagnostics: list[Diagnostic] = []
    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ExpectedFailure(
            [
                Diagnostic(
                    "config.missing", f"missing ./{CONFIG_NAME}", Phase.CONFIG, path=CONFIG_NAME
                )
            ]
        ) from None
    except OSError as error:
        raise ExpectedFailure(
            [Diagnostic("config.read_failed", str(error), Phase.CONFIG, path=CONFIG_NAME)]
        ) from error

    try:
        raw = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as error:
        raise ExpectedFailure(
            [Diagnostic("config.invalid_toml", str(error), Phase.CONFIG, path=CONFIG_NAME)]
        ) from error

    try:
        model = MapperModel.model_validate(raw, strict=True)
    except ValidationError as error:
        for item in error.errors(include_url=False):
            location = ".".join(str(part) for part in item["loc"])
            kind = (
                "config.unknown_field"
                if item["type"] == "extra_forbidden"
                else "config.invalid_value"
            )
            diagnostics.append(
                Diagnostic(kind, item["msg"], Phase.CONFIG, path=f"{CONFIG_NAME}:{location}")
            )
        raise ExpectedFailure(diagnostics) from error

    project_problem = validate_segment(canonical_root.name)
    if project_problem is not None:
        diagnostics.append(
            Diagnostic(
                "config.project_name_nonportable",
                project_problem.message,
                Phase.CONFIG,
                path=canonical_root.name,
            )
        )

    patterns: list[PortableGlob] = []
    for index, source in enumerate(model.ignore):
        try:
            patterns.append(PortableGlob.parse(source))
        except ValueError as error:
            diagnostics.append(
                Diagnostic(
                    "config.ignore_unsupported",
                    str(error),
                    Phase.CONFIG,
                    path=f"{CONFIG_NAME}:ignore[{index}]",
                )
            )

    mounts: list[StaticMount] = []
    source_keys: dict[str, str] = {}
    target_keys: dict[tuple[str, ...], str] = {}
    places_root = canonical_root / "Source" / "Places"
    for source, target_value in model.static.items():
        try:
            parts = normalize_relative(source)
            source_path = contained_directory(canonical_root, parts)
            if source_path == places_root or places_root in source_path.parents:
                raise ValueError("static sources may not lie beneath Source/Places")
        except (OSError, ValueError) as error:
            diagnostics.append(
                Diagnostic("config.static_source_invalid", str(error), Phase.CONFIG, path=source)
            )
            continue
        source_normalized = "/".join(parts)
        source_key = portable_key(source_normalized)
        previous_source = source_keys.get(source_key)
        if previous_source is not None:
            diagnostics.append(
                Diagnostic(
                    "config.static_source_case_collision",
                    "static source keys collide under Unicode case-folding",
                    Phase.CONFIG,
                    sources=tuple(sorted((previous_source, source_normalized))),
                )
            )
            continue
        source_keys[source_key] = source_normalized

        target = tuple(normalize_segment(segment) for segment in target_value.split("."))
        target_problem = next((validate_segment(segment) for segment in target if segment), None)
        if not target or any(not segment for segment in target) or target_problem is not None:
            message = (
                target_problem.message
                if target_problem is not None
                else "target must be a nonempty dot path"
            )
            diagnostics.append(
                Diagnostic(
                    "config.static_target_invalid", message, Phase.CONFIG, target=target_value
                )
            )
            continue
        target_key = tuple(portable_key(segment) for segment in target)
        previous_target = target_keys.get(target_key)
        if previous_target is not None:
            diagnostics.append(
                Diagnostic(
                    "config.static_target_case_collision",
                    "static targets collide under Unicode case-folding",
                    Phase.CONFIG,
                    target=target_value,
                    sources=tuple(sorted((previous_target, source_normalized))),
                )
            )
            continue
        target_keys[target_key] = source_normalized
        mounts.append(StaticMount(source_normalized, source_path, target))

    diagnostics.extend(
        Diagnostic(
            "config.static_source_overlap",
            "static source directories may not contain one another",
            Phase.CONFIG,
            sources=tuple(sorted((mount.source, other.source))),
        )
        for index, mount in enumerate(mounts)
        for other in mounts[index + 1 :]
        if (
            mount.source_path in other.source_path.parents
            or other.source_path in mount.source_path.parents
        )
    )

    raw_cloud_places = dict(model.cloud.places) if model.cloud is not None else {}
    cloud_places: dict[str, int] = {}
    cloud_keys: dict[str, str] = {}
    for raw_name, place_id in raw_cloud_places.items():
        name = normalize_segment(raw_name)
        problem = validate_segment(raw_name)
        if problem is not None:
            diagnostics.append(
                Diagnostic(
                    "config.cloud_place_invalid", problem.message, Phase.CONFIG, target=raw_name
                )
            )
        if place_id <= 0:
            diagnostics.append(
                Diagnostic(
                    "config.cloud_place_invalid",
                    "Cloud place IDs must be positive integers",
                    Phase.CONFIG,
                    target=raw_name,
                )
            )
        key = portable_key(name)
        if key in cloud_keys:
            diagnostics.append(
                Diagnostic(
                    "config.cloud_place_case_collision",
                    "Cloud place keys collide under Unicode case-folding",
                    Phase.CONFIG,
                    sources=tuple(sorted((cloud_keys[key], raw_name))),
                )
            )
        else:
            cloud_keys[key] = raw_name
            cloud_places[name] = place_id

    if diagnostics:
        raise ExpectedFailure(diagnostics)
    return Config(
        root=canonical_root,
        project_name=canonical_root.name,
        ignore=tuple(patterns),
        static=tuple(sorted(mounts, key=lambda mount: mount.source)),
        universe_id=model.cloud.universe_id if model.cloud is not None else None,
        cloud_places=cloud_places,
    )


def validate_cloud_places(config: Config, places: tuple[str, ...]) -> list[Diagnostic]:
    allowed = {"Common", *places}
    return [
        Diagnostic(
            "config.cloud_place_unknown",
            "Cloud place key must be exact Common or a discovered place name",
            Phase.CONFIG,
            target=name,
        )
        for name in sorted(config.cloud_places)
        if name not in allowed
    ]
