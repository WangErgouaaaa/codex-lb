from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence

CREATE_NO_WINDOW = 0x08000000


def _powershell_executable() -> Path:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    return system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def main(arguments: Sequence[str] | None = None) -> int:
    values = list(sys.argv[1:] if arguments is None else arguments)
    if not values:
        return 2

    script = Path(values[0])
    if not script.is_file():
        return 2

    command = [
        str(_powershell_executable()),
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        *values[1:],
    ]
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
    except OSError:
        return 3
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
