#!/usr/bin/env python3
"""
Bootstrap script: discovers a suitable Python interpreter, creates a venv,
and installs the plugin's dependencies.

Run once (or re-run safely) before using any other script in this plugin.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# ── Constants ────────────────────────────────────────────────────────────────

PYTHON_CANDIDATES = [
    "python3.14",
    "python3.13",
    "python3.12",
    "python3.11",
    "python3.10",
    "python3",
]

MIN_VERSION = (3, 10)

SCRIPTS_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPTS_DIR.parent
VENV_DIR = PLUGIN_ROOT / ".coding-productivity" / ".venv"
REQUIREMENTS = SCRIPTS_DIR / "requirements.txt"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _find_python() -> str | None:
    """Return the first suitable Python binary on PATH."""
    for candidate in PYTHON_CANDIDATES:
        exe = shutil.which(candidate)
        if exe is None:
            continue
        try:
            out = subprocess.check_output(
                [exe, "--version"], stderr=subprocess.STDOUT, text=True
            )
            match = re.search(r"(\d+)\.(\d+)", out)
            if match:
                major, minor = int(match.group(1)), int(match.group(2))
                if (major, minor) >= MIN_VERSION:
                    return exe
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
    return None


def _venv_is_functional(venv: Path) -> bool:
    """Return True if the venv directory contains a working Python."""
    if sys.platform == "win32":
        python = venv / "Scripts" / "python.exe"
    else:
        python = venv / "bin" / "python"
    if not python.exists():
        return False
    try:
        subprocess.check_call(
            [str(python), "-c", "import sys; sys.exit(0)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _venv_python(venv: Path) -> str:
    if sys.platform == "win32":
        return str(venv / "Scripts" / "python.exe")
    return str(venv / "bin" / "python")


# ── Main ─────────────────────────────────────────────────────────────────────

def setup() -> Path:
    """
    Create (or reuse) a virtual environment and install requirements.

    Returns the path to the venv Python interpreter.
    """
    python = _find_python()
    if python is None:
        print(
            "ERROR: No suitable Python >= 3.10 found.\n"
            "Searched for: " + ", ".join(PYTHON_CANDIDATES) + "\n\n"
            "Install Python 3.10+ and make sure it is on your PATH.\n"
            "  macOS:   brew install python@3.14\n"
            "  Ubuntu:  sudo apt install python3.12 python3.12-venv\n"
            "  Windows: https://www.python.org/downloads/",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Using interpreter: {python}")

    # Reuse an existing functional venv; recreate a broken one.
    if VENV_DIR.exists():
        if _venv_is_functional(VENV_DIR):
            print(f"Reusing existing venv at {VENV_DIR}")
        else:
            print(f"Existing venv is broken, recreating: {VENV_DIR}")
            shutil.rmtree(VENV_DIR)
            subprocess.check_call([python, "-m", "venv", str(VENV_DIR)])
    else:
        VENV_DIR.parent.mkdir(parents=True, exist_ok=True)
        print(f"Creating venv at {VENV_DIR}")
        subprocess.check_call([python, "-m", "venv", str(VENV_DIR)])

    vpy = _venv_python(VENV_DIR)

    # Upgrade pip
    print("Upgrading pip...")
    subprocess.check_call(
        [vpy, "-m", "pip", "install", "--upgrade", "pip"],
        stdout=subprocess.DEVNULL,
    )

    # Install requirements
    if REQUIREMENTS.exists():
        print(f"Installing dependencies from {REQUIREMENTS.name}...")
        subprocess.check_call(
            [vpy, "-m", "pip", "install", "-r", str(REQUIREMENTS)],
        )
    else:
        print(f"WARNING: {REQUIREMENTS} not found, skipping dependency install.")

    print(f"\nSetup complete. Venv Python: {vpy}")
    return Path(vpy)


if __name__ == "__main__":
    try:
        setup()
    except subprocess.CalledProcessError as exc:
        print(
            f"\nERROR: Command failed (exit {exc.returncode}):\n"
            f"  {' '.join(str(a) for a in exc.cmd)}\n\n"
            "Possible fixes:\n"
            "  - Check your internet connection\n"
            "  - Ensure Python venv support is installed:\n"
            "      sudo apt install python3.12-venv   (Debian/Ubuntu)\n"
            "  - Try running with a different Python version",
            file=sys.stderr,
        )
        sys.exit(1)
