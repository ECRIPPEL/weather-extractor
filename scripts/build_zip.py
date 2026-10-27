"""Build the QGIS plugin ZIP for Windows: dist/Weather_Extractor-<version>-win.zip.

    python scripts/build_zip.py            # reuse weather_extractor/vendor if present
    python scripts/build_zip.py --vendor   # rebuild weather_extractor/vendor first

vendor/ holds the Python libraries that QGIS for Windows lacks, pinned in
weather_extractor/vendor-requirements.txt and downloaded with pip for Windows and
Python 3.12 (the QGIS Python). It is not kept in git. The tests in tests/ need it too.
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


def build_zip():
    target = ROOT / "dist" / f"Weather_Extractor-{version()}-win.zip"
    target.parent.mkdir(exist_ok=True)
    count = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(PACKAGE.rglob("*")):
            relative = path.relative_to(ROOT)
            if path.is_dir() or SKIPPED & set(relative.parts) or path.suffix == ".pyc":
                continue
            archive.write(path, relative.as_posix())
            count += 1
        for name in ROOT_FILES:
            archive.write(ROOT / name, f"{PACKAGE.name}/{name}")
            count += 1
    print(f"{target.relative_to(ROOT)}: {count} files, {target.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vendor", action="store_true", help="rebuild weather_extractor/vendor with pip")
    options = parser.parse_args()
    if options.vendor or not VENDOR.is_dir():
        build_vendor()
    build_zip()
