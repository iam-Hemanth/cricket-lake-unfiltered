"""
src/commentary.py
Commentary and Scorecard Retriever with Byte-for-Byte Raw Storage.
"""
import gzip
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.auth import generate_url_token
from src.client import CricinfoClient

BASE_URL = "https://hs-consumer-api.cricinfo.com"


def canonical_match_payload(payload: Optional[dict]) -> Optional[dict]:
    """Normalize the two shapes ESPN serves for a match page.

    Older stats pages put ``match`` and ``content`` at the top of
    ``appPageProps.data``. Newer pages wrap them one level deeper as
    ``{"data": {"match": ..., "content": ...}, "sponsoredFeatures": ...}``.
    The constructor only understands the flat shape, so unwrap the new one.
    """
    if not isinstance(payload, dict):
        return payload
    if isinstance(payload.get("content"), dict) and isinstance(payload.get("match"), dict):
        return payload
    nested = payload.get("data")
    if isinstance(nested, dict) and isinstance(nested.get("content"), dict):
        return nested
    return payload


def _atomic_write_bytes(path: Path, payload: bytes):
    """Write bytes to a temp file and atomically rename it into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(payload)
    tmp.replace(path)


def save_raw_payload(
    raw_match_dir: Path,
    filename: str,
    content_bytes: bytes,
    url: str,
    status_code: int
):
    """Save an unfiltered HTTP response body gzipped alongside metadata.

    The legacy per-file ``meta.json`` map is kept for backward compatibility
    with the batch tarballs already published. ``fetch_meta.json`` (written by
    :func:`write_fetch_meta`) is the authoritative bundle-level record used by
    the catalog to decide whether a match's raw payload actually changed.
    """
    raw_match_dir.mkdir(parents=True, exist_ok=True)
    gz_path = raw_match_dir / f"{filename}.gz"
    with gzip.open(gz_path, "wb") as f:
        f.write(content_bytes)

    meta_path = raw_match_dir / "meta.json"
    meta = {}
    if meta_path.exists():
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            pass

    sha256 = hashlib.sha256(content_bytes).hexdigest()
    meta[filename] = {
        "url": url,
        "status_code": status_code,
        "sha256": sha256,
        "bytes_length": len(content_bytes),
        "fetched_at": datetime.now(timezone.utc).isoformat()
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


def bundle_sha256(raw_match_dir: Path) -> str:
    """Deterministic digest over the raw files of a match bundle.

    The digest covers file names and their decompressed content, so a change
    in any stored payload changes the digest. Two fetches of an unchanged
    match therefore produce the same ``bundle_sha256``, which is what lets the
    catalog skip a rebuild.
    """
    digest = hashlib.sha256()
    for path in sorted(raw_match_dir.glob("*.json.gz")):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        try:
            with gzip.open(path, "rb") as f:
                digest.update(f.read())
        except OSError:
            digest.update(b"<unreadable>")
        digest.update(b"\0")
    return digest.hexdigest()


def write_fetch_meta(
    raw_match_dir: Path,
    match_id: str,
    extra: Optional[dict] = None
) -> dict:
    """Write ``fetch_meta.json`` describing the stored raw bundle.

    The file is the single source of truth for "did the raw payload change?"
    and for the innings-number -> commentary-key mapping that the offline
    re-parser needs to rebuild the same match the live extractor produced.
    """
    raw_match_dir.mkdir(parents=True, exist_ok=True)
    files: Dict[str, dict] = {}
    meta_path = raw_match_dir / "meta.json"
    if meta_path.exists():
        try:
            raw_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw_meta = {}
        if isinstance(raw_meta, dict):
            for name, record in raw_meta.items():
                if not isinstance(record, dict):
                    continue
                files[str(name)] = {
                    "url": record.get("url"),
                    "status_code": record.get("status_code"),
                    "sha256": record.get("sha256"),
                    "bytes_length": record.get("bytes_length"),
                }

    payload = {
        "match_id": str(match_id),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "bundle_sha256": bundle_sha256(raw_match_dir),
    }
    if extra:
        payload.update(extra)
    _atomic_write_bytes(
        raw_match_dir / "fetch_meta.json",
        json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"),
    )
    return payload

def fetch_match_details(
    client: CricinfoClient,
    match_id: str,
    series_id: Optional[str] = None,
    raw_dir: Optional[Path] = None,
    delay_sec: float = 0.5
) -> Tuple[Optional[dict], Optional[str]]:
    """Fetch complete match details and scorecard."""
    import re

    # If series_id is known, attempt consumer API
    if series_id:
        path = f"/v1/pages/match/details?lang=en&seriesId={series_id}&matchId={match_id}&latest=true"
        token = generate_url_token(path)
        url = f"{BASE_URL}{path}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Origin": "https://www.espncricinfo.com",
            "Referer": "https://www.espncricinfo.com/",
            "Accept": "application/json",
            "x-hsci-auth-token": token
        }
        r, err = client.get(url, headers=headers, delay_sec=delay_sec)
        if r and r.status_code == 200:
            content_bytes = r.content
            if raw_dir:
                save_raw_payload(raw_dir / str(match_id), "scorecard.json", content_bytes, url, r.status_code)
            try:
                data = json.loads(content_bytes.decode("utf-8"))
                return canonical_match_payload(data), None
            except Exception as e:
                return None, f"JSON parse error: {e}"

    # Primary / Fallback: Fetch via stats engine HTML (__NEXT_DATA__)
    engine_url = f"https://stats.espncricinfo.com/ci/engine/match/{match_id}.html"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }
    r, err = client.get(engine_url, headers=headers, delay_sec=delay_sec)
    if err or not r:
        return None, err or f"Failed fetching base match {match_id}"

    if '<script id="__NEXT_DATA__"' not in r.text:
        return None, f"No __NEXT_DATA__ script found in {engine_url}"

    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', r.text)
    if not m:
        return None, "Failed extracting __NEXT_DATA__ regex"

    try:
        full_data = json.loads(m.group(1))
        app_props = full_data.get("props", {}).get("appPageProps", {}).get("data", {})
        if not app_props:
            return None, "Empty appPageProps.data in Next.js payload"

        content_bytes = json.dumps(app_props, ensure_ascii=False).encode("utf-8")
        if raw_dir:
            save_raw_payload(raw_dir / str(match_id), "scorecard.json", content_bytes, engine_url, r.status_code)

        return canonical_match_payload(app_props), None
    except Exception as e:
        return None, f"JSON parse error from __NEXT_DATA__: {e}"


def fetch_innings_commentary(
    client: CricinfoClient,
    series_id: str,
    match_id: str,
    inn_num: int,
    raw_dir: Optional[Path] = None,
    delay_sec: float = 0.5,
    chunk_prefix: str = "commentary_inn",
    chunk_label: Optional[int] = None
) -> Tuple[List[dict], Optional[str]]:
    """
    Fetch complete over-by-over commentary for an innings.
    Saves unfiltered raw chunks to raw_dir if specified.

    ``chunk_prefix`` selects the on-disk naming scheme. Regulation innings use
    ``commentary_inn<N>_chunk<M>``; Super Over innings use
    ``commentary_super<N>_chunk<M>`` so a Super Over inning number can never
    overwrite a regulation chunk that happens to carry the same number.

    ``chunk_label`` is the number written into the file name. It defaults to
    the inning number used for the API call, but a Super Over innings is
    stored under its 0-based position in ``content.superOverInnings`` so the
    offline loader can restore the ``("super", index)`` key exactly.
    """
    all_chunks = []
    seen_overs = set()
    next_over = None
    chunk_idx = 0
    label = inn_num if chunk_label is None else chunk_label

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Origin": "https://www.espncricinfo.com",
        "Referer": "https://www.espncricinfo.com/",
        "Accept": "application/json"
    }

    while True:
        qs = f"?seriesId={series_id}&matchId={match_id}&inningNumber={inn_num}&commentType=ALL"
        if next_over is not None:
            qs += f"&fromInningOver={next_over}"
        path = f"/v1/pages/match/comments{qs}"
        headers["x-hsci-auth-token"] = generate_url_token(path)
        url = f"{BASE_URL}{path}"

        r, err = client.get(url, headers=headers, delay_sec=delay_sec)
        if err or not r:
            return all_chunks, err

        content_bytes = r.content
        if raw_dir:
            save_raw_payload(
                raw_dir / str(match_id),
                f"{chunk_prefix}{label}_chunk{chunk_idx}.json",
                content_bytes,
                url,
                r.status_code
            )
        chunk_idx += 1

        try:
            data = json.loads(content_bytes.decode("utf-8"))
        except Exception as e:
            return all_chunks, f"JSON parse error in commentary chunk: {e}"

        comments = data.get("comments") or []
        if not comments:
            break

        all_chunks.extend(comments)

        nxt = data.get("nextInningOver")
        if not nxt or nxt in seen_overs:
            break
        seen_overs.add(nxt)
        next_over = nxt

    return all_chunks, None


class FetchBundleError(Exception):
    """A fetch failed in a way that the caller must record."""

    def __init__(self, message: str, kind: str = "transient_fetch"):
        super().__init__(message)
        self.kind = kind


def fetch_match_bundle(
    client: CricinfoClient,
    match_id: str,
    raw_dir: Optional[Path] = None,
    delay_sec: float = 0.5,
) -> Tuple[Optional[dict], Dict[object, List[dict]], dict]:
    """Fetch a match and store every raw payload it produced.

    This is the only place the lake talks to ESPN. The raw payloads are
    written to ``raw_dir`` as they arrive, so a bundle that later fails to
    build is still recoverable offline. That is the property the whole
    raw-first design rests on: no fetch may be lost because the build failed.

    Returns ``(raw_details, commentary_chunks_by_inn, fetch_meta)``. On
    failure it raises :class:`FetchBundleError` with an explicit failure kind
    rather than returning an error string, so the caller never has to guess
    what went wrong from the wording of a message.
    """
    details, err = fetch_match_details(
        client=client,
        match_id=match_id,
        raw_dir=raw_dir,
        delay_sec=delay_sec,
    )
    if err or not details:
        raise FetchBundleError(f"Details fetch failed: {err}")

    match_info = details.get("match") or {}
    content = details.get("content") or {}
    series_id = str(
        (match_info.get("series") or {}).get("objectId")
        or (match_info.get("series") or {}).get("id")
        or ""
    )

    innings_list = content.get("innings") or []
    commentary_by_inn: Dict[object, List[dict]] = {}

    for inn_idx, inn in enumerate(innings_list, 1):
        inn_num = inn.get("inningNumber") or inn_idx
        chunks, c_err = fetch_innings_commentary(
            client=client,
            series_id=series_id,
            match_id=match_id,
            inn_num=inn_num,
            raw_dir=raw_dir,
            delay_sec=delay_sec,
        )
        if c_err:
            raise FetchBundleError(f"Commentary inn {inn_num} failed: {c_err}")
        commentary_by_inn[inn_idx] = chunks

    # Super Over innings are probed in fail-soft mode: ESPN serves no
    # deliveries for them, so an error leaves the scorecard-only line in
    # place and never fails the match.
    for so_idx, so_inn in enumerate(content.get("superOverInnings") or []):
        so_num = so_inn.get("inningNumber")
        if type(so_num) is not int or so_num < 1:
            so_num = len(innings_list) + so_idx + 1
        try:
            chunks, c_err = fetch_innings_commentary(
                client=client,
                series_id=series_id,
                match_id=match_id,
                inn_num=so_num,
                raw_dir=raw_dir,
                delay_sec=delay_sec,
                chunk_prefix="commentary_super",
                chunk_label=so_idx,
            )
        except Exception as exc:
            chunks, c_err = [], str(exc)
        if c_err:
            chunks = []
        commentary_by_inn[("super", so_idx)] = chunks

    fetch_meta: dict = {}
    if raw_dir is not None:
        fetch_meta = write_fetch_meta(
            raw_dir / str(match_id),
            str(match_id),
            extra={
                "series_id": series_id or None,
                "raw_innings_map": {
                    str(inn.get("inningNumber") or idx): idx
                    for idx, inn in enumerate(innings_list, 1)
                    if isinstance(inn, dict)
                },
            },
        )

    return details, commentary_by_inn, fetch_meta


def load_fetch_meta(raw_match_dir: Path) -> dict:
    """Read ``fetch_meta.json`` if the bundle carries one."""
    meta_path = raw_match_dir / "fetch_meta.json"
    if not meta_path.exists():
        return {}
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _regulation_key_map(raw_details: dict, fetch_meta: dict) -> Dict[int, int]:
    """Map the inning number used on the wire to the 1-based innings index.

    The constructor keys regulation commentary by the *position* of the
    innings in ``content.innings`` (``1, 2, ...``), while the commentary API
    is queried with the *inning number*. The two differ whenever ESPN skips
    or renumbers innings, so the mapping must be restored rather than assumed.
    """
    innings = ((raw_details or {}).get("content") or {}).get("innings") or []

    # 1. An explicit map recorded at fetch time is authoritative.
    explicit = (fetch_meta or {}).get("raw_innings_map")
    if isinstance(explicit, dict):
        resolved: Dict[int, int] = {}
        for number, index in explicit.items():
            try:
                resolved[int(number)] = int(index)
            except (TypeError, ValueError):
                continue
        if resolved:
            return resolved

    # 2. Fall back to matching the inning number against the scorecard.
    numbers = [
        inn.get("inningNumber") for inn in innings
        if isinstance(inn, dict)
    ]
    mapping: Dict[int, int] = {}
    for position, number in enumerate(numbers, 1):
        if type(number) is int and number >= 1 and number not in mapping:
            mapping[number] = position
    # 3. A number with no scorecard counterpart keeps its own position.
    for position, number in enumerate(numbers, 1):
        if type(number) is not int:
            mapping.setdefault(position, position)
    return mapping


def load_raw_match_payloads(raw_match_dir: Path) -> Tuple[Optional[dict], Dict[object, List[dict]], Optional[str]]:
    """
    Load raw match payloads from raw_match_dir (gzipped json files).
    Returns: (raw_details, commentary_chunks_by_inn, error_str)

    ``commentary_chunks_by_inn`` uses exactly the keys the constructor
    expects: ``1, 2, ...`` for regulation innings (1-based position in
    ``content.innings``) and ``("super", index)`` for Super Over innings
    (0-based position in ``content.superOverInnings``).
    """
    import re
    sc_path = raw_match_dir / "scorecard.json.gz"
    if not sc_path.exists():
        return None, {}, f"Scorecard file not found: {sc_path}"

    try:
        with gzip.open(sc_path, "rt", encoding="utf-8") as f:
            raw_details = canonical_match_payload(json.load(f))
    except Exception as e:
        return None, {}, f"Failed to read scorecard.json.gz: {e}"

    fetch_meta = load_fetch_meta(raw_match_dir)
    regulation_map = _regulation_key_map(raw_details, fetch_meta)

    commentary_by_inn: Dict[object, List[dict]] = {}
    for cf in sorted(raw_match_dir.glob("commentary_inn*_chunk*.json.gz")):
        m = re.search(r"commentary_inn(\d+)_chunk", cf.name)
        if not m:
            continue
        inn_num = int(m.group(1))
        key = regulation_map.get(inn_num, inn_num)
        try:
            with gzip.open(cf, "rt", encoding="utf-8") as f:
                chunk_data = json.load(f)
                comments = chunk_data.get("comments") or []
                commentary_by_inn.setdefault(key, []).extend(comments)
        except Exception as e:
            return raw_details, commentary_by_inn, f"Failed to read chunk {cf.name}: {e}"

    for cf in sorted(raw_match_dir.glob("commentary_super*_chunk*.json.gz")):
        m = re.search(r"commentary_super(\d+)_chunk", cf.name)
        if not m:
            continue
        so_num = int(m.group(1))
        key = ("super", so_num)
        try:
            with gzip.open(cf, "rt", encoding="utf-8") as f:
                chunk_data = json.load(f)
                comments = chunk_data.get("comments") or []
                commentary_by_inn.setdefault(key, []).extend(comments)
        except Exception as e:
            return raw_details, commentary_by_inn, f"Failed to read chunk {cf.name}: {e}"

    return raw_details, commentary_by_inn, None

