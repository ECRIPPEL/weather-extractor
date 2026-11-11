"""ANA data access: the public ServiceANA web service, or HidroWebService with an ANA login."""

from concurrent.futures import ThreadPoolExecutor
import threading
import time
import xml.etree.ElementTree as ET

import pandas as pd

from .usgs_client import MAX_ATTEMPTS, UsgsClient


LEGACY_URL = "https://telemetriaws1.ana.gov.br/ServiceANA.asmx"
API_URL = "https://www.ana.gov.br/hidrowebservice/EstacoesTelemetricas"
API_SERIES = {1: "HidroSerieCotas", 2: "HidroSerieChuva", 3: "HidroSerieVazao"}
INVENTORY_PARAMS = [
    "codEstDE", "codEstATE", "tpEst", "nmEst", "nmRio", "codSubBacia", "codBacia",
    "nmMunicipio", "nmEstado", "sgResp", "sgOper", "telemetrica",
]
# Long requests to ServiceANA can hit its database timeout (about 40 s).
LEGACY_CHUNK_YEARS = 20
# HidroWebService returns at most 366 days per request.
API_CHUNK_DAYS = 366
# The token is valid for 60 minutes; ANA blocks IPs that sign in too often.
TOKEN_SECONDS = 50 * 60
# Inventory replies take 1-15 s; a slow one is dropped rather than waited for.
INVENTORY_TIMEOUT = (15, 60)
# ServiceANA accepts about one request per second per IP (all methods, http and https
# alike), with a queue of about ten; faster requests get HTTP 429. Requests are sent at
# that rate, after a first burst, so none is refused.
LEGACY_RATE = 1.0
LEGACY_BURST = 8
# Requests ServiceANA refused (HTTP 429 "too many requests", or 500 under load) are asked
# again, after the whole batch, following these pauses in seconds.
RETRY_PAUSES = (15, 30, 60)
# A series request that fails twice (HTTP 429, or 500/504 and timeouts when the service is
# down) goes back to the queue for the retry rounds, which say so in the progress line.
SERIES_ATTEMPTS = 2
SERIES_TIMEOUT = (15, 90)


def _records(content, name):
    """Rows named ``name`` of a .NET DataSet XML reply, as dicts of field -> text."""
    root = ET.fromstring(content)
    return [
        {child.tag.rsplit("}", 1)[-1]: child.text for child in element}
        for element in root.iter()
        if element.tag.rsplit("}", 1)[-1] == name
    ]


class RatePacer:
    """Spaces requests to ``rate`` per second after a burst of ``burst``, across threads."""

    def __init__(self, rate, burst):
        self.interval = 1.0 / rate
        self.tolerance = (burst - 1) * self.interval
        self.lock = threading.Lock()
        self.next_slot = 0.0

    def delay(self, now=None):
        """Seconds to wait before sending one request; the slot is reserved."""
        with self.lock:
            now = time.monotonic() if now is None else now
            self.next_slot = max(self.next_slot, now)
            wait = max(0.0, self.next_slot - now - self.tolerance)
            self.next_slot += self.interval
            return wait


# One pacer for the whole QGIS session: the limit is per IP, for every tab and run.
LEGACY_PACER = RatePacer(LEGACY_RATE, LEGACY_BURST)


def run_with_retries(jobs, run, workers, wait, on_retry=None, pauses=RETRY_PAUSES):
    """run(job) for every job, ``workers`` at a time. Jobs that fail are asked again after
    each pause, all together, so a refused request is not lost.

    Returns the results, the jobs that still failed, and the last error of each job.
    """
    results, errors, pending = [], {}, list(jobs)
    for pause in (0,) + tuple(pauses):
        if not pending:
            break
        if pause:
            if on_retry:
                on_retry(len(pending), pause)
            wait(pause)
        failed = []
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(run, job): job for job in pending}
            for future, job in futures.items():
                try:
                    results.append(future.result())
                except InterruptedError:
                    raise
                except Exception as exc:
                    failed.append(job)
                    errors[job] = exc
        pending = failed
    return results, pending, errors


