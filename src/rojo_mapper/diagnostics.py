from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from rich.console import Console
from rich.table import Table


class Phase(IntEnum):
    CONFIG = 10
    SOURCE = 20
    MAPPING = 30
    TARGET = 40
    FILESYSTEM = 50
    ROJO = 60
    WATCHER = 70
    DEV = 80
    INTERNAL = 90


@dataclass(frozen=True, slots=True)
class Diagnostic:
    kind: str
    message: str
    phase: Phase
    path: str | None = None
    target: str | None = None
    sources: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=lambda: {})

    def sort_key(self) -> tuple[object, ...]:
        return (
            int(self.phase),
            self.kind,
            self.target or "",
            self.path or "",
            tuple(sorted(self.sources)),
            self.message,
        )

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"kind": self.kind, "message": self.message}
        if self.path is not None:
            value["path"] = self.path
        if self.target is not None:
            value["target"] = self.target
        if self.sources:
            value["sources"] = sorted(self.sources)
        if self.details:
            value["details"] = self.details
        return value


def sorted_diagnostics(diagnostics: list[Diagnostic] | tuple[Diagnostic, ...]) -> list[Diagnostic]:
    return sorted(diagnostics, key=Diagnostic.sort_key)


class ExpectedFailure(Exception):
    def __init__(self, diagnostics: list[Diagnostic]) -> None:
        super().__init__(diagnostics[0].message if diagnostics else "operation failed")
        self.diagnostics = sorted_diagnostics(diagnostics)


def render_human(
    console: Console,
    diagnostics: list[Diagnostic],
    *,
    title: str = "Diagnostics",
) -> None:
    if not diagnostics:
        return
    table = Table(title=title, show_header=True, header_style="bold")
    table.add_column("Kind", no_wrap=True)
    table.add_column("Location")
    table.add_column("Message")
    for diagnostic in sorted_diagnostics(diagnostics):
        location = diagnostic.path or diagnostic.target or "-"
        table.add_row(diagnostic.kind, location, diagnostic.message)
    console.print(table)
