import hashlib
import json
import unittest

from datahub_official_macro import DataHubClient, DataHubError


class DataHubClientTests(unittest.TestCase):
    def setUp(self):
        self.bundle = {
            "schema_version": 3,
            "generated_at": "2026-11-30T03:17:00Z",
            "series": [
                {
                    "series_id": "CUUR0000SA0",
                    "source": "BLS",
                    "country": "US",
                    "label": "CPI",
                    "unit": "Index",
                    "points": [{"date": "2026-10", "value": 1.0}],
                },
                {
                    "series_id": "marts:sample",
                    "source": "Census Bureau",
                    "country": "US",
                    "dimensions": {"seasonally_adj": "yes"},
                    "points": [{"date": "2026-10", "value": 2.0}],
                },
            ],
            "jp_candidates": [{"target_id": "jp.candidate", "observations": [{"value": 10}]}],
        }
        self.bundle_bytes = json.dumps(self.bundle).encode("utf-8")
        self.manifest = {
            "generated_at": self.bundle["generated_at"],
            "distribution": {
                "release_id": "2026-11-30T03-17-00Z",
                "files": {
                    "official-bundle.json": {
                        "url": "https://data.example/releases/current/official-bundle.json",
                        "sha256": hashlib.sha256(self.bundle_bytes).hexdigest(),
                        "size_bytes": len(self.bundle_bytes),
                        "content_type": "application/json; charset=utf-8",
                    }
                },
            },
        }
        self.manifest_bytes = json.dumps(self.manifest).encode("utf-8")

    def client(self, bundle_bytes=None):
        payloads = {
            "https://data.example/latest/manifest.json": self.manifest_bytes,
            self.manifest["distribution"]["files"]["official-bundle.json"]["url"]:
                self.bundle_bytes if bundle_bytes is None else bundle_bytes,
        }
        return DataHubClient(
            "https://data.example/latest/manifest.json",
            fetch_bytes=lambda url, timeout: payloads[url],
        )

    def test_verifies_bundle_and_keeps_candidate_data_separate(self):
        snapshot = self.client().load_macro_snapshot()
        self.assertEqual([x["series_id"] for x in snapshot.official_series],
                         ["CUUR0000SA0", "marts:sample"])
        self.assertEqual(len(snapshot.estat_candidate_tables), 1)
        self.assertFalse(snapshot.context_summary()["estat_candidates_approved"])
        metadata = snapshot.metadata_rows()
        self.assertEqual(metadata[1]["seasonal_adjustment"], "yes")
        self.assertEqual(metadata[0]["start_date"], "2026-10")

    def test_rejects_checksum_mismatch(self):
        with self.assertRaisesRegex(DataHubError, "SHA-256"):
            self.client(self.bundle_bytes[:-1] + b"x").load_macro_snapshot()

    def test_rejects_non_https_manifest_url(self):
        with self.assertRaises(DataHubError):
            DataHubClient("http://data.example/latest/manifest.json")


if __name__ == "__main__":
    unittest.main()
