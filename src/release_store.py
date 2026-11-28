"""
src/release_store.py
Reading and writing the lake's GitHub release assets.

The lake stores everything in GitHub releases: the raw payloads, the built
matches, the catalog and the published snapshot. This module is the only
place that talks to the GitHub API, so the rest of the pipeline never has to
know how an asset is fetched or uploaded.

Only the standard library is used. The GitHub API is reached with ``urllib``
and the token is read from the environment, which keeps the pipeline free of
a third-party dependency and of any credential stored in the repository.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, TypeVar

#: The repository the lake lives in.
DEFAULT_REPO = "iam-Hemanth/cricket-lake-unfiltered"

#: The releases the lake is split across.
RELEASE_RAW = "lake-raw"
RELEASE_BUILT = "lake-built"
RELEASE_CATALOG = "lake-catalog"
RELEASE_PUBLISHED = "data-unfiltered-master"

_USER_AGENT = "cricket-lake-unfiltered"

#: The HTTP statuses that are worth retrying. A 4xx other than 429 is a
#: request the server will keep rejecting, so it is raised immediately.
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_RETRY_ATTEMPTS = 4
_RETRY_BASE_SECONDS = 1.0
_RETRY_MAX_SECONDS = 30.0

_T = TypeVar("_T")


def _with_retries(operation: Callable[[], _T]) -> _T:
    """Run an HTTP operation, retrying transient failures with backoff."""
    delay = _RETRY_BASE_SECONDS
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            return operation()
        except urllib.error.HTTPError as exc:
            if exc.code not in _RETRY_STATUS or attempt == _RETRY_ATTEMPTS:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == _RETRY_ATTEMPTS:
                raise
        time.sleep(delay)
        delay = min(delay * 2, _RETRY_MAX_SECONDS)
    raise RuntimeError("unreachable")


def _token() -> Optional[str]:
    """The GitHub token, from the environment or from the gh CLI."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    if shutil.which("gh"):
        try:
            result = subprocess.run(
                ["gh", "auth", "token"], capture_output=True, text=True, check=False
            )
            if result.returncode == 0:
                return result.stdout.strip() or None
        except OSError:
            return None
    return None


def _api_headers() -> Dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": _USER_AGENT,
    }
    token = _token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def download_asset(
    repo: str,
    release: str,
    asset: str,
    dest_dir: str | Path,
    overwrite: bool = True,
) -> Path:
    """Download one release asset.

    The download goes to a temporary file and is renamed into place, so an
    interrupted download never leaves a half-written asset that a later step
    would treat as complete.
    """
    destination = Path(dest_dir)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / asset
    if target.exists() and not overwrite:
        return target

    url = f"https://github.com/{repo}/releases/download/{release}/{asset}"
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    handle, temp_name = tempfile.mkstemp(dir=str(destination), suffix=".tmp")
    os.close(handle)
    temp_path = Path(temp_name)

    def download() -> None:
        with urllib.request.urlopen(request, timeout=300) as response:
            with open(temp_path, "wb") as out:
                shutil.copyfileobj(response, out)

    try:
        _with_retries(download)
        temp_path.replace(target)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return target


def list_assets(repo: str, release: str) -> List[Dict[str, Any]]:
    """The assets of a release, or an empty list when it does not exist."""
    url = f"https://api.github.com/repos/{repo}/releases/tags/{release}"
    request = urllib.request.Request(url, headers=_api_headers())

    def fetch() -> Dict[str, Any]:
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {}
            raise

    payload = _with_retries(fetch)
    assets = payload.get("assets") or []
    return [
        {
            "id": asset.get("id"),
            "name": asset.get("name"),
            "size": asset.get("size"),
            "digest": asset.get("digest"),
            "updated_at": asset.get("updated_at"),
            "url": asset.get("browser_download_url"),
        }
        for asset in assets
    ]


def find_asset(repo: str, release: str, asset_name: str) -> Optional[Dict[str, Any]]:
    """One asset of a release, or None when it is not there."""
    for asset in list_assets(repo, release):
        if asset.get("name") == asset_name:
            return asset
    return None


