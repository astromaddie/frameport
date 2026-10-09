"""Safe, metadata-only owner correction for unencrypted Android OBB v1 files."""
from __future__ import annotations

import shutil
import struct
from dataclasses import dataclass
from pathlib import Path

OBB_SIGNATURE = 0x01059983
OBB_VERSION = 1
OBB_OVERLAY = 1 << 0
OBB_SALTED = 1 << 1
_KNOWN_FLAGS = OBB_OVERLAY | OBB_SALTED
_FIXED_FOOTER_SIZE = 24
_MAX_PACKAGE_NAME_BYTES = 4096


class ObbFormatError(ValueError):
    """The file has an OBB footer that cannot be safely rewritten."""


@dataclass(frozen=True)
class ObbFooter:
    source_size: int
    footer_start: int
    footer_size: int
    version: int
    package_version: int
    flags: int
    salt: bytes
    owner: str


@dataclass(frozen=True)
class OwnerRewrite:
    source_size: int
    footer_start: int
    old_owner: str
    new_owner: str
    footer: bytes

    @property
    def output_size(self) -> int:
        return self.footer_start + len(self.footer)


def inspect_footer(path: Path) -> ObbFooter | None:
    """Read AOSP OBB v1 footer metadata; ordinary ZIP OBBs and files without its signature return None."""
    path = Path(path)
    file_size = path.stat().st_size
    if file_size < 8:
        return None
    with path.open("rb") as stream:
        stream.seek(file_size - 8)
        footer_size, signature = struct.unpack("<II", stream.read(8))
        if signature != OBB_SIGNATURE:
            return None
        if footer_size < _FIXED_FOOTER_SIZE or footer_size > file_size - 8:
            raise ObbFormatError("the OBB v1 footer bounds are invalid")
        footer_start = file_size - footer_size - 8
        stream.seek(footer_start)
        prefix = stream.read(_FIXED_FOOTER_SIZE)
        if len(prefix) != _FIXED_FOOTER_SIZE:
            raise ObbFormatError("the OBB v1 fixed footer is truncated")
        version, package_version, flags, salt, package_len = struct.unpack("<III8sI", prefix)
        if version != OBB_VERSION:
            raise ObbFormatError(f"unsupported Android OBB footer version {version}")
        if package_len == 0 or package_len > _MAX_PACKAGE_NAME_BYTES or footer_size != _FIXED_FOOTER_SIZE + package_len:
            raise ObbFormatError("the OBB v1 package-name length does not match its footer bounds")
        encoded_owner = stream.read(package_len)
        if len(encoded_owner) != package_len:
            raise ObbFormatError("the OBB v1 package name is truncated")
        try:
            owner = encoded_owner.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ObbFormatError("the OBB v1 package name is not valid UTF-8") from exc
        if not owner or "\0" in owner:
            raise ObbFormatError("the OBB v1 package name is empty or contains a NUL byte")
        return ObbFooter(file_size, footer_start, footer_size, version, package_version, flags, salt, owner)


def plan_owner_rewrite(path: Path, package: str) -> OwnerRewrite | None:
    """Return the replacement footer for a mismatched, unencrypted OBB v1; None means no rewrite is needed.

    Files without the AOSP OBB v1 signature (including ordinary ZIP OBBs) are left alone. Once the signature is
    recognized, malformed or unsupported metadata is an error instead of a best-effort edit.
    """
    try:
        owner_bytes = package.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ObbFormatError("the target package name is not valid UTF-8") from exc
    if not owner_bytes or b"\0" in owner_bytes or len(owner_bytes) > _MAX_PACKAGE_NAME_BYTES:
        raise ObbFormatError("the target package name is empty or outside the supported length")

    path = Path(path)
    footer = inspect_footer(path)
    if footer is None or footer.owner == package:
        return None
    if footer.flags & OBB_SALTED:
        raise ObbFormatError("the owner differs and the OBB_SALTED encryption flag is set; encrypted OBBs "
                             "cannot be retargeted safely")
    if footer.flags & ~_KNOWN_FLAGS:
        raise ObbFormatError(f"the owner differs and OBB flags 0x{footer.flags:x} include unsupported bits")

    replacement_size = _FIXED_FOOTER_SIZE + len(owner_bytes)
    encoded_footer = (struct.pack("<III8sI", footer.version, footer.package_version, footer.flags, footer.salt,
                                  len(owner_bytes)) + owner_bytes + struct.pack("<II", replacement_size, OBB_SIGNATURE))
    return OwnerRewrite(footer.source_size, footer.footer_start, footer.owner, package, encoded_footer)


def write_owner_rewrite(source: Path, destination: Path, rewrite: OwnerRewrite) -> int:
    """Copy an OBB and replace only its owner footer; never edit the supplied source file."""
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve():
        raise ValueError("OBB owner correction requires a separate destination file")
    current = plan_owner_rewrite(source, rewrite.new_owner)
    if current != rewrite:
        raise ObbFormatError("the OBB changed after its footer was inspected; retry the install")
    destination.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with source.open("rb") as src, destination.open("xb") as dst:
            created = True
            shutil.copyfileobj(src, dst, length=1 << 20)
        with destination.open("r+b") as stream:
            stream.seek(rewrite.footer_start)
            stream.write(rewrite.footer)
            stream.truncate(rewrite.output_size)
        shutil.copystat(source, destination)
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise
    return rewrite.output_size
