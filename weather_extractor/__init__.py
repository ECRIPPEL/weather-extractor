import sys
from pathlib import Path


_VENDOR_DIR = str(Path(__file__).resolve().parent / "vendor")
if Path(_VENDOR_DIR).is_dir() and _VENDOR_DIR not in sys.path:
	sys.path.append(_VENDOR_DIR)


def classFactory(iface):
	from .aorc_plugin import AORCWeatherPlugin

	return AORCWeatherPlugin(iface)
