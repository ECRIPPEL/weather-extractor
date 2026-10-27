"""HTTP access to the USGS Water Data APIs, USGS Samples, the Water Quality Portal, and NOAA NCEI."""

from io import StringIO
import threading
import time

import pandas as pd


WATERDATA_URL = "https://api.waterdata.usgs.gov/ogcapi/v1/collections"
SAMPLES_URL = "https://api.waterdata.usgs.gov/samples-data"
WQP_URL = "https://www.waterqualitydata.us/data"
NCEI_SEARCH_URL = "https://www.ncei.noaa.gov/access/services/search/v1/data"
NCEI_DATA_URL = "https://www.ncei.noaa.gov/access/services/data/v1"
NCEI_PAGE = 1000
SIGNUP_URL = "https://api.waterdata.usgs.gov/signup/"
USER_AGENT = "WeatherExtractor-QGIS/2.1.0"
TIMEOUT = (30, 300)  # (connect, read) seconds
MAX_ATTEMPTS = 5
MAX_WAIT_SECONDS = 60
PAGE_LIMIT = 10000
CSV_LIMIT = 50000  # Water Data API maximum per request
RETRY_STATUS = {429, 500, 502, 503, 504}


class UsgsClient:
    """Requests with retries, cancellation, and an optional USGS API key."""

    def __init__(self, api_key="", is_canceled=None, sleep=time.sleep):
        self.api_key = (api_key or "").strip()
        self.is_canceled = is_canceled or (lambda: False)
        self.sleep = sleep
        self._local = threading.local()

    def _session(self):
        import requests

        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers["User-Agent"] = USER_AGENT
            self._local.session = session
        return session

    def check_canceled(self):
        if self.is_canceled():
            raise InterruptedError("The download was canceled.")

    def _wait(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.check_canceled()
            self.sleep(min(0.5, max(0.0, deadline - time.monotonic())))

    def request(self, url, params=None, method="GET", timeout=TIMEOUT, attempts=MAX_ATTEMPTS):
        import requests

        headers = {}
        if self.api_key and url.startswith("https://api.waterdata.usgs.gov"):
            headers["X-Api-Key"] = self.api_key
        last_error = None
        rate_limited = False
        for attempt in range(attempts):
            self.check_canceled()
            try:
                response = self._session().request(
                    method, url, params=params, headers=headers, timeout=timeout
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = type(exc).__name__
                self._wait(min(2**attempt, MAX_WAIT_SECONDS))
                continue
            if response.status_code == 200:
                return response
            if response.status_code in RETRY_STATUS:
                last_error = f"HTTP {response.status_code}"
                rate_limited = rate_limited or response.status_code == 429
                retry_after = response.headers.get("Retry-After", "")
                wait = int(retry_after) if retry_after.isdigit() else 2**attempt
                self._wait(min(wait, MAX_WAIT_SECONDS))
                continue
            raise RuntimeError(_error_text(url, response))
        hint = ""
        if rate_limited and not self.api_key and url.startswith("https://api.waterdata.usgs.gov"):
            hint = f" A free API key ({SIGNUP_URL}) raises the request limit."
        raise RuntimeError(f"{_service_name(url)} did not respond ({last_error}).{hint}")

    # -- USGS Water Data API (OGC API Features) ---------------------------

    def features(self, collection, params):
        """All GeoJSON features of a query, following 'next' links."""
        url = f"{WATERDATA_URL}/{collection}/items"
        query = dict(params, f="json", limit=PAGE_LIMIT)
        features = []
        while url:
            page = self.request(url, query).json()
            features.extend(page.get("features", []))
            url = next(
                (link["href"] for link in page.get("links", []) if link.get("rel") == "next"),
                None,
            )
            query = None  # the next link already carries the query
        return features

    def csv(self, collection, params, sortby=None):
        """All rows of a CSV query, paged with offset (sorted for stable pages)."""
        url = f"{WATERDATA_URL}/{collection}/items"
        frames = []
        offset = 0
        while True:
            query = dict(params, f="csv", limit=CSV_LIMIT, offset=offset, skipGeometry="true")
            if sortby:
                query["sortby"] = sortby
            frame = _read_csv(self.request(url, query).text)
            if frame.empty:
                break
            frames.append(frame)
            if len(frame) < CSV_LIMIT:
                break
            offset += CSV_LIMIT
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    # -- USGS Samples -----------------------------------------------------

    def samples(self, endpoint, params, timeout=TIMEOUT, attempts=MAX_ATTEMPTS):
        """CSV from the USGS Samples service. ``params`` may repeat keys (list of pairs)."""
        params = list(params.items()) if isinstance(params, dict) else list(params)
        params.append(("mimeType", "text/csv"))
        response = self.request(f"{SAMPLES_URL}/{endpoint}", params, timeout=timeout, attempts=attempts)
        return _read_csv(response.text)

    # -- Water Quality Portal ---------------------------------------------

    def wqp(self, endpoint, params):
        query = dict(params, mimeType="csv")
        return _read_csv(self.request(f"{WQP_URL}/{endpoint}/search", query).text)

    # -- NOAA NCEI (GHCN-Daily and GHCNh) ----------------------------------

    def ncei_search(self, params):
        """All results of an NCEI search (one per station, or per station-year file), paged."""
        results, offset = [], 0
        while True:
            page = self.request(NCEI_SEARCH_URL, dict(params, limit=NCEI_PAGE, offset=offset)).json()
            batch = page.get("results", [])
            results.extend(batch)
            offset += len(batch)
            if not batch or offset >= int(page.get("count", 0) or 0):
                return results

    def ncei_data(self, params):
        return _read_csv(self.request(NCEI_DATA_URL, dict(params, format="csv")).text)


def _read_csv(text):
    if not text.strip():
        return pd.DataFrame()
    return pd.read_csv(StringIO(text), dtype=str, keep_default_na=False, low_memory=False)


def _service_name(url):
    if url.startswith(SAMPLES_URL):
        return "The USGS Samples service"
    if url.startswith(WQP_URL):
        return "The Water Quality Portal"
    if url.startswith("https://www.ncei.noaa.gov"):
        return "The NOAA NCEI service"
    return "The USGS Water Data API"


def _error_text(url, response):
    try:
        detail = response.json()
        detail = detail.get("description") or detail.get("detail") or str(detail)
    except ValueError:
        detail = response.text[:200]
    return f"{_service_name(url)} error {response.status_code}: {detail}".strip()