def legacy_chunks(start, end, years=LEGACY_CHUNK_YEARS):
    first, last = pd.Timestamp(start), pd.Timestamp(end)
    while first <= last:
        stop = min(first + pd.DateOffset(years=years) - pd.Timedelta(days=1), last)
        yield first, stop
        first = stop + pd.Timedelta(days=1)


def api_chunks(start, end, days=API_CHUNK_DAYS):
    first, last = pd.Timestamp(start), pd.Timestamp(end)
    while first <= last:
        stop = min(first + pd.Timedelta(days=days - 1), last)
        yield first, stop
        first = stop + pd.Timedelta(days=1)


class HidrowebClient(UsgsClient):
    """ServiceANA by default; HidroWebService when an ANA user (CPF/CNPJ) and password are given."""

    def __init__(self, user="", password="", is_canceled=None, sleep=time.sleep):
        super().__init__("", is_canceled, sleep)
        self.user = (user or "").strip()
        self.password = password or ""
        self._token = None
        self._token_time = 0.0
        self._token_lock = threading.Lock()

    @property
    def uses_api(self):
        return bool(self.user and self.password)

    def wait(self, seconds):
        """Pause, still answering Cancel."""
        self._wait(seconds)

    def request(self, url, *args, **kwargs):
        if url.startswith(LEGACY_URL):
            self._wait(LEGACY_PACER.delay())
        return super().request(url, *args, **kwargs)

    # -- ServiceANA (public) -------------------------------------------------

    def inventory(self, attempts=MAX_ATTEMPTS, timeout=INVENTORY_TIMEOUT, **filters):
        """Stations of HidroInventario matching the filters (station code range, type)."""
        params = dict.fromkeys(INVENTORY_PARAMS, "")
        params.update({key: str(value) for key, value in filters.items()})
        reply = self.request(f"{LEGACY_URL}/HidroInventario", params, attempts=attempts, timeout=timeout)
        return _records(reply.content, "Table")

    def series(self, code, data_type, start, end):
        """Monthly records (one value field per day) of one station and data type."""
        if self.uses_api:
            return self._api_series(code, data_type, start, end)
        records = []
        for first, last in legacy_chunks(start, end):
            params = {
                "codEstacao": code,
                "dataInicio": f"{first:%d/%m/%Y}",
                "dataFim": f"{last:%d/%m/%Y}",
                "tipoDados": data_type,
                "nivelConsistencia": "",
            }
            reply = self.request(f"{LEGACY_URL}/HidroSerieHistorica", params, attempts=SERIES_ATTEMPTS,
                                 timeout=SERIES_TIMEOUT)
            # "Sem dados..." replies hold an ErrorTable and no SerieHistorica rows.
            records += _records(reply.content, "SerieHistorica")
        return records

    # -- HidroWebService (ANA login) ----------------------------------------

    def token(self):
        with self._token_lock:
            if self._token and time.monotonic() - self._token_time < TOKEN_SECONDS:
                return self._token
            reply = self.request(
                f"{API_URL}/OAUth/v1", headers={"Identificador": self.user, "Senha": self.password}
            )
            items = (reply.json() or {}).get("items") or {}
            token = items.get("tokenautenticacao") if isinstance(items, dict) else None
            if not token:
                raise RuntimeError(
                    "HidroWebService sign-in failed: check the CPF/CNPJ and the password sent by ANA."
                )
            self._token, self._token_time = token, time.monotonic()
            return token

    def _api_series(self, code, data_type, start, end):
        records = []
        for first, last in api_chunks(start, end):
            params = {
                "Código da Estação": code,
                "Tipo Filtro Data": "DATA_LEITURA",
                "Data Inicial (yyyy-MM-dd)": f"{first:%Y-%m-%d}",
                "Data Final (yyyy-MM-dd)": f"{last:%Y-%m-%d}",
            }
            reply = self.request(
                f"{API_URL}/{API_SERIES[data_type]}/v1", params,
                headers={"Authorization": f"Bearer {self.token()}"}, empty_status=(404,),
                attempts=SERIES_ATTEMPTS, timeout=SERIES_TIMEOUT,
            )
            items = (reply.json() or {}).get("items") if reply is not None else None
            if isinstance(items, list):
                records += [item for item in items if isinstance(item, dict)]
        return records
