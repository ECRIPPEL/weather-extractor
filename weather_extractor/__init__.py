from .dependencies import add_to_path

# NOAA AORC support dependencies: vendor/ (full ZIP, tests) or first-use installation.
add_to_path()


def classFactory(iface):
    from .plugin import WeatherExtractorPlugin

    return WeatherExtractorPlugin(iface)
