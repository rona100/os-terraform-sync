"""Thin HTTP client for the (mock) Oasis Platform REST API.

The assignment says: "Assume the Oasis Platform exposes a REST API for reading
and writing identity records." This client targets exactly that surface.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

DEFAULT_BASE_URL = os.environ.get("OASIS_API_URL", "http://127.0.0.1:8080")


class OasisClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout)

    # --- inventory ------------------------------------------------------
    def list_identities(self) -> list[dict[str, Any]]:
        r = self._client.get(f"{self.base_url}/api/v1/identities")
        r.raise_for_status()
        return r.json()["identities"]

    def sync_identities(self, changes: list[dict[str, Any]]) -> dict[str, Any]:
        """Upsert a batch of identity changes; returns a reconciliation report."""
        r = self._client.post(
            f"{self.base_url}/api/v1/identities:sync",
            json={"changes": changes},
        )
        r.raise_for_status()
        return r.json()

    # --- policy gate ----------------------------------------------------
    def review_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        r = self._client.post(
            f"{self.base_url}/api/v1/terraform/plan-review",
            json=payload,
        )
        r.raise_for_status()
        return r.json()

    def close(self) -> None:
        self._client.close()
