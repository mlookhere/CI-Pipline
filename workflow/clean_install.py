#!/usr/bin/env python3
"""Install the built wheel into an empty environment and import it.

The `build` group proves a wheel can be produced; it does not prove the wheel works. Those
are different claims, and the gap between them is where packaging defects live: a module left
out of `packages.find`, a data file that never made it into the archive, a dependency that
happens to be importable in the development venv because something else pulled it in.

`check_dependencies.py --artifacts` approaches the same risk from the manifest side, by
asserting that named assets appear inside the archive. This approaches it from the other:
nothing is named, the wheel is simply installed somewhere that has nothing else in it and the
package is imported. An asset nobody thought to name is covered by the second and not the
first.

Written for Issue #92, where the `clean_install` group was listed in the `release` stage and
was an empty list, so the required production check reported success having never installed
anything.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bash_tools import use_utf8_streams  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def newest_wheel(dist: Path) -> Path:
    """The wheel to test.

    Sorted rather than globbed-and-taken-first: a stale wheel from an earlier version sitting
    beside the current one would otherwise be installed instead, and the gate would pass for
    an artifact the release does not ship.
    """
    wheels = sorted(dist.glob("*.whl"), key=lambda path: path.stat().st_mtime)
    if not wheels:
        raise SystemExit(f"no wheel to install in {dist}; run the build group first")
    return wheels[-1]


def interpreter(environment: Path) -> Path:
    for candidate in ("bin/python", "Scripts/python.exe"):
        path = environment / candidate
        if path.exists():
            return path
    raise SystemExit(f"no interpreter in the throwaway environment at {environment}")


def check(wheel: Path, package: str) -> int:
    with tempfile.TemporaryDirectory(prefix="clean-install-") as directory:
        environment = Path(directory) / "venv"
        venv.create(environment, with_pip=True, clear=True)
        python = interpreter(environment)

        install = subprocess.run(
            [str(python), "-m", "pip", "install", "--disable-pip-version-check", str(wheel)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if install.returncode != 0:
            print(install.stdout)
            print(install.stderr, file=sys.stderr)
            raise SystemExit(f"{wheel.name} does not install into an empty environment")

        # Run in the temporary directory, not the repository: importing from `ROOT` would pick
        # up the source tree that is already on the path and prove nothing about the wheel.
        imported = subprocess.run(
            [str(python), "-c", f"import {package}; print({package}.__version__)"],
            cwd=directory,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if imported.returncode != 0:
            print(imported.stderr, file=sys.stderr)
            raise SystemExit(f"{wheel.name} installed but {package} could not be imported")

        print(f"{wheel.name} installs clean and imports as {package} {imported.stdout.strip()}.")
    return 0


def main() -> int:
    use_utf8_streams()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", default="artifacts/dist", help="directory holding the built wheel")
    parser.add_argument("--package", required=True, help="import name the wheel must provide")
    args = parser.parse_args()
    return check(newest_wheel(ROOT / args.dist), args.package)


if __name__ == "__main__":
    raise SystemExit(main())
