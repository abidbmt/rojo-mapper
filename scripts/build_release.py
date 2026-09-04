from __future__ import annotations

import argparse
import hashlib
import platform
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import zipfile
from pathlib import Path

from rojo_mapper import __version__

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build" / "nuitka"
DIST = ROOT / "dist"


def platform_label() -> tuple[str, str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    os_name = {"windows": "windows", "linux": "linux", "darwin": "macos"}.get(system)
    architecture = {
        "amd64": "x64",
        "x86_64": "x64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(machine)
    if os_name is None or architecture is None:
        raise RuntimeError(f"unsupported release platform: {system}-{machine}")
    executable = "rojo-mapper.exe" if system == "windows" else "rojo-mapper"
    return os_name, architecture, executable


def run(command: list[str], *, cwd: Path = ROOT) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def _ensure_venv_libpython() -> None:
    # Nuitka standalone resolves the Python dylib relative to the running
    # venv (sys.prefix). uv venvs on macOS do not carry libpython, while the
    # base interpreter does. Symlink it into place so the link step succeeds.
    if platform.system().lower() != "darwin":
        return
    names = dict.fromkeys(
        [
            sysconfig.get_config_var("LDLIBRARY"),
            sysconfig.get_config_var("LIBRARY"),
            "libpython3.14.dylib",
        ]
    )
    target_dir = Path(sys.prefix, "lib")
    base_dir = Path(sys.base_prefix, "lib")
    for name in names:
        if not name or not str(name).endswith(".dylib"):
            continue
        target = target_dir / str(name)
        if target.exists():
            return
        source = base_dir / str(name)
        if source.exists():
            target_dir.mkdir(parents=True, exist_ok=True)
            target.symlink_to(source)
            return
    raise RuntimeError("unable to locate a base libpython dylib for the macOS build")


def build(*, clean: bool = True) -> Path:
    os_name, architecture, executable = platform_label()
    if clean:
        shutil.rmtree(BUILD, ignore_errors=True)
    _ensure_venv_libpython()
    DIST.mkdir(exist_ok=True)
    run(
        [
            sys.executable,
            "-m",
            "nuitka",
            "--mode=standalone",
            "--assume-yes-for-downloads",
            f"--output-dir={BUILD}",
            f"--output-filename={executable}",
            "--product-name=rojo-mapper",
            f"--product-version={__version__}",
            f"--file-version={__version__}",
            "--nofollow-import-to=pydantic.mypy",
            "--nofollow-import-to=pydantic.v1.*",
            "--python-flag=-m",
            str(ROOT / "src" / "rojo_mapper"),
        ]
    )
    built_directory = BUILD / "__main__.dist"
    if not built_directory.is_dir():
        candidates = sorted(BUILD.glob("*.dist"))
        if len(candidates) != 1:
            raise RuntimeError("Nuitka did not produce exactly one standalone directory")
        built_directory = candidates[0]
    root_name = f"rojo-mapper-v{__version__}-{os_name}-{architecture}"
    archive_root = BUILD / root_name
    if archive_root.exists():
        shutil.rmtree(archive_root)
    built_directory.rename(archive_root)
    binary = archive_root / executable
    run([str(binary), "--version"])
    run([str(binary), "--help"])
    run([str(binary), "validate"], cwd=ROOT / "examples" / "multi-place")

    if os_name == "windows":
        archive = DIST / f"{root_name}.zip"
        archive.unlink(missing_ok=True)
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
            for path in sorted(archive_root.rglob("*")):
                if path.is_file():
                    output.write(path, Path(root_name) / path.relative_to(archive_root))
    else:
        archive = DIST / f"{root_name}.tar.gz"
        archive.unlink(missing_ok=True)
        with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as output:
            output.add(archive_root, arcname=root_name, recursive=True)
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(archive.suffix + ".sha256").write_text(
        f"{checksum}  {archive.name}\n",
        encoding="utf-8",
        newline="\n",
    )
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build the standalone release archive.")
    parser.add_argument(
        "--no-clean",
        action="store_true",
        help="reuse the existing build directory for faster local rebuilds (CI stays clean)",
    )
    artifact = build(clean=not parser.parse_args().no_clean)
    print(artifact.relative_to(ROOT).as_posix())
