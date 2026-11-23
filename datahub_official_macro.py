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


PUBLIC_REPOSITORY = "shihuiarenjinba-png/data-hub-distribution"
PUBLIC_LOOKBACK_UNIT = "calendar_months_anchored_per_series_to_actual_latest_observation"
PUBLIC_RELEASE_PATTERN = re.compile(r"^sample-[0-9]{8}T[0-9]{6}Z$")


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
        return str(self.manifest.get("release_id") or self.manifest.get("distribution", {}).get("release_id", ""))

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


def _public_asset_url(value: Any, release_id: str) -> str:
    """Accept only the official bundle asset from its matching public Release."""
    url = _https_url(value, "official bundle URL")
    parsed = urlsplit(url)
    expected_path = (
        f"/{PUBLIC_REPOSITORY}/releases/download/{release_id}/official-bundle.json"
    )
    if (
        parsed.hostname.lower() != "github.com"
        or parsed.path != expected_path
        or parsed.query
        or parsed.fragment
    ):
        raise DataHubError("official bundle URL is not the matching authoritative public Release asset")
    return url


def _validate_public_window(bundle: dict[str, Any], manifest: dict[str, Any]) -> None:
    """Reject a bundle whose observations exceed the public manifest's short window."""
    import calendar
    from datetime import date

    lookback = manifest.get("max_lookback", 3)
    if not isinstance(lookback, int) or lookback < 1 or lookback > 3:
        raise DataHubError("public manifest lookback is invalid")
    if manifest.get("e_stat_candidates_approved") is not False:
        raise DataHubError("e-Stat candidates must remain unapproved")

    collections = [(item.get("points", []), "series") for item in bundle.get("series", [])]
    collections.extend(
        (item.get("observations", []), "e-Stat candidate")
        for item in bundle.get("jp_candidates", [])
    )
    for points, label in collections:
        if not points:
            continue
        try:
            dates = [date.fromisoformat(str(point["date"])[:10]) for point in points]
        except (KeyError, ValueError, TypeError):
            raise DataHubError(f"{label} contains an invalid observation date") from None
        latest = max(dates)
        absolute = latest.year * 12 + latest.month - 1 - (lookback - 1)
        year, month0 = divmod(absolute, 12)
        month = month0 + 1
        cutoff = date(year, month, min(latest.day, calendar.monthrange(year, month)[1]))
        if min(dates) < cutoff or max(dates) > latest:
            raise DataHubError(f"{label} exceeds the public manifest period")


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
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
            raise DataHubError("unsupported public manifest schema")
        if not isinstance(manifest.get("published_at"), str):
            raise DataHubError("manifest is missing published_at")
        if manifest.get("entitlement_scope") != "public_sample":
            raise DataHubError("this adapter accepts public_sample manifests only")

        release_id = manifest.get("release_id")
        if not isinstance(release_id, str) or not PUBLIC_RELEASE_PATTERN.fullmatch(release_id):
            raise DataHubError("public release_id is missing or malformed")
        lookback = manifest.get("max_lookback")
        if type(lookback) is not int or not 1 <= lookback <= 3:
            raise DataHubError("public manifest exceeds the supported lookback cap")
        if manifest.get("max_lookback_unit") != PUBLIC_LOOKBACK_UNIT:
            raise DataHubError("public manifest lookback unit is missing or unsupported")
        if manifest.get("e_stat_candidates_approved") is not False:
            raise DataHubError("e-Stat candidates must remain unapproved")

        files = manifest.get("files")
        if not isinstance(files, dict):
            raise DataHubError("manifest is missing files")
        item = files.get("official-bundle.json")
        if not isinstance(item, dict):
            raise DataHubError("manifest does not list official-bundle.json")
        if item.get("entitlement_scope") != "public_sample":
            raise DataHubError("official bundle is not marked public_sample")
        if item.get("data_status") != "verified":
            raise DataHubError("official bundle is not marked verified")
        if not str(item.get("content_type", "")).lower().startswith("application/json"):
            raise DataHubError("official bundle content type is not JSON")
        _public_asset_url(item.get("url"), release_id)
        return manifest

    def load_macro_snapshot(self) -> MacroSnapshot:
        manifest = self.load_manifest()
        files = manifest.get("files") or manifest.get("distribution", {}).get("files", {})
        item = files.get("official-bundle.json")
        if not isinstance(item, dict):
            raise DataHubError("manifest does not list official-bundle.json")
        url = _public_asset_url(item.get("url"), str(manifest["release_id"]))
        expected_sha = item.get("sha256")
        expected_size = item.get("byte_size", item.get("size_bytes"))
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
        distribution = bundle.get("distribution")
        if not isinstance(distribution, dict):
            raise DataHubError("official bundle is missing distribution boundary metadata")
        if distribution.get("entitlement_scope") != "public_sample":
            raise DataHubError("official bundle entitlement scope is not public_sample")
        if distribution.get("max_lookback_months") != manifest["max_lookback"]:
            raise DataHubError("manifest and bundle lookback periods do not match")
        if distribution.get("estat_candidates_approved") is not False:
            raise DataHubError("official bundle cannot approve e-Stat candidates")
        _validate_public_window(bundle, manifest)

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
