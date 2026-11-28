"""
src/shard_store.py
Storage for the lake's two layers: raw payloads and built matches.

Both layers live in gzipped tar archives that are split into a fixed number
of shards. A match always belongs to the same shard, so a shard can be
fetched, verified and replaced on its own.

The catalog decides which archive holds a match and what the digest of its
member must be. Nothing here infers precedence from a file name or from the
order archives happen to be unpacked in: a member is only accepted when it
matches the digest the catalog recorded for it.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
import zlib
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from src.catalog_db import NUM_SHARDS
from src.commentary import bundle_sha256_from_members, raw_payload_files

#: The layers the lake stores.
LAYER_RAW = "raw"
LAYER_BUILT = "built"


def shard_id(match_id: str, num_shards: int = NUM_SHARDS) -> int:
    """The shard a match belongs to.

    ESPN match ids are integers, so the shard is simply the id modulo the
    shard count. Any id that is not an integer falls back to a CRC32, which
    keeps the mapping stable without needing a different scheme.
    """
    text = str(match_id)
    try:
        return int(text) % num_shards
    except ValueError:
        return zlib.crc32(text.encode("utf-8")) % num_shards


def shard_name(layer: str, index: int, num_shards: int = NUM_SHARDS) -> str:
    """The archive name of one shard."""
    return f"{layer}_shard_{index:02d}.tar.gz"


def pack_name(layer: str, stamp: str) -> str:
    """The archive name of an incremental pack."""
    return f"{layer}_pack_{stamp}.tar.gz"


def sha256_bytes(payload: bytes) -> str:
    """The digest of a byte string."""
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    """The digest of a file's contents."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def member_digest(payload: bytes) -> str:
    """The digest of an archive member's uncompressed content.

    The catalog stores the digest of the content, not of the gzip wrapper,
    so a member can be verified after it is extracted.
    """
    return sha256_bytes(payload)


def members_by_match(layer: str, members: Dict[str, bytes]) -> Dict[str, Dict[str, bytes]]:
    """Group an archive's members by the match they belong to.

    A raw match occupies every ``<match_id>/...`` member in the archive: its
    scorecard, every commentary chunk, and its fetch metadata. A built match
    occupies ``<match_id>.json``. Grouping once keeps every later lookup O(1).
    """
    grouped: Dict[str, Dict[str, bytes]] = {}
    if layer == LAYER_RAW:
        for name, payload in members.items():
            if "/" not in name:
                continue
            match_id, base = name.split("/", 1)
            if not match_id or not base or base.startswith("._"):
                continue
            grouped.setdefault(match_id, {})[name] = payload
        return grouped
    for name, payload in members.items():
        if "/" in name or not name.endswith(".json"):
            continue
        grouped.setdefault(name[:-5], {})[name] = payload
    return grouped


def has_payload(layer: str, match_id: str, match_members: Dict[str, bytes]) -> bool:
    """Whether a match's member group holds the payload the layer requires."""
    if layer == LAYER_RAW:
        return f"{match_id}/scorecard.json.gz" in match_members
    return f"{match_id}.json" in match_members


def member_digest_for(layer: str, match_id: str, match_members: Dict[str, bytes]) -> Optional[str]:
    """The digest the catalog stores for a match's member group."""
    if layer == LAYER_RAW:
        return bundle_sha256_from_members(match_members, match_id)
    payload = match_members.get(f"{match_id}.json")
    return member_digest(payload) if payload is not None else None


def write_pack(
    out_path: str | Path,
    layer: str,
    members: Dict[str, bytes],
) -> Path:
    """Write a set of archive members into a gzipped tar archive.

    ``members`` maps an archive member name to its uncompressed content.
    The archive is written beside its destination and renamed into place, so
    a partial write is never published.
    """
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    os.close(handle)
    temp_path = Path(temp_name)
    try:
        with tarfile.open(temp_path, "w:gz") as archive:
            for name in sorted(members):
                payload = members[name]
                info = tarfile.TarInfo(name=name)
                info.size = len(payload)
                info.mtime = 0
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(payload))
        temp_path.replace(target)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return target


def read_pack(path: str | Path) -> Dict[str, bytes]:
    """Read every member of a gzipped tar archive into memory."""
    members: Dict[str, bytes] = {}
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            if not member.isfile():
                continue
            handle = archive.extractfile(member)
            if handle is None:
                continue
            members[member.name] = handle.read()
    return members


