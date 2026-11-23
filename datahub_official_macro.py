"""Read and verify the data-Hub public manifest and official macro bundle.

This adapter is intentionally read-only. It returns the existing BLS/Census series
with their original provenance and metadata intact; e-Stat candidate tables remain
separate and are never promoted to approved series.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class DataHubError(ValueError):
    """The manifest or downloaded data-Hub file is invalid."""


@dataclass(frozen=True)
class DataHubFile:
    name: str
    url: str
    sha256: str
    size_bytes: int
    content_type: str


@dataclass(frozen=True)
class MacroSnapshot:
    manifest: dict[str, Any]
    bundle: dict[str, Any]
    file: DataHubFile

    @property
    def generated_at(self) -> str:
        return str(self.bundle.get("generated_at", ""))

    @property
    def release_id(self) -> str:
        return str(self.manifest.get("distribution", {}).get("release_id", ""))

    @property
    def official_series(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            item for item in self.bundle.get("series", [])
            if item.get("country") == "US"
            and item.get("source") in {"BLS", "Census Bureau"}
        )

    @property
    def estat_candidate_tables(self) -> tuple[dict[str, Any], ...]:
        return tuple(self.bundle.get("jp_candidates", []))

    def metadata_rows(self) -> list[dict[str, Any]]:
        rows = []
        for item in self.official_series:
            points = item.get("points", [])
            dates = [str(point.get("date", "")) for point in points if point.get("date")]
            dimensions = item.get("dimensions", {})
            rows.append({
                "series_id": item.get("series_id", ""),
                "source": item.get("source", ""),
                "label": item.get("label", ""),
                "frequency": item.get("frequency") or item.get("period_frequency"),
                "unit": item.get("unit", ""),
                "seasonal_adjustment": (
                    item.get("seasonal_adjustment")
                    or dimensions.get("seasonally_adj")
                ),
                "start_date": min(dates) if dates else "",
                "end_date": max(dates) if dates else "",
                "observation_count": len(points),
                "metadata": item,
            })
        return rows

    def context_summary(self) -> dict[str, Any]:
        """Small, provenance-preserving context for reports; not a research calculation input."""
        return {
            "provider": "data-Hub",
            "generated_at": self.generated_at,
            "release_id": self.release_id,
            "bundle_sha256": self.file.sha256,
            "official_series_count": len(self.official_series),
            "estat_candidate_table_count": len(self.estat_candidate_tables),
            "estat_candidates_approved": False,
        }


def _https_url(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise DataHubError(f"{label} must be a URL string")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise DataHubError(f"{label} must be an HTTPS URL without embedded credentials")
    return value


def _http_get(url: str, timeout: float) -> bytes:
    request = Request(url, headers={"User-Agent": "data-hub-consumer/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise DataHubError(f"GET failed with HTTP {response.status}")
            return response.read()
    except (URLError, TimeoutError) as exc:
        raise DataHubError(f"GET failed: {type(exc).__name__}") from None


class DataHubClient:
    """Download the manifest and bundle, checking HTTPS, byte length, and SHA-256."""

    def __init__(
        self,
        manifest_url: str,
        *,
        timeout: float = 30,
        fetch_bytes: Callable[[str, float], bytes] | None = None,
    ) -> None:
        self.manifest_url = _https_url(manifest_url, "manifest_url")
        self.timeout = timeout
        self._fetch_bytes = fetch_bytes or _http_get

    def load_manifest(self) -> dict[str, Any]:
        raw = self._fetch_bytes(self.manifest_url, self.timeout)
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DataHubError("manifest is not valid UTF-8 JSON") from None
        if not isinstance(manifest, dict) or not manifest.get("generated_at"):
            raise DataHubError("manifest is missing generated_at")
        distribution = manifest.get("distribution")
        files = distribution.get("files") if isinstance(distribution, dict) else None
        if not isinstance(files, dict):
            raise DataHubError("manifest is missing distribution.files")
        return manifest

    def load_macro_snapshot(self) -> MacroSnapshot:
        manifest = self.load_manifest()
        files = manifest["distribution"]["files"]
        item = files.get("official-bundle.json")
        if not isinstance(item, dict):
            raise DataHubError("manifest does not list official-bundle.json")
        url = _https_url(item.get("url"), "official-bundle URL")
        expected_sha = item.get("sha256")
        expected_size = item.get("size_bytes")
        if not isinstance(expected_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha):
            raise DataHubError("official bundle SHA-256 is missing or malformed")
        if not isinstance(expected_size, int) or expected_size < 1:
            raise DataHubError("official bundle size_bytes is missing or invalid")

        raw = self._fetch_bytes(url, self.timeout)
        if len(raw) != expected_size:
            raise DataHubError("official bundle byte length does not match manifest")
        digest = hashlib.sha256(raw).hexdigest()
        if digest.lower() != expected_sha.lower():
            raise DataHubError("official bundle SHA-256 does not match manifest")
        try:
            bundle = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DataHubError("official bundle is not valid UTF-8 JSON") from None
        if not isinstance(bundle, dict) or bundle.get("schema_version") != 3:
            raise DataHubError("unsupported official bundle schema")
        if not isinstance(bundle.get("series"), list):
            raise DataHubError("official bundle series must be a list")

        series_file = DataHubFile(
            name="official-bundle.json",
            url=url,
            sha256=digest,
            size_bytes=len(raw),
            content_type=str(item.get("content_type", "")),
        )
        return MacroSnapshot(manifest=manifest, bundle=bundle, file=series_file)


def load_macro_snapshot(manifest_url: str, **kwargs: Any) -> MacroSnapshot:
    """Convenience wrapper for consumers that want one verified current snapshot."""
    return DataHubClient(manifest_url, **kwargs).load_macro_snapshot()
