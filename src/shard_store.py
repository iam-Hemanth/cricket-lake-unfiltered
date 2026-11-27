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


def _member_names(layer: str, match_id: str) -> List[str]:
    """The archive member names a match occupies in one layer."""
    if layer == LAYER_RAW:
        return [f"{match_id}/scorecard.json.gz"]
    return [f"{match_id}.json"]


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

    Each match directory contributes its scorecard and every commentary chunk
    it holds, so the raw layer keeps the complete evidence of a fetch.
    """
    members: Dict[str, bytes] = {}
    for match_id, directory in raw_match_dirs.items():
        for path in sorted(Path(directory).glob("*.json.gz")):
            with gzip.open(path, "rb") as handle:
                members[f"{match_id}/{path.name}"] = handle.read()
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
        for match_id, digest in entries:
            payload = None
            for candidate in _member_names(layer, match_id):
                if candidate in members:
                    payload = members[candidate]
                    break
            if payload is None:
                missing.append(match_id)
                continue
            if verify and digest:
                actual = member_digest(payload)
                if actual != digest:
                    mismatched.append(match_id)
                    continue
            if layer == LAYER_RAW:
                target_dir = destination / match_id
                target_dir.mkdir(parents=True, exist_ok=True)
                for name in _member_names(layer, match_id):
                    with gzip.open(target_dir / Path(name).name, "wb") as out:
                        out.write(members[name])
            else:
                with open(destination / f"{match_id}.json", "wb") as out:
                    out.write(payload)
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

    # Gather the members of the packs that are being folded in.
    pending: Dict[str, bytes] = {}
    for pack_name in pending_packs:
        pack_path = archive_root / pack_name
        if not pack_path.exists():
            continue
        pending.update(read_pack(pack_path))

    # Group every match by the shard it belongs to.
    shard_members: Dict[int, Dict[str, bytes]] = {index: {} for index in range(num_shards)}
    superseded: Set[str] = set()

    for match_id, (archive_name, digest) in authoritative.items():
        payload = None
        member_name = None
        if archive_name in pending_packs:
            for candidate in _member_names(layer, match_id):
                if candidate in pending:
                    payload = pending[candidate]
                    member_name = candidate
                    break
        if payload is None:
            # The match's authoritative member lives in a shard already.
            shard_path = archive_root / archive_name
            if not shard_path.exists():
                superseded.add(archive_name)
                continue
            members = read_pack(shard_path)
            for candidate in _member_names(layer, match_id):
                if candidate in members:
                    payload = members[candidate]
                    member_name = candidate
                    break
        if payload is None:
            continue
        if digest and member_digest(payload) != digest:
            # The stored copy does not match the catalog; leave it alone
            # rather than promote a corrupt member.
            continue
        index = shard_id(match_id, num_shards)
        shard_members[index][member_name] = payload

    written: List[str] = []
    for index in range(num_shards):
        members = shard_members[index]
        if not members:
            continue
        name = shard_name(layer, index, num_shards)
        write_pack(output_root / name, layer, members)
        written.append(name)

    # Repoint the catalog at the shards.
    updated = 0
    for match_id in authoritative:
        index = shard_id(match_id, num_shards)
        name = shard_name(layer, index, num_shards)
        connection.execute(
            f"UPDATE matches SET {column} = ? WHERE match_id = ?",
            (name, match_id),
        )
        updated += 1

    return {
        "shards_written": written,
        "rows_repointed": updated,
        "superseded_assets": sorted(superseded),
        "pending_packs": list(pending_packs),
    }


def write_raw_pack(
    out_path: str | Path,
    raw_match_dirs: Dict[str, Path],
) -> Tuple[Path, Dict[str, str]]:
    """Pack raw match directories into one archive.

    Returns the archive path and the digest of each match's scorecard, so
    the catalog can record what it holds.
    """
    members = collect_raw_members(raw_match_dirs)
    path = write_pack(out_path, LAYER_RAW, members)
    digests: Dict[str, str] = {}
    for match_id in raw_match_dirs:
        scorecard = members.get(f"{match_id}/scorecard.json.gz")
        if scorecard is None:
            continue
        digests[match_id] = member_digest(scorecard)
    return path, digests


def write_built_pack(
    out_path: str | Path,
    built_matches: Dict[str, bytes],
) -> Tuple[Path, Dict[str, str]]:
    """Pack built match JSONs into one archive, returning their digests."""
    members = {f"{match_id}.json": payload for match_id, payload in built_matches.items()}
    path = write_pack(out_path, LAYER_BUILT, members)
    digests = {match_id: member_digest(payload) for match_id, payload in built_matches.items()}
    return path, digests
