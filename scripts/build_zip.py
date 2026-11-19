"""Build the QGIS plugin ZIP.

    python scripts/build_zip.py            # dist/Weather_Extractor-<version>.zip, no compiled files
    python scripts/build_zip.py --vendor   # (re)build weather_extractor/vendor, used by the tests
    python scripts/build_zip.py --full     # also dist/Weather_Extractor-<version>-win-full.zip, with vendor/

The plain ZIP is the one for the QGIS plugin repository, which does not accept compiled
files: the AORC libraries (weather_extractor/vendor-requirements.txt) are installed with
pip on first use (weather_extractor/dependencies.py). The full ZIP bundles them in vendor/
for computers that cannot reach pypi.org. vendor/ is downloaded with pip for Windows and
Python 3.12 (the QGIS Python) and is not kept in git.
"""

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "weather_extractor"
VENDOR = PACKAGE / "vendor"
REQUIREMENTS = PACKAGE / "vendor-requirements.txt"
# Published only as source; pure Python, so it installs on any platform.
SOURCE_ONLY = {"asciitree"}
# Copied from the repository root into the plugin folder of the ZIP.
ROOT_FILES = ["README.md", "LICENSE"]
SKIPPED = {"__pycache__"}
COMPILED = {".pyd", ".dll", ".so", ".dylib", ".exe", ".pyc"}


def pip(*args):
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                    "--no-deps", "--target", str(VENDOR), *args], check=True)


def build_vendor():
    lines = [line.strip() for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines()]
    pins = [line for line in lines if line and not line.startswith("#")]
    binary = [pin for pin in pins if pin.split("==")[0].lower() not in SOURCE_ONLY]
    source = [pin for pin in pins if pin not in binary]
    shutil.rmtree(VENDOR, ignore_errors=True)
    pip("--platform", "win_amd64", "--python-version", "3.12", "--implementation", "cp",
        "--only-binary=:all:", *binary)
    if source:
        pip(*source)
    # Test suites of the libraries and console scripts are not needed by the plugin.
    for path in list(VENDOR.glob("*/tests")) + [VENDOR / "bin"]:
        shutil.rmtree(path, ignore_errors=True)
    for path in VENDOR.rglob("__pycache__"):
        shutil.rmtree(path, ignore_errors=True)
    files = sum(1 for path in VENDOR.rglob("*") if path.is_file())
    print(f"vendor: {len(pins)} packages, {files} files")


def version():
    for line in (PACKAGE / "metadata.txt").read_text(encoding="utf-8").splitlines():
        if line.startswith("version="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("metadata.txt has no version.")


def build_zip(full=False):
    suffix = "-win-full" if full else ""
    target = ROOT / "dist" / f"Weather_Extractor-{version()}{suffix}.zip"
    target.parent.mkdir(exist_ok=True)
    count = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(PACKAGE.rglob("*")):
            relative = path.relative_to(ROOT)
            parts = relative.parts
            hidden = any(part.startswith(".") for part in parts)
            in_vendor = len(parts) > 1 and parts[1] == VENDOR.name
            if path.is_dir() or hidden or SKIPPED & set(parts) or path.suffix == ".pyc" or (in_vendor and not full):
                continue
            if not full and path.suffix.lower() in COMPILED:
                raise SystemExit(f"Compiled file outside vendor/: {relative}")
            archive.write(path, relative.as_posix())
            count += 1
        for name in ROOT_FILES:
            archive.write(ROOT / name, f"{PACKAGE.name}/{name}")
            count += 1
    print(f"{target.relative_to(ROOT)}: {count} files, {target.stat().st_size / 1e6:.2f} MB")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vendor", action="store_true", help="rebuild weather_extractor/vendor with pip")
    parser.add_argument("--full", action="store_true", help="also write the ZIP that bundles vendor/")
    options = parser.parse_args()
    if options.vendor or (options.full and not VENDOR.is_dir()):
        build_vendor()
    build_zip()
    if options.full:
        build_zip(full=True)
