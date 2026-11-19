from .dependencies import add_to_path

# The AORC libraries: vendor/ (full ZIP, tests) or the ones installed on first use.
add_to_path()


def classFactory(iface):
    from .aorc_plugin import AORCWeatherPlugin

    return AORCWeatherPlugin(iface)
