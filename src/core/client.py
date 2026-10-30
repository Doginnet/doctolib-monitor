"""Resilient HTTP client for Doctolib Germany with Chrome TLS impersonation, CSRF warm-up, and anti-cache guarantees."""

from __future__ import annotations

import os
import random
import re
import time
from datetime import datetime
from typing import Any

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException
from loguru import logger

from config.settings import settings


CSRF_META_RE = re.compile(r'<meta\s+name=["\']csrf-token["\']\s+content=["\']([^"\']+)["\']', re.I)


class CircuitBreakerOpenError(RuntimeError):
    """Raised when consecutive anti-bot or network blocks exceed threshold."""


def sanitize_accept_language() -> str:
    """Prevents malformed POSIX LANG=en_US.UTF-8 environment variables from leaking into headers."""
    raw_lang = os.environ.get("DOCTOLIB_ACCEPT_LANGUAGE", "").strip()
    if raw_lang and "UTF" not in raw_lang.upper() and "_" not in raw_lang:
        return raw_lang
    return "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7"


class ResilientHttpClient:
    """Thread-isolated curl_cffi wrapper implementing Chrome 124 TLS, dynamic CSRF warm-up, and strict cache busting."""

    def __init__(
        self,
        base_url: str | None = None,
        impersonate: str | None = None,
        min_delay: float | None = None,
        max_delay: float | None = None,
    ) -> None:
        self.base_url = (base_url or settings.doctolib_base_url).rstrip("/")
        self.impersonate = impersonate or settings.doctolib_impersonate
        self.timeout = settings.doctolib_request_timeout
        self.min_delay = settings.doctolib_min_delay if min_delay is None else min_delay
        self.max_delay = settings.doctolib_max_delay if max_delay is None else max_delay
        self.max_retries = settings.doctolib_max_retries
        self.breaker_threshold = settings.doctolib_circuit_breaker_threshold

        self.accept_language = sanitize_accept_language()
        self.session = requests.Session(impersonate=self.impersonate)
        self.csrf_token: str | None = None
        self._warmed_up = False
        self._consecutive_failures = 0

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> ResilientHttpClient:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def polite_sleep(self) -> None:
        if self.max_delay > 0:
            delay = random.uniform(self.min_delay, max(self.min_delay, self.max_delay))
            time.sleep(delay)

    def warmup(self, force: bool = False) -> str | None:
        """Seeds Cloudflare & _doctolib_session cookies and extracts a fresh Rails x-csrf-token."""
        if self._warmed_up and not force and self.csrf_token:
            return self.csrf_token

        warmup_url = f"{self.base_url}/login"
        headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": self.accept_language,
            "cache-control": "no-cache",
            "pragma": "no-cache",
        }
        logger.debug(f"Warming up Doctolib session via {warmup_url} ({self.impersonate})")
        resp = self.session.get(warmup_url, headers=headers, timeout=self.timeout)
        if resp.status_code != 200:
            logger.warning(f"Session warm-up returned HTTP {resp.status_code}")
        match = CSRF_META_RE.search(resp.text)
        if match:
            self.csrf_token = match.group(1)
            logger.debug("Extracted fresh Doctolib x-csrf-token")
        self._warmed_up = True
        return self.csrf_token

    def _build_json_headers(self, referer_path: str = "/") -> dict[str, str]:
        """Constructs browser-accurate CORS headers with strict anti-cache directives (disabling 304 ETag caching)."""
        headers = {
            "accept": "application/json",
            "accept-language": self.accept_language,
            "content-type": "application/json; charset=utf-8",
            "cache-control": "no-cache, no-store, must-revalidate",
            "pragma": "no-cache",
            "expires": "0",
            "origin": self.base_url,
            "referer": f"{self.base_url}{referer_path}",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }
        if self.csrf_token:
            headers["x-csrf-token"] = self.csrf_token
        return headers

    def _dump_failed_payload(self, url: str, status_code: int, body: str) -> None:
        try:
            raw_dir = settings.doctolib_raw_dir
            raw_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_file = raw_dir / f"failed_payload_{status_code}_{ts}.txt"
            out_file.write_text(f"URL: {url}\nSTATUS: {status_code}\n\n{body[:10000]}", encoding="utf-8")
        except Exception as dump_err:
            logger.debug(f"Could not write failed payload dump: {dump_err}")

    def request_json(
        self,
        method: str,
        path_or_url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        referer_path: str = "/",
        require_warmup: bool = True,
    ) -> Any:
        if self._consecutive_failures >= self.breaker_threshold:
            raise CircuitBreakerOpenError(
                f"Circuit breaker open after {self._consecutive_failures} consecutive failures."
            )

        if require_warmup and not self._warmed_up:
            self.warmup()

        url = path_or_url if path_or_url.startswith("http") else f"{self.base_url}{path_or_url}"
        csrf_refreshed = False

        for attempt in range(1, self.max_retries + 1):
            headers = self._build_json_headers(referer_path=referer_path)
            try:
                resp = self.session.request(
                    method=method.upper(),
                    url=url,
                    params=params,
                    json=json_body,
                    headers=headers,
                    timeout=self.timeout,
                )

                # Doctolib returns 200 OK for standard JSON and 206 Partial Content for paginated HCP search
                if resp.status_code in (200, 206):
                    self._consecutive_failures = 0
                    return resp.json()

                # Handle expired CSRF token once seamlessly
                if resp.status_code in (401, 403, 422) and not csrf_refreshed and method.upper() == "POST":
                    logger.warning(
                        f"HTTP {resp.status_code} on {url} — refreshing session & CSRF token..."
                    )
                    csrf_refreshed = True
                    self.warmup(force=True)
                    continue

                if resp.status_code in (403, 429, 500, 502, 503, 504):
                    self._consecutive_failures += 1
                    backoff = (1.6 ** attempt) + random.uniform(0.3, 0.9)
                    logger.warning(
                        f"HTTP {resp.status_code} on {url} (attempt {attempt}/{self.max_retries}). "
                        f"Backing off {backoff:.2f}s"
                    )
                    if attempt == self.max_retries:
                        self._dump_failed_payload(url, resp.status_code, resp.text)
                        resp.raise_for_status()
                    time.sleep(backoff)
                    continue

                self._dump_failed_payload(url, resp.status_code, resp.text)
                resp.raise_for_status()

            except RequestException as exc:
                self._consecutive_failures += 1
                backoff = (1.6 ** attempt) + random.uniform(0.3, 0.9)
                logger.warning(
                    f"Network error on {url} (attempt {attempt}/{self.max_retries}): {exc}"
                )
                if attempt == self.max_retries:
                    raise
                time.sleep(backoff)

        raise RuntimeError(f"Exhausted retries for {url}")
