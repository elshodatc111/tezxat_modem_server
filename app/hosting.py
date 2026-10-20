"""Hostingdagi Laravel gateway API bilan aloqa."""
from __future__ import annotations

import httpx


class HostingError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class HostingClient:
    def __init__(self, base_url: str, token: str, timeout: float = 30, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "sms-modem-server/1.0"},
            timeout=httpx.Timeout(timeout, connect=10),
            transport=transport,
            follow_redirects=False,
        )

    def close(self) -> None:
        self.client.close()

    def _request(self, method: str, path: str, json: dict | None = None, timeout: float | None = None) -> dict:
        try:
            r = self.client.request(method, path, json=json, timeout=timeout) if timeout else self.client.request(method, path, json=json)
        except httpx.HTTPError as e:
            raise HostingError(f"Ulanib bo'lmadi: {e.__class__.__name__}: {e}")
        try:
            data = r.json()
        except ValueError:
            raise HostingError(f"Hosting JSON qaytarmadi (HTTP {r.status_code}). URL to'g'ri ekanini tekshiring.", r.status_code)
        if r.status_code >= 400 or not data.get("success", False):
            err = (data.get("error") or {}) if isinstance(data, dict) else {}
            raise HostingError(f"HTTP {r.status_code}: {err.get('message') or err.get('code') or data}", r.status_code)
        return data

    def ping(self) -> dict:
        return self._request("GET", "/api/gateway/ping")

    def pull(self, limit: int, wait: int, worker: str) -> list[dict]:
        timeout = 30 + wait
        data = self._request("POST", "/api/gateway/pull", {"limit": limit, "wait": wait, "worker": worker}, timeout=timeout)
        return data.get("messages", [])

    def result(self, results: list[dict]) -> dict:
        return self._request("POST", "/api/gateway/result", {"results": results})

    def heartbeat(self, payload: dict) -> dict:
        return self._request("POST", "/api/gateway/heartbeat", payload)
