import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import release_store


class UploadTests(unittest.TestCase):
    def _upload(self, expected_updated_at=None):
        seen = []

        def fake_urlopen(request, timeout=None):
            seen.append(request)
            return io.BytesIO(b'{"browser_download_url": "https://example/u"}')

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cricket_lake_unfiltered_master.tar.gz"
            path.write_bytes(b"payload")
            with mock.patch.object(release_store, "find_asset", return_value=None), \
                    mock.patch.object(release_store, "_release", return_value={"id": 7}), \
                    mock.patch.object(release_store, "_token", return_value=None), \
                    mock.patch("urllib.request.urlopen", fake_urlopen):
                release_store.upload_asset(
                    "example/repo", "data-unfiltered-master", path,
                    expected_updated_at=expected_updated_at,
                )
        return seen[0]

    def test_the_asset_name_is_sent_as_the_api_requires(self):
        request = self._upload()
        self.assertIn("/releases/7/assets?name=cricket_lake_unfiltered_master.tar.gz",
                      request.full_url)
        self.assertEqual(request.get_header("X-github-asset-name"),
                         "cricket_lake_unfiltered_master.tar.gz")

    def test_a_guarded_upload_refuses_a_missing_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "asset.bin"
            path.write_bytes(b"payload")
            with mock.patch.object(release_store, "find_asset", return_value=None):
                with self.assertRaises(RuntimeError):
                    release_store.upload_asset(
                        "example/repo", "lake-catalog", path,
                        expected_updated_at="2026-11-28T00:00:00Z",
                    )


if __name__ == "__main__":
    unittest.main()
