"""Python libraries of the AORC dataset, installed once with pip into the QGIS profile.

The plugin ZIP holds no compiled files (a rule of the QGIS plugin repository). QGIS
supplies numpy, pandas, shapely, requests, tzdata, and GDAL. Reading the AORC archive
also needs the libraries pinned in vendor-requirements.txt (xarray, zarr, numcodecs,
fsspec, aiohttp, and their helpers), which this module installs on first use. A
``vendor/`` folder next to this file (full ZIP, tests) is used instead when present.
"""

from importlib.util import find_spec
import importlib
from pathlib import Path
import subprocess
import sys
import time


PACKAGE_DIR = Path(__file__).resolve().parent
REQUIREMENTS = PACKAGE_DIR / "vendor-requirements.txt"
VENDOR_DIR = PACKAGE_DIR / "vendor"
# Modules the AORC reader imports; if any is missing, the pinned set is installed.
AORC_MODULES = ["xarray", "zarr", "numcodecs", "fsspec", "aiohttp", "asciitree"]
# Published on PyPI only as source; pure Python.
SOURCE_ONLY = {"asciitree"}
INSTALL_TIMEOUT = 900


def library_dir():
    """Folder of the installed libraries: the QGIS user profile, one per Python version."""
    try:
        from qgis.core import QgsApplication

        base = Path(QgsApplication.qgisSettingsDirPath())
    except Exception:
        base = Path.home() / ".weather_extractor"
    version = f"python{sys.version_info.major}{sys.version_info.minor}"
    return base / "weather_extractor" / "libraries" / version


def add_to_path():
    """Make vendor/ or the installed libraries importable, after the QGIS packages."""
    for folder in (VENDOR_DIR, library_dir()):
        if folder.is_dir() and str(folder) not in sys.path:
            sys.path.append(str(folder))
    importlib.invalidate_caches()


def missing(modules=AORC_MODULES):
    return [name for name in modules if find_spec(name) is None]


def pins():
    lines = (line.strip() for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def python_executable():
    """The Python of QGIS. Inside QGIS, sys.executable is QGIS itself, so look in sys.prefix."""
    candidates = [Path(sys.executable)] + [
        Path(sys.prefix) / name for name in ("python.exe", "python3.exe", "bin/python3", "bin/python")
    ]
    for path in candidates:
        if path.name.lower().startswith("python") and path.is_file():
            return str(path)
    raise RuntimeError("The Python of QGIS was not found, so the AORC libraries cannot be installed.")


def pip_commands(target, python=None):
    """pip runs that install the pinned set into ``target``: wheels first, then the source-only ones."""
    python = python or python_executable()
    base = [python, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--no-deps",
            "--upgrade", "--target", str(target)]
    pinned = pins()
    wheels = [pin for pin in pinned if pin.split("==")[0].lower() not in SOURCE_ONLY]
    sources = [pin for pin in pinned if pin not in wheels]
    commands = [base + ["--only-binary=:all:"] + wheels]
    if sources:
        commands.append(base + sources)
    return commands


def _run(command, is_canceled, popen):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console window on Windows
    process = popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, creationflags=flags)
    started = time.monotonic()
    while True:
        try:
            output, _ = process.communicate(timeout=1)
            return process.returncode, output or ""
        except subprocess.TimeoutExpired:
            if is_canceled() or time.monotonic() - started > INSTALL_TIMEOUT:
                process.kill()
                process.communicate()
                if is_canceled():
                    raise InterruptedError("Installation canceled.")
                raise RuntimeError("pip did not finish in 15 minutes.")


def install(target=None, progress=None, is_canceled=None, popen=subprocess.Popen):
    """Install the pinned AORC libraries with pip; returns the folder they went to."""
    target = Path(target or library_dir())
    is_canceled = is_canceled or (lambda: False)
    target.mkdir(parents=True, exist_ok=True)
    commands = pip_commands(target)
    for index, command in enumerate(commands):
        if progress:
            progress(10.0 + 80.0 * index / len(commands), "Installing the AORC libraries with pip (only once)...")
        code, output = _run(command, is_canceled, popen)
        if code:
            tail = "\n".join(output.strip().splitlines()[-8:])
            raise RuntimeError(
                "pip could not install the AORC libraries. Check the internet connection "
                f"(some networks block pypi.org), then press Run again.\n\n{tail}"
            )
    if str(target) not in sys.path:
        sys.path.append(str(target))
    importlib.invalidate_caches()
    still_missing = missing()
    if still_missing:
        raise RuntimeError("Installed, but these modules still cannot be loaded: " + ", ".join(still_missing))
    if progress:
        progress(100.0, "AORC libraries installed.")
    return target