def collect_raw_members(raw_match_dirs: Dict[str, Path]) -> Dict[str, bytes]:
    """Gather the raw members of several match directories.

    Each match directory contributes its scorecard, every commentary chunk it
    holds, and its fetch metadata, so the raw layer keeps the complete
    evidence of a fetch. The ``*.json.gz`` payloads are stored uncompressed
    inside the archive, which is the convention both the migration and the
    ingest path already use.
    """
    members: Dict[str, bytes] = {}
    for match_id, directory in raw_match_dirs.items():
        directory = Path(directory)
        for path in raw_payload_files(directory):
            with gzip.open(path, "rb") as handle:
                members[f"{match_id}/{path.name}"] = handle.read()
        for name in ("fetch_meta.json", "meta.json"):
            metadata = directory / name
            if metadata.exists():
                members[f"{match_id}/{name}"] = metadata.read_bytes()
    return members


def catalog_targets(
    connection,
    wanted_ids: Iterable[str],
    layer: str = LAYER_RAW,
) -> Dict[str, List[Tuple[str, str]]]:
    """Group the wanted match ids by the archive that holds them.

    The catalog names the authoritative archive and the digest of each
    match's member. Grouping by archive means every archive is opened once,
    no matter how many matches it holds.
    """
    column = "raw_archive" if layer == LAYER_RAW else "built_archive"
    digest_column = "raw_sha256" if layer == LAYER_RAW else "built_sha256"
    targets: Dict[str, List[Tuple[str, str]]] = {}
    ids = [str(mid) for mid in wanted_ids]
    if not ids:
        return targets
    placeholders = ", ".join("?" for _ in ids)
    cursor = connection.execute(
        f"SELECT match_id, {column} AS archive, {digest_column} AS digest"
        f"  FROM matches WHERE match_id IN ({placeholders})",
        ids,
    )
    for row in cursor:
        archive = row["archive"]
        if not archive:
            continue
        targets.setdefault(str(archive), []).append((str(row["match_id"]), row["digest"]))
    return targets


def materialize_from_catalog(
    connection,
    wanted_ids: Iterable[str],
    archive_dir: str | Path,
    dest_dir: str | Path,
    layer: str = LAYER_RAW,
    verify: bool = True,
) -> Dict[str, Any]:
    """Extract the wanted matches from the archives the catalog names.

    Only the members the catalog points at are extracted, and their digests
    are checked. A member whose digest does not match is reported and left
    out, because a corrupted archive must never be mistaken for good data.
    """
    archive_root = Path(archive_dir)
    destination = Path(dest_dir)
    destination.mkdir(parents=True, exist_ok=True)

    targets = catalog_targets(connection, wanted_ids, layer)
    extracted: List[str] = []
    missing: List[str] = []
    mismatched: List[str] = []
    archives_opened: List[str] = []

    for archive_name, entries in sorted(targets.items()):
        archive_path = archive_root / archive_name
        if not archive_path.exists():
            missing.extend(match_id for match_id, _ in entries)
            continue
        archives_opened.append(archive_name)
        members = read_pack(archive_path)
        grouped = members_by_match(layer, members)
        for match_id, digest in entries:
            match_members = grouped.get(match_id) or {}
            if not has_payload(layer, match_id, match_members):
                missing.append(match_id)
                continue
            if verify and digest:
                actual = member_digest_for(layer, match_id, match_members)
                if actual != digest:
                    mismatched.append(match_id)
                    continue
            if layer == LAYER_RAW:
                target_dir = destination / match_id
                target_dir.mkdir(parents=True, exist_ok=True)
                for name, payload in match_members.items():
                    base = name.split("/", 1)[1]
                    if base.endswith(".json.gz"):
                        with gzip.open(target_dir / base, "wb") as out:
                            out.write(payload)
                    elif base in ("fetch_meta.json", "meta.json"):
                        (target_dir / base).write_bytes(payload)
            else:
                with open(destination / f"{match_id}.json", "wb") as out:
                    out.write(match_members[f"{match_id}.json"])
            extracted.append(match_id)

    return {
        "extracted": extracted,
        "missing": missing,
        "mismatched": mismatched,
        "archives_opened": archives_opened,
        "archive_count": len(archives_opened),
    }


