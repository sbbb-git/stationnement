"""Client HTTP commun : retries, timeouts, logs lisibles."""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

log = logging.getLogger("allovalet.http")

DEFAULT_TIMEOUT = 30
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4


class HttpClient:
    """requests.Session avec retry exponentiel sur erreurs réseau/5xx."""

    def __init__(self, base_url: str = "", timeout: int = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict | None = None,
        params: dict | None = None,
        json: Any = None,
        data: Any = None,
        expected: tuple[int, ...] | None = None,
        retry: bool = True,
    ) -> requests.Response:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        attempts = MAX_ATTEMPTS if retry else 1
        last_exc: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                resp = self.session.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json,
                    data=data,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:  # réseau / DNS / TLS
                last_exc = exc
                if attempt == attempts:
                    break
                delay = 2**attempt
                log.warning("%s %s → %s ; retry dans %ss", method, url, exc, delay)
                time.sleep(delay)
                continue

            log.debug("%s %s → %s", method, url, resp.status_code)

            if resp.status_code in RETRY_STATUSES and attempt < attempts:
                delay = 2**attempt
                log.warning(
                    "%s %s → HTTP %s ; retry dans %ss", method, url, resp.status_code, delay
                )
                time.sleep(delay)
                continue

            if expected and resp.status_code not in expected:
                from .errors import ApiError

                raise ApiError(f"{method} {url} inattendu", resp.status_code, resp.text)
            return resp

        from .errors import ApiError

        raise ApiError(f"{method} {url} injoignable : {last_exc}")

    def get(self, path: str, **kw) -> requests.Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw) -> requests.Response:
        return self.request("POST", path, **kw)

    def put(self, path: str, **kw) -> requests.Response:
        return self.request("PUT", path, **kw)
