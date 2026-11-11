"""Daily series offered in the HidroWeb Data tab (ANA, Brazilian hydrometeorological network)."""

DAILY = "daily"
COLUMNS = [DAILY]
COLUMN_LABELS = {DAILY: "Daily"}
COLUMN_TOOLTIPS = {DAILY: "Daily series; -99 marks missing days"}

# Station types of the ANA inventory (TipoEstacao), one map layer each.
STREAMFLOW, RAIN = "streamflow", "rain"
STATION_TYPES = {"1": STREAMFLOW, "2": RAIN}
LAYERS = (STREAMFLOW, RAIN)
LAYER_NAMES = {STREAMFLOW: "streamflow_stations", RAIN: "rain_gauges"}
SITE_TYPES = {STREAMFLOW: "Streamflow station", RAIN: "Rain gauge"}

# Missing day in every daily CSV.
MISSING_VALUE = -99


class Row:
    """One variable: its ANA data type code, value field prefix, unit, and inventory periods."""

    def __init__(self, key, label, data_type, prefix, unit, periods):
        self.key = key
        self.label = label
        # tipoDados of HidroSerieHistorica: 1 stage, 2 rainfall, 3 discharge.
        self.data_type = data_type
        # Daily fields of a monthly record: Vazao01..Vazao31 (Vazao_01 in HidroWebService).
        self.prefix = prefix
        self.unit = unit
        # Inventory fields Periodo<name>Inicio / Periodo<name>Fim that cover the variable.
        self.periods = periods

    @property
    def column(self):
        return f"{self.key}_{self.unit}"


ROWS = [
    Row("discharge", "Discharge (vazão)", 3, "Vazao", "m3s", ("DescLiquida",)),
    Row("stage", "Water level (cota)", 1, "Cota", "cm", ("Escala", "RegistradorNivel")),
    Row("precipitation", "Precipitation (chuva)", 2, "Chuva", "mm", ("Pluviometro", "RegistradorChuva")),
]
ROWS_BY_KEY = {row.key: row for row in ROWS}
