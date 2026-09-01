from __future__ import annotations

# pyright: reportUnknownVariableType=false
import asyncio
import contextlib
import os
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

from watchfiles import Change, awatch

from rojo_mapper.config import CONFIG_NAME, Config
from rojo_mapper.diagnostics import Diagnostic, ExpectedFailure, Phase
from rojo_mapper.project import ProjectCandidate

MANIFEST_NAME = "default.project.json"
SOURCEMAP_NAME = "sourcemap.json"


async def watch_structural_changes(
    paths: tuple[Path, ...],
    callback: Callable[[set[tuple[Change, str]]], Awaitable[tuple[Path, ...] | None]],
) -> None:
    current = paths
    while True:
        iterator = awatch(
            *current, debounce=150, step=50, rust_timeout=1000, yield_on_timeout=False
        )
        async for changes in iterator:
            replacement = await callback(changes)
            if replacement is not None and replacement != current:
                current = replacement
                break
        else:
            raise RuntimeError("watchfiles iterator terminated unexpectedly")


def watched_paths(config: Config) -> tuple[Path, ...]:
    candidates = {config.root / "Source", config.root / CONFIG_NAME}
    candidates.update(mount.source_path for mount in config.static)
    return tuple(sorted(candidates, key=lambda path: path.as_posix()))


def write_candidate(config: Config, candidate: ProjectCandidate) -> bool:
    destination = config.root / MANIFEST_NAME
    try:
        if destination.exists() and destination.read_bytes() == candidate.encoded:
            return False
    except OSError as error:
        raise ExpectedFailure(
            [
                Diagnostic(
                    "filesystem.read_failed",
                    str(error),
                    Phase.FILESYSTEM,
                    path=MANIFEST_NAME,
                )
            ]
        ) from error

    descriptor = -1
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".rojo-mapper-",
            suffix=".tmp",
            dir=config.root,
        )
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(candidate.encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, destination)
        temporary_name = None
    except OSError as error:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        if temporary_name is not None:
            with contextlib.suppress(OSError):
                Path(temporary_name).unlink(missing_ok=True)
        raise ExpectedFailure(
            [
                Diagnostic(
                    "filesystem.write_failed",
                    str(error),
                    Phase.FILESYSTEM,
                    path=MANIFEST_NAME,
                )
            ]
        ) from error
    return True


async def cancel_task(task: asyncio.Task[None]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
