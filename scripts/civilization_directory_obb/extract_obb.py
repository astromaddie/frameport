#!/usr/bin/env python3
"""Extract an unencrypted Android FAT OBB without kernel mounts or source writes."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import tempfile

SUFFIX = ".frameport-extracted"
MARKER = ".frameport-obb-ready"
ALIAS_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.obb$")

def metadata(source):
    before = source.stat()
    if not stat.S_ISREG(before.st_mode) or source.is_symlink():
        raise ValueError("source must be an ordinary file")
    with source.open("rb") as image:
        image.seek(-8, os.SEEK_END)
        footer_size, signature = struct.unpack("<II", image.read(8))
        if signature != 0x01059983 or not 24 <= footer_size <= 4096:
            raise ValueError("not a supported AOSP OBB footer")
        payload_length = before.st_size - footer_size - 8
        if payload_length < 512:
            raise ValueError("OBB payload too short")
        image.seek(payload_length)
        footer = image.read(footer_size)
        version, package_version, flags = struct.unpack_from("<III", footer)
        name_length = struct.unpack_from("<I", footer, 20)[0]
        if version != 1 or flags != 0 or 24 + name_length != footer_size:
            raise ValueError("only complete unencrypted v1 OBBs are supported")
        owner = footer[24:].decode("ascii")
        image.seek(0)
        boot = image.read(512)
        sector_size = struct.unpack_from("<H", boot, 11)[0]
        sectors = struct.unpack_from("<H", boot, 19)[0] or struct.unpack_from("<I", boot, 32)[0]
        if boot[510:512] != b"\x55\xaa" or sector_size != 512 or sectors * sector_size != payload_length:
            raise ValueError("FAT size/signature does not match OBB payload")
    return before, {"owner": owner, "package_version": package_version,
                    "payload_length": payload_length, "footer_sha256": hashlib.sha256(footer).hexdigest()}

def relative_path(raw):
    path = PurePosixPath(raw.lstrip("/"))
    if not path.parts or any(part in ("", ".", "..") for part in path.parts) or "\\" in raw or "\0" in raw:
        raise ValueError("unsafe FAT filename")
    if path.parts[0] == MARKER:
        raise ValueError("reserved marker path")
    return Path(*path.parts)


def validate_alias_name(alias_name, source_name):
    """Accept one ordinary OBB basename, never a path or the source's own name."""
    if (not isinstance(alias_name, str) or not ALIAS_NAME.fullmatch(alias_name)
            or alias_name == source_name):
        raise ValueError("alias must be a distinct direct .obb basename")
    return alias_name


def _check_existing_alias(alias_path, source):
    """Return whether alias_path is the exact safe relative link to source."""
    try:
        mode = alias_path.lstat().st_mode
    except FileNotFoundError:
        return False
    if not stat.S_ISLNK(mode):
        try:
            if os.path.samefile(alias_path, source):
                raise ValueError("alias path collides with the selected source on this filesystem")
        except FileNotFoundError:
            pass
        raise ValueError("alias path is already occupied by a non-symlink")
    target = os.readlink(alias_path)
    if target != source.name or Path(target).is_absolute() or "/" in target or "\\" in target:
        raise ValueError("existing alias must be a relative link to the selected source basename")
    try:
        resolved = alias_path.resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as error:
        raise ValueError("existing alias is dangling or unsafe") from error
    if resolved != source:
        raise ValueError("existing alias does not resolve to the selected source")
    return True


def _ensure_alias(alias_path, source):
    if _check_existing_alias(alias_path, source):
        return
    try:
        # symlink creation is atomic and fails rather than replacing any existing path.
        os.symlink(source.name, alias_path)
    except FileExistsError:
        if not _check_existing_alias(alias_path, source):
            raise ValueError("alias path changed while it was being created")
    if not _check_existing_alias(alias_path, source):
        raise ValueError("could not verify the new source alias")


def checked_cluster_chains(fat):
    """Reject malformed FAT cycles while retaining the pinned library's cluster reader."""
    original = fat.get_cluster_chain

    def checked(first_cluster):
        seen = set()
        for cluster in original(first_cluster):
            if cluster in seen:
                raise ValueError("cyclic FAT cluster chain")
            seen.add(cluster)
            yield cluster

    fat.get_cluster_chain = checked


def copy_entry(fat, entry, destination):
    """Copy a known directory entry directly, without repeated path resolution."""
    remaining = entry.filesize
    with destination.open("xb") as output:
        if remaining:
            for cluster in fat.get_cluster_chain(entry.get_cluster()):
                block = fat.read_cluster_contents(cluster)
                count = min(remaining, len(block))
                if not count:
                    raise ValueError("empty FAT data cluster")
                output.write(block[:count])
                remaining -= count
                if not remaining:
                    break
    if remaining:
        raise ValueError("FAT chain shorter than file length")


