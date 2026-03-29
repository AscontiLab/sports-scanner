#!/usr/bin/env python3
"""Generische HTTP-Request-Retry-Logik fuer Scanner."""

import time

import requests


def request_with_retry(
    url: str,
    method: str = "GET",
    retries: int = 3,
    backoff: list | None = None,
    timeout: int = 30,
    **kwargs,
) -> requests.Response:
    """HTTP-Request mit Retry-Logik und exponentiellem Backoff."""
    if backoff is None:
        backoff = [2, 4, 8]

    last_error = None
    for attempt in range(retries):
        try:
            response = requests.request(method, url, timeout=timeout, **kwargs)
            response.raise_for_status()
            return response
        except Exception as exc:
            last_error = exc
            if attempt < retries - 1:
                wait = backoff[min(attempt, len(backoff) - 1)]
                print(f"    Retry ({attempt + 1}/{retries}): {exc} - warte {wait}s ...")
                time.sleep(wait)

    raise last_error