def download_assets(
    repo: str,
    release: str,
    names: Iterable[str],
    dest_dir: str | Path,
) -> List[Path]:
    """Download several assets, skipping the ones already present."""
    downloaded: List[Path] = []
    for name in names:
        downloaded.append(download_asset(repo, release, name, dest_dir))
    return downloaded


def upload_asset(
    repo: str,
    release: str,
    path: str | Path,
    asset_name: Optional[str] = None,
    clobber: bool = True,
    expected_updated_at: Optional[str] = None,
) -> str:
    """Upload one file as a release asset.

    An asset with the same name is deleted first when clobbering is allowed,
    because the GitHub API refuses to overwrite an existing asset.

    ``expected_updated_at`` makes the upload optimistic: when the asset
    changed on the release since the caller read it, the upload fails closed
    rather than overwriting another run's catalog.
    """
    source = Path(path)
    name = asset_name or source.name
    current = find_asset(repo, release, name)
    if expected_updated_at is not None:
        if current is None:
            raise RuntimeError(
                f"{name} disappeared from {release} while this run was working; "
                "refusing to recreate it unguarded"
            )
        if current.get("updated_at") not in (None, expected_updated_at):
            raise RuntimeError(
                f"{name} changed on {release} while this run was working "
                f"(expected {expected_updated_at}, found {current.get('updated_at')}); "
                "refusing to overwrite it"
            )
    if clobber and current is not None:
        delete_asset(repo, release, name)

    # The upload needs the asset id, which comes from the release itself.
    release_payload = _release(repo, release)
    url = f"https://uploads.github.com/repos/{repo}/releases/{release_payload['id']}/assets"
    payload = source.read_bytes()
    headers = {
        "Content-Type": "application/octet-stream",
        "User-Agent": _USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = _token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def upload() -> Dict[str, Any]:
        request = urllib.request.Request(
            url, data=payload, headers=headers, method="POST"
        )
        request.add_header("X-GitHub-Asset-Name", name)
        with urllib.request.urlopen(request, timeout=600) as response:
            return json.loads(response.read().decode("utf-8"))

    body = _with_retries(upload)
    return str(body.get("browser_download_url") or name)


def _release(repo: str, release: str) -> Dict[str, Any]:
    """The release payload, creating the release when it does not exist."""
    url = f"https://api.github.com/repos/{repo}/releases/tags/{release}"
    request = urllib.request.Request(url, headers=_api_headers())
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    return _create_release(repo, release)


def _create_release(repo: str, release: str) -> Dict[str, Any]:
    """Create a release, if it does not exist yet."""
    url = f"https://api.github.com/repos/{repo}/releases"
    headers = _api_headers()
    headers["Content-Type"] = "application/json"
    body = json.dumps({
        "tag_name": release,
        "name": release,
        "body": "Managed by the cricket lake pipeline.",
        "draft": False,
        "prerelease": False,
    }).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def delete_asset(repo: str, release: str, name: str) -> bool:
    """Delete one asset from a release. Returns whether it existed."""
    for asset in list_assets(repo, release):
        if asset.get("name") != name:
            continue
        if asset.get("id") is None:
            return False
        url = f"https://api.github.com/repos/{repo}/releases/assets/{asset['id']}"
        request = urllib.request.Request(url, headers=_api_headers(), method="DELETE")

        def delete() -> bool:
            try:
                with urllib.request.urlopen(request, timeout=60):
                    return True
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return False
                raise

        return _with_retries(delete)
    return False


def extract_tar_gz(archive: str | Path, dest_dir: str | Path, members: Optional[Sequence[str]] = None) -> List[str]:
    """Extract a gzipped tar archive, optionally only some members."""
    destination = Path(dest_dir)
    destination.mkdir(parents=True, exist_ok=True)
    extracted: List[str] = []
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            if not member.isfile():
                continue
            if members is not None and member.name not in members:
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            target = destination / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "wb") as out:
                shutil.copyfileobj(handle, out)
            extracted.append(member.name)
    return extracted