def extract(source, root, expected_owner, alias_name=None):
    try:
        from pyfatfs.PyFatFS import PyFatFS
    except ModuleNotFoundError as error:
        if error.name and error.name.startswith("pyfatfs"):
            raise RuntimeError("extracting OBBs requires the pinned pyfatfs host dependency") from error
        raise
    root = root.resolve(strict=True)
    if source.is_symlink():
        raise ValueError("symlink source rejected")
    source = source.resolve(strict=True)
    if source.parent != root or source.suffix != ".obb":
        raise ValueError("source is outside selected OBB folder")
    before, meta = metadata(source)
    if meta["owner"] != expected_owner:
        raise ValueError("unexpected OBB owner")
    if alias_name is not None:
        validate_alias_name(alias_name, source.name)
        alias_path = root / alias_name
        _check_existing_alias(alias_path, source)
    marker_text = f"{before.st_size}:{before.st_mtime_ns // 1000000}\n"
    output = Path(str(source) + SUFFIX)
    if output.exists() or output.is_symlink():
        marker = output / MARKER
        if (not output.is_symlink() and output.is_dir() and marker.is_file() and not marker.is_symlink()
                and marker.read_text() == marker_text):
            for child in output.rglob("*"):
                if child.is_symlink() or not (child.is_dir() or child.is_file()):
                    raise ValueError("existing output contains a symlink or special file")
            result = {"source": source.name, "output": str(output), "reused": True, **meta}
            if alias_name is not None:
                after = source.stat()
                if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                        after.st_size, after.st_mtime_ns, after.st_ino):
                    raise ValueError("source changed while validating ready extraction")
                _ensure_alias(alias_path, source)
                result["alias"] = alias_name
            return result
        raise ValueError("existing output lacks matching ready marker; refusing overwrite")
    stage = Path(tempfile.mkdtemp(prefix=".frameport-obb-stage-", dir=root))
    files = 0
    total = 0
    try:
        with PyFatFS(str(source), read_only=True) as fat:
            checked_cluster_chains(fat.fs)
            seen_directories = {fat.fs.root_dir.get_cluster()}
            # High-level path lookup linearly scans each FAT directory for every file. Walking parsed entries directly
            # keeps large shared-data folders proportional to their entry count.
            for folder, directories, entries in fat.fs.root_dir.walk():
                if folder != "/":
                    (stage / relative_path(folder)).mkdir(parents=True, exist_ok=True)
                for directory in directories:
                    cluster = directory.get_cluster()
                    if cluster in seen_directories:
                        raise ValueError("cross-linked or cyclic FAT directory")
                    seen_directories.add(cluster)
                    (stage / relative_path(folder.rstrip("/") + "/" + str(directory))).mkdir(
                        parents=True, exist_ok=True
                    )
                for entry in entries:
                    raw = folder.rstrip("/") + "/" + str(entry)
                    destination = stage / relative_path(raw)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    copy_entry(fat.fs, entry, destination)
                    destination.chmod(0o644)  # Android app must read host-created expansion assets.
                    length = destination.stat().st_size
                    if length != entry.filesize:
                        raise ValueError("extracted file length mismatch")
                    files += 1
                    total += length
                    if total > meta["payload_length"]:
                        raise ValueError("extracted length exceeds FAT image")
                    if files % 100 == 0:
                        print(json.dumps({"source": source.name, "files": files, "bytes": total}), flush=True)
        after = source.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise ValueError("source changed while extracting")
        marker = stage / MARKER
        marker.write_text(marker_text)
        marker.chmod(0o644)
        # mkdtemp creates 0700; publish only after completion with app-readable
        # directory permissions, including when the extractor has a restrictive umask.
        for directory in sorted(stage.rglob("*")):
            if directory.is_dir():
                directory.chmod(0o755)
        stage.chmod(0o755)
        os.rename(stage, output)
        result = {"source": source.name, "output": str(output), "reused": False,
                  "files": files, "bytes": total, **meta}
        if alias_name is not None:
            _ensure_alias(alias_path, source)
            result["alias"] = alias_name
        return result
    finally:
        if stage.exists():
            shutil.rmtree(stage)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--owner", required=True, help="exact package recorded in the AOSP OBB footer")
    parser.add_argument("--alias", help="optional direct .obb basename symlink to create after extraction validates")
    parser.add_argument("sources", type=Path, nargs="+")
    args = parser.parse_args()
    for source in args.sources:
        print(json.dumps(extract(source, args.root, args.owner, args.alias)), flush=True)