def compact_shards(
    connection,
    layer: str,
    archive_dir: str | Path,
    out_dir: str | Path,
    pending_packs: Sequence[str],
    num_shards: int = NUM_SHARDS,
) -> Dict[str, Any]:
    """Fold the pending packs into the shards and repoint the catalog.

    Only the member the catalog names for a match survives compaction, so a
    pack that still holds an older copy of a match cannot overwrite the
    authoritative one. The catalog is updated in the same transaction, so a
    catalog never points at an archive that does not exist.
    """
    archive_root = Path(archive_dir)
    output_root = Path(out_dir)
    output_root.mkdir(parents=True, exist_ok=True)

    column = "raw_archive" if layer == LAYER_RAW else "built_archive"
    digest_column = "raw_sha256" if layer == LAYER_RAW else "built_sha256"

    # The authoritative member for every match, taken from the catalog.
    authoritative: Dict[str, Tuple[str, str]] = {}
    cursor = connection.execute(
        f"SELECT match_id, {column} AS archive, {digest_column} AS digest FROM matches"
        f" WHERE {column} IS NOT NULL"
    )
    for row in cursor:
        authoritative[str(row["match_id"])] = (str(row["archive"]), row["digest"])

    # Each distinct archive is read once, however many matches it holds. The
    # old version reopened a shard inside the per-match loop and re-parsed it
    # hundreds of times.
    archive_cache: Dict[str, Dict[str, Dict[str, bytes]]] = {}
    superseded: Set[str] = set()

    def grouped_members(archive_name: str) -> Dict[str, Dict[str, bytes]]:
        if archive_name not in archive_cache:
            archive_path = archive_root / archive_name
            if not archive_path.exists():
                superseded.add(archive_name)
                archive_cache[archive_name] = {}
            else:
                archive_cache[archive_name] = members_by_match(layer, read_pack(archive_path))
        return archive_cache[archive_name]

    # Group every match by the shard it belongs to.
    shard_members: Dict[int, Dict[str, bytes]] = {index: {} for index in range(num_shards)}
    placed: List[str] = []

    for match_id, (archive_name, digest) in authoritative.items():
        match_members = grouped_members(archive_name).get(match_id) or {}
        if not has_payload(layer, match_id, match_members):
            continue
        if digest and member_digest_for(layer, match_id, match_members) != digest:
            # The stored copy does not match the catalog; leave it alone
            # rather than promote a corrupt member.
            continue
        index = shard_id(match_id, num_shards)
        shard_members[index].update(match_members)
        placed.append(match_id)

    written: List[str] = []
    for index in range(num_shards):
        members = shard_members[index]
        if not members:
            continue
        name = shard_name(layer, index, num_shards)
        write_pack(output_root / name, layer, members)
        written.append(name)
        members.clear()

    # Repoint only the rows whose verified payload was actually written.
    for match_id in placed:
        index = shard_id(match_id, num_shards)
        name = shard_name(layer, index, num_shards)
        connection.execute(
            f"UPDATE matches SET {column} = ? WHERE match_id = ?",
            (name, match_id),
        )

    return {
        "shards_written": written,
        "rows_repointed": len(placed),
        "superseded_assets": sorted(superseded),
        "pending_packs": list(pending_packs),
    }


def write_raw_pack(
    out_path: str | Path,
    raw_match_dirs: Dict[str, Path],
) -> Tuple[Path, Dict[str, str]]:
    """Pack raw match directories into one archive.

    Returns the archive path and the full-bundle digest of each match, so the
    catalog records exactly what the archive holds: scorecard, every
    commentary chunk and the fetch metadata.
    """
    members = collect_raw_members(raw_match_dirs)
    path = write_pack(out_path, LAYER_RAW, members)
    grouped = members_by_match(LAYER_RAW, members)
    digests: Dict[str, str] = {}
    for match_id in raw_match_dirs:
        match_members = grouped.get(str(match_id)) or {}
        if not has_payload(LAYER_RAW, str(match_id), match_members):
            continue
        digests[str(match_id)] = member_digest_for(LAYER_RAW, str(match_id), match_members)
    return path, digests


def write_built_pack(
    out_path: str | Path,
    built_matches: Dict[str, bytes],
) -> Tuple[Path, Dict[str, str]]:
    """Pack built match JSONs into one archive, returning their digests.

    The keys are match ids. A key that already carries the ``.json``
    extension is accepted as well, so a caller cannot end up with a member
    named ``<id>.json.json`` and a catalog row that points at nothing.
    """
    members: Dict[str, bytes] = {}
    digests: Dict[str, str] = {}
    for match_id, payload in built_matches.items():
        stem = str(match_id)
        if stem.endswith(".json"):
            stem = stem[:-5]
        members[f"{stem}.json"] = payload
        digests[stem] = member_digest(payload)
    path = write_pack(out_path, LAYER_BUILT, members)
    return path, digests
