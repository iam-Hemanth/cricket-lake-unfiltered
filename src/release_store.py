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
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

#: The repository the lake lives in.
DEFAULT_REPO = "iam-Hemanth/cricket-lake-unfiltered"

#: The releases the lake is split across.
RELEASE_RAW = "lake-raw"
RELEASE_BUILT = "lake-built"
RELEASE_CATALOG = "lake-catalog"
RELEASE_PUBLISHED = "data-unfiltered-master"

_USER_AGENT = "cricket-lake-unfiltered"


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
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            with open(temp_path, "wb") as out:
                shutil.copyfileobj(response, out)
        temp_path.replace(target)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return target


def list_assets(repo: str, release: str) -> List[Dict[str, Any]]:
    """The assets of a release, or an empty list when it does not exist."""
    url = f"https://api.github.com/repos/{repo}/releases/tags/{release}"
    request = urllib.request.Request(url, headers=_api_headers())
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []
        raise
    assets = payload.get("assets") or []
    return [
        {
            "name": asset.get("name"),
            "size": asset.get("size"),
            "digest": asset.get("digest"),
            "url": asset.get("browser_download_url"),
        }
        for asset in assets
    ]


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
) -> str:
    """Upload one file as a release asset.

    An asset with the same name is deleted first when clobbering is allowed,
    because the GitHub API refuses to overwrite an existing asset.
    """
    source = Path(path)
    name = asset_name or source.name
    if clobber:
        delete_asset(repo, release, name)

    url = f"https://uploads.github.com/repos/{repo}/releases/assets"
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
    request = urllib.request.Request(
        url, data=payload, headers=headers, method="POST"
    )
    request.add_header("X-GitHub-Asset-Name", name)
    with urllib.request.urlopen(request, timeout=600) as response:
        body = json.loads(response.read().decode("utf-8"))
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
        url = f"https://api.github.com/repos/{repo}/releases/assets/{_asset_id(repo, release, name)}"
        request = urllib.request.Request(url, headers=_api_headers(), method="DELETE")
        try:
            with urllib.request.urlopen(request, timeout=60):
                return True
        except urllib.error.HTTPError:
            return False
    return False


def _asset_id(repo: str, release: str, name: str) -> Optional[int]:
    """The id of one asset."""
    url = f"https://api.github.com/repos/{repo}/releases/tags/{release}"
    request = urllib.request.Request(url, headers=_api_headers())
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError:
        return None
    for asset in payload.get("assets") or []:
        if asset.get("name") == name:
            return asset.get("id")
    return None


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
