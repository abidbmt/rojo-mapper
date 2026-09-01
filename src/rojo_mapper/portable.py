from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_INVALID_WINDOWS = re.compile(r'[<>:"|?*]')
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_DRIVE = re.compile(r"^[A-Za-z]:")


@dataclass(frozen=True, slots=True)
class PathProblem:
    kind: str
    message: str


def normalize_segment(segment: str) -> str:
    return unicodedata.normalize("NFC", segment)


def portable_key(value: str) -> str:
    return normalize_segment(value).casefold()


def validate_segment(segment: str) -> PathProblem | None:
    normalized = normalize_segment(segment)
    if not normalized or normalized in {".", ".."}:
        return PathProblem("path.invalid_segment", f"invalid path segment {segment!r}")
    if _CONTROL.search(normalized):
        return PathProblem(
            "path.control_character", f"path segment contains a control character: {segment!r}"
        )
    if _INVALID_WINDOWS.search(normalized):
        return PathProblem(
            "path.windows_invalid", f"path segment is not portable to Windows: {segment!r}"
        )
    if normalized.endswith((".", " ")):
        return PathProblem(
            "path.windows_trailing", f"path segment ends in a dot or space: {segment!r}"
        )
    stem = normalized.split(".", maxsplit=1)[0].upper()
    if stem in _WINDOWS_RESERVED:
        return PathProblem(
            "path.windows_reserved", f"path segment is reserved on Windows: {segment!r}"
        )
    return None


def normalize_relative(value: str) -> tuple[str, ...]:
    normalized = value.replace("\\", "/")
    if normalized.startswith(("/", "//")) or _DRIVE.match(normalized):
        raise ValueError("path must be project-root-relative")
    parts = tuple(normalized.split("/"))
    for part in parts:
        problem = validate_segment(part)
        if problem is not None:
            raise ValueError(problem.message)
    return parts


def contained_directory(root: Path, parts: tuple[str, ...]) -> Path:
    canonical_root = root.resolve(strict=True)
    candidate = canonical_root.joinpath(*parts).resolve(strict=True)
    if not candidate.is_dir():
        raise ValueError("path must name an existing directory")
    if candidate == canonical_root or canonical_root not in candidate.parents:
        raise ValueError("path must be a directory contained by the project root")
    return candidate


@dataclass(frozen=True, slots=True)
class PortableGlob:
    source: str
    segments: tuple[str, ...]
    segment_patterns: tuple[re.Pattern[str] | None, ...]

    @classmethod
    def parse(cls, source: str) -> PortableGlob:
        if not source:
            raise ValueError("ignore pattern must not be empty")
        if "\\" in source:
            raise ValueError("escapes and backslash separators are unsupported")
        if source.startswith("!"):
            raise ValueError("negated ignore patterns are unsupported")
        if source.startswith("/"):
            raise ValueError("leading-root ignore anchors are unsupported")
        if source.endswith("/"):
            raise ValueError("trailing directory-only ignore patterns are unsupported")
        if any(token in source for token in ("?", "[", "]")):
            raise ValueError("question marks and character classes are unsupported")
        segments = tuple(source.split("/"))
        if any(not segment for segment in segments):
            raise ValueError("ignore patterns must not contain empty segments")
        if any("**" in segment and segment != "**" for segment in segments):
            raise ValueError("double-star is supported only as a whole segment")
        for segment in segments:
            if segment == "**":
                continue
            problem = validate_segment(segment.replace("*", "x"))
            if problem is not None:
                raise ValueError(problem.message)
        segment_patterns = tuple(
            None
            if segment == "**"
            else re.compile(
                "".join(".*" if character == "*" else re.escape(character) for character in segment)
            )
            for segment in segments
        )
        return cls(source=source, segments=segments, segment_patterns=segment_patterns)

    def matches(self, relative_path: str) -> bool:
        path_segments = tuple(relative_path.replace("\\", "/").split("/"))
        return self._match(0, 0, path_segments)

    def _match(self, pattern_index: int, path_index: int, path: tuple[str, ...]) -> bool:
        if pattern_index == len(self.segments):
            return path_index == len(path)
        segment = self.segments[pattern_index]
        if segment == "**":
            return any(
                self._match(pattern_index + 1, next_index, path)
                for next_index in range(path_index, len(path) + 1)
            )
        pattern = self.segment_patterns[pattern_index]
        if (
            path_index == len(path)
            or pattern is None
            or pattern.fullmatch(path[path_index]) is None
        ):
            return False
        return self._match(pattern_index + 1, path_index + 1, path)
