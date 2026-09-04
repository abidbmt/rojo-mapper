from __future__ import annotations

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
import json
import sys
import traceback
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from rojo_mapper import __version__
from rojo_mapper.dev import run_dev
from rojo_mapper.diagnostics import Diagnostic, ExpectedFailure, Phase, render_human
from rojo_mapper.service import (
    candidate_for,
    load_workspace,
    select_single_target,
    validate_workspace,
)
from rojo_mapper.watcher import MANIFEST_NAME, SOURCEMAP_NAME, write_candidate

app = typer.Typer(
    name="rmp",
    help="Generate one opinionated single-target Rojo project.",
    no_args_is_help=True,
    invoke_without_command=True,
    pretty_exceptions_enable=False,
)
console = Console(stderr=False)


class OutputFormat(StrEnum):
    HUMAN = "human"
    JSON = "json"


@app.callback()
def callback(
    context: typer.Context,
    debug: Annotated[bool, typer.Option("--debug", help="Show internal tracebacks.")] = False,
    version: Annotated[
        bool,
        typer.Option("--version", is_eager=True, help="Print the CLI version."),
    ] = False,
) -> None:
    if version:
        console.print(__version__)
        raise typer.Exit()
    context.ensure_object(dict)
    context.obj["debug"] = debug


@app.command("generate")
def generate_command(
    context: typer.Context,
    target: Annotated[str | None, typer.Argument(help="Place name or Common.")] = None,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", help="Output format."),
    ] = OutputFormat.HUMAN,
) -> None:
    command = "generate"
    try:
        workspace = load_workspace(Path.cwd())
        selected = select_single_target(workspace, target)
        candidate = candidate_for(workspace, selected)
        changed = write_candidate(workspace.config, candidate)
        payload = _payload(command, target=selected, changed=changed)
        if output_format == OutputFormat.JSON:
            _json(payload)
        else:
            action = "wrote" if changed else "kept unchanged"
            console.print(f"[green]Generated {selected}[/green]; {action} {MANIFEST_NAME}")
    except ExpectedFailure as error:
        _expected_failure(command, target, output_format, error)
    except Exception as error:
        _internal_failure(command, target, output_format, error, _debug(context))


@app.command("validate")
def validate_command(
    context: typer.Context,
    target: Annotated[str | None, typer.Argument(help="Place name or Common.")] = None,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", help="Output format."),
    ] = OutputFormat.HUMAN,
) -> None:
    command = "validate"
    try:
        workspace = load_workspace(Path.cwd())
        targets = validate_workspace(workspace, target)
        payload = _payload(command, target=target, changed=False)
        payload["targets"] = list(targets)
        if output_format == OutputFormat.JSON:
            _json(payload)
        else:
            console.print(f"[green]Valid[/green]: {', '.join(targets)}")
    except ExpectedFailure as error:
        _expected_failure(command, target, output_format, error)
    except Exception as error:
        _internal_failure(command, target, output_format, error, _debug(context))


@app.command("list")
def list_command(
    context: typer.Context,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", help="Output format."),
    ] = OutputFormat.HUMAN,
) -> None:
    command = "list"
    try:
        workspace = load_workspace(Path.cwd())
        targets: list[dict[str, str | int]] = [
            {
                "name": target,
                **(
                    {"cloud_place_id": workspace.config.cloud_places[target]}
                    if target in workspace.config.cloud_places
                    else {}
                ),
            }
            for target in workspace.targets
        ]
        payload = _payload(command, changed=False)
        payload.update(
            {
                "targets": targets,
                "manifest": {"path": MANIFEST_NAME, "owner": "rmp"},
                "sourcemap": {"path": SOURCEMAP_NAME, "owner": "Luau-LSP/Rojo"},
            }
        )
        if output_format == OutputFormat.JSON:
            _json(payload)
        else:
            table = Table(title="Targets")
            table.add_column("Target")
            table.add_column("Cloud place ID")
            for item in targets:
                table.add_row(str(item["name"]), str(item.get("cloud_place_id", "-")))
            console.print(table)
            console.print(f"Manifest: {MANIFEST_NAME} (rmp)")
            console.print(f"Sourcemap: {SOURCEMAP_NAME} (Luau-LSP/Rojo)")
    except ExpectedFailure as error:
        _expected_failure(command, None, output_format, error)
    except Exception as error:
        _internal_failure(command, None, output_format, error, _debug(context))


@app.command("dev")
def dev_command(
    context: typer.Context,
    target: Annotated[str | None, typer.Argument(help="Place name or Common.")] = None,
) -> None:
    try:
        selected = run_dev(Path.cwd(), target, console)
        if selected:
            console.print(f"Stopped {selected}")
    except ExpectedFailure as error:
        render_human(console, error.diagnostics)
        raise typer.Exit(1) from error
    except Exception as error:
        diagnostic = Diagnostic("internal.error", str(error), Phase.INTERNAL)
        render_human(console, [diagnostic])
        if _debug(context):
            details = _debug_details()
            console.print(details["traceback"])
            console.print(f"Python: {details['python']}")
            console.print(f"Working directory: {details['cwd']}")
        raise typer.Exit(1) from error


def _payload(
    command: str,
    *,
    target: str | None = None,
    changed: bool,
    success: bool = True,
    diagnostics: list[Diagnostic] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "success": success,
        "command": command,
    }
    if target is not None:
        payload["target"] = target
    payload["changed"] = changed
    payload["manifest"] = MANIFEST_NAME
    payload["diagnostics"] = [item.to_dict() for item in diagnostics or []]
    return payload


def _expected_failure(
    command: str,
    target: str | None,
    output_format: OutputFormat,
    error: ExpectedFailure,
) -> None:
    if output_format == OutputFormat.JSON:
        _json(
            _payload(
                command,
                target=target,
                changed=False,
                success=False,
                diagnostics=error.diagnostics,
            )
        )
    else:
        render_human(console, error.diagnostics)
    raise typer.Exit(1) from error


def _internal_failure(
    command: str,
    target: str | None,
    output_format: OutputFormat,
    error: Exception,
    debug: bool,
) -> None:
    diagnostic = Diagnostic("internal.error", str(error), Phase.INTERNAL)
    if output_format == OutputFormat.JSON:
        payload = _payload(
            command,
            target=target,
            changed=False,
            success=False,
            diagnostics=[diagnostic],
        )
        if debug:
            payload["debug"] = _debug_details()
        _json(payload)
    else:
        render_human(console, [diagnostic])
        if debug:
            details = _debug_details()
            console.print(details["traceback"])
            console.print(f"Python: {details['python']}")
            console.print(f"Working directory: {details['cwd']}")
    raise typer.Exit(1) from error


def _json(payload: dict[str, Any]) -> None:
    console.print_json(json.dumps(payload, ensure_ascii=False))


def _debug(context: typer.Context) -> bool:
    return bool(context.obj and context.obj.get("debug", False))


def _debug_details() -> dict[str, str]:
    return {
        "traceback": traceback.format_exc(),
        "python": sys.version,
        "cwd": str(Path.cwd()),
    }


def main() -> None:
    app(prog_name="rmp")
