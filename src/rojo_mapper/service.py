from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rojo_mapper.config import Config, load_config, validate_cloud_places
from rojo_mapper.diagnostics import Diagnostic, ExpectedFailure, Phase, sorted_diagnostics
from rojo_mapper.discovery import Layout, inspect_layout
from rojo_mapper.mapping import MappingResult, build_mapping
from rojo_mapper.project import ProjectCandidate, build_project


@dataclass(frozen=True, slots=True)
class Workspace:
    config: Config
    layout: Layout

    @property
    def targets(self) -> tuple[str, ...]:
        return ("Common", *self.layout.places)


def load_workspace(root: Path) -> Workspace:
    config = load_config(root)
    layout, layout_diagnostics = inspect_layout(config)
    cloud_diagnostics = validate_cloud_places(config, layout.places)
    if cloud_diagnostics:
        raise ExpectedFailure(cloud_diagnostics)
    if layout_diagnostics:
        raise ExpectedFailure(layout_diagnostics)
    return Workspace(config, layout)


def select_single_target(workspace: Workspace, requested: str | None) -> str:
    if requested is not None:
        if requested in workspace.targets:
            return requested
        raise ExpectedFailure(
            [
                Diagnostic(
                    "target.unknown",
                    "target must be exact Common or a discovered place name",
                    Phase.TARGET,
                    target=requested,
                    details={"available": list(workspace.targets)},
                )
            ]
        )
    if len(workspace.layout.places) == 1:
        return workspace.layout.places[0]
    raise ExpectedFailure(
        [
            Diagnostic(
                "target.required",
                "target is required unless exactly one place is discovered",
                Phase.TARGET,
                details={"available": list(workspace.targets)},
            )
        ]
    )


def candidate_for(workspace: Workspace, target: str) -> ProjectCandidate:
    mapping = build_mapping(workspace.config, workspace.layout, target)
    if mapping.diagnostics:
        raise ExpectedFailure(list(mapping.diagnostics))
    return build_project(workspace.config, mapping)


def validate_workspace(workspace: Workspace, requested: str | None) -> tuple[str, ...]:
    targets = (
        workspace.targets if requested is None else (select_single_target(workspace, requested),)
    )
    diagnostics: list[Diagnostic] = []
    for target in targets:
        result: MappingResult = build_mapping(workspace.config, workspace.layout, target)
        diagnostics.extend(result.diagnostics)
    if diagnostics:
        unique = {diagnostic.sort_key(): diagnostic for diagnostic in diagnostics}
        raise ExpectedFailure(sorted_diagnostics(list(unique.values())))
    return targets
