#!/usr/bin/env python3
"""Build a narrowly scoped, certificate-preserving Civilization VII OBB mount repair."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import zipfile

REPO = Path(__file__).resolve().parents[2]
if (REPO / "src" / "frameport").is_dir():
    sys.path.insert(0, str(REPO / "src"))

from frameport.apk.axml import ANDROID_NS, Axml, TYPE_INT_BOOLEAN, TYPE_STRING, component_filters  # noqa: E402

PACKAGE = "com.mrf.firaxis.invernessdemo"
OBB_OWNER = "com.firaxis.invernessdemo"
ACTIVITY = "frameport.civ.FrameNativeActivity"
NATIVE_ACTIVITY_NAME = "android.app.NativeActivity"
RUNTIME_CLASSES = {
    "Lframeport/civ/FrameNativeActivity;",
    "Lframeport/civ/FrameStorageManager;",
    "Lframeport/civ/DirectoryMount;",
    "Lframeport/civ/FrameClassLoaderParent;",
}
DEX_NAME = re.compile(r"^classes(?:(\d+))?\.dex$")


def _fail(message: str) -> None:
    raise ValueError(message)


def _uleb128(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data) and shift <= 28:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            return value, offset
        shift += 7
    _fail("invalid DEX ULEB128")


def dex_classes(data: bytes) -> dict[str, str | None]:
    """Return defined class descriptors and direct superclasses from a DEX file."""
    if len(data) < 112 or not data.startswith(b"dex\n"):
        _fail("invalid DEX header")
    file_size, header_size, endian = struct.unpack_from("<III", data, 32)
    if file_size != len(data) or header_size != 112 or endian != 0x12345678:
        _fail("unsupported or truncated DEX file")
    string_count, string_off = struct.unpack_from("<II", data, 56)
    type_count, type_off = struct.unpack_from("<II", data, 64)
    class_count, class_off = struct.unpack_from("<II", data, 96)
    if any(off > len(data) for off in (string_off, type_off, class_off)):
        _fail("DEX table offset exceeds file size")
    if string_count > (len(data) - string_off) // 4 or type_count > (len(data) - type_off) // 4:
        _fail("DEX identifier table exceeds file size")
    if class_count > (len(data) - class_off) // 32:
        _fail("DEX class table exceeds file size")

    string_offsets = struct.unpack_from(f"<{string_count}I", data, string_off) if string_count else ()

    def read_string(index: int) -> str:
        if index >= string_count:
            _fail("DEX descriptor references missing string")
        pos = string_offsets[index]
        if pos >= len(data):
            _fail("DEX string offset exceeds file size")
        _, pos = _uleb128(data, pos)  # UTF-16 length; descriptors are ASCII.
        end = data.find(b"\0", pos)
        if end < 0:
            _fail("unterminated DEX string")
        try:
            return data[pos:end].decode("ascii")
        except UnicodeDecodeError:
            return data[pos:end].decode("utf-8", "replace")

    type_descriptors: list[str] = []
    for index in range(type_count):
        descriptor_index = struct.unpack_from("<I", data, type_off + index * 4)[0]
        type_descriptors.append(read_string(descriptor_index))
    classes: dict[str, str | None] = {}
    for index in range(class_count):
        class_index, _, super_index = struct.unpack_from("<III", data, class_off + index * 32)
        if class_index >= len(type_descriptors):
            _fail("DEX class references missing type")
        descriptor = type_descriptors[class_index]
        parent = type_descriptors[super_index] if super_index != 0xFFFFFFFF else None
        if descriptor in classes:
            _fail(f"duplicate DEX class definition: {descriptor}")
        classes[descriptor] = parent
    return classes


def _dex_infos(infos: list[zipfile.ZipInfo]) -> list[tuple[int, zipfile.ZipInfo]]:
    result = []
    for info in infos:
        match = DEX_NAME.fullmatch(info.filename)
        if match:
            result.append((int(match.group(1) or "1"), info))
    result.sort(key=lambda item: item[0])
    numbers = [number for number, _ in result]
    if len(numbers) != len(set(numbers)):
        _fail("APK has duplicate DEX entry numbers")
    return result


def _main_activity(manifest: bytes, *, allow_repair_activity: bool = False) -> tuple[Axml, str, str, int]:
    axml = Axml(manifest)
    names = axml.strings()
    root = next((element for element in axml.elements() if element.name == "manifest"), None)
    package = axml.attr_str(root, "package") if root else None
    if package != PACKAGE:
        _fail(f"APK package must be {PACKAGE}; found {package!r}")
    main_components = []
    for element, name, items in component_filters(manifest):
        has_main = any(kind == "action" and names[index] == "android.intent.action.MAIN"
                       for kind, _, index in items if index < len(names))
        if has_main:
            main_components.append((element, name))
    if len(main_components) != 1 or main_components[0][0] != "activity":
        _fail("APK must declare exactly one MAIN activity; aliases, services, or multiple entries are unsupported")
    raw_name = main_components[0][1]
    if not raw_name:
        _fail("MAIN activity has no android:name")
    full_name = raw_name
    if raw_name.startswith("."):
        full_name = PACKAGE + raw_name
    elif "." not in raw_name:
        full_name = PACKAGE + "." + raw_name
    if full_name != NATIVE_ACTIVITY_NAME and not (allow_repair_activity and full_name == ACTIVITY):
        _fail(f"MAIN activity must directly name {NATIVE_ACTIVITY_NAME}; found {full_name!r}")
    activities = [element for element in axml.elements()
                  if element.name == "activity" and axml.attr_str(element, "name") == raw_name]
    if len(activities) != 1:
        _fail("could not locate MAIN activity declaration")
    activity = activities[0]
    name_index = axml.index("name")
    android_ns_index = axml.index(ANDROID_NS)
    attrs = [attr for attr in activity.attrs if attr.name == name_index and attr.ns == android_ns_index]
    if len(attrs) != 1 or attrs[0].dtype != TYPE_STRING:
        _fail("MAIN activity must have one android:name attribute")
    return axml, raw_name, full_name, attrs[0].offset


def _patch_manifest(manifest: bytes) -> bytes:
    axml, old_name, full_name, attr_offset = _main_activity(manifest)
    if full_name == ACTIVITY:
        _fail("MAIN activity already names the repair class")
    new_index = axml.index(ACTIVITY)
    old_size = len(axml.data)
    if new_index is None:
        new_index = axml.append_string(ACTIVITY)
    shift = len(axml.data) - old_size
    offset = attr_offset + shift
    struct.pack_into("<I", axml.data, offset + 8, new_index)
    if axml.data[offset + 15] == TYPE_STRING:
        struct.pack_into("<I", axml.data, offset + 16, new_index)
    names = axml.strings()
    android_ns = axml.index(ANDROID_NS)
    has_code_name = axml.index("hasCode")
    applications = [element for element in axml.elements() if element.name == "application"]
    if android_ns is None or has_code_name is None or len(applications) != 1:
        _fail("application must declare one safely editable android:hasCode boolean")
    has_code_attrs = [attr for attr in applications[0].attrs
                      if attr.name < len(names) and names[attr.name] == "hasCode"]
    if len(has_code_attrs) != 1 or has_code_attrs[0].ns != android_ns \
            or has_code_attrs[0].dtype != TYPE_INT_BOOLEAN:
        _fail("application must have one existing android:hasCode boolean; adding a missing attribute is unsupported")
    if axml.set_bool("application", "hasCode", True) != 1:
        _fail("could not safely set application android:hasCode=true")
    patched = axml.bytes()
    patched_axml, _, patched_name, _ = _main_activity(patched, allow_repair_activity=True)
    if patched_name != ACTIVITY:
        _fail("patched manifest did not select the repair activity")
    if patched_axml.get_bool("application", "hasCode") is not True:
        _fail("patched manifest did not set application android:hasCode=true")
    return patched


def _guard_repair_class_collisions(classes: dict[str, str | None]) -> None:
    collisions = sorted(name for name in classes if name.startswith("Lframeport/civ/"))
    if collisions:
        _fail(f"APK already defines repair namespace classes: {collisions[:8]}")


def _signature_entry(name: str) -> bool:
    upper = name.upper()
    if not upper.startswith("META-INF/"):
        return False
    leaf = upper.rsplit("/", 1)[-1]
    return (leaf == "MANIFEST.MF" or leaf.startswith("SIG-")
            or leaf.endswith((".SF", ".RSA", ".DSA", ".EC")))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _hash_zip_entry(apk: zipfile.ZipFile, info: zipfile.ZipInfo) -> str:
    digest = hashlib.sha256()
    with apk.open(info) as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _entry_hashes(apk_path: Path) -> dict[str, str]:
    with zipfile.ZipFile(apk_path) as apk:
        infos = apk.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            _fail("APK contains duplicate ZIP entry names")
        return {info.filename: _hash_zip_entry(apk, info) for info in infos if not _signature_entry(info.filename)}


def _check_required_tool(path: Path, label: str) -> Path:
    path = path.expanduser().resolve()
    if not path.is_file():
        _fail(f"{label} does not exist: {path}")
    return path


def _configure_jdk(javac: Path) -> None:
    version = subprocess.run([str(javac), "-version"], capture_output=True, text=True)
    text = version.stdout + version.stderr
    match = re.search(r"(?:javac\s+)?(\d+)(?:\.|$)", text.strip())
    if version.returncode or not match or int(match.group(1)) < 17:
        _fail(f"javac must be a working JDK 17 or newer executable: {text.strip()}")
    java_home = javac.resolve().parent.parent
    os.environ["JAVA_HOME"] = str(java_home)
    os.environ["PATH"] = str(java_home / "bin") + os.pathsep + os.environ.get("PATH", "")


def _run(command: list[str], label: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        detail = (result.stdout + result.stderr).strip()[-1600:]
        _fail(f"{label} failed with exit {result.returncode}: {detail}")
    return result


def _certificate_digest(apk: Path, apksigner: Path | None, frameport_signer=None) -> str:
    if frameport_signer is not None:
        digest = frameport_signer.cert_digest(apk)
    else:
        result = _run([str(apksigner), "verify", "--print-certs", str(apk)], "APK signature verification")
        match = re.search(r"certificate SHA-256 digest:\s*([0-9a-fA-F]+)", result.stdout + result.stderr, re.I)
        digest = match.group(1).lower() if match else None
    if not digest:
        _fail(f"APK has no verifiable signing certificate: {apk}")
    return digest.lower()


def _compile_helper(javac: Path, android_jar: Path, d8: Path, temp: Path) -> bytes:
    source_root = Path(__file__).resolve().parent
    stubs = source_root / "stubs"
    java_root = source_root / "java"
    temp.mkdir(parents=True)
    stub_classes = temp / "stub-classes"
    classes = temp / "classes"
    dex_out = temp / "dex"
    stub_classes.mkdir()
    classes.mkdir()
    dex_out.mkdir()
    stub_sources = sorted(stubs.rglob("*.java"))
    runtime_sources = sorted(java_root.rglob("*.java"))
    if len(runtime_sources) != 4 or not stub_sources:
        _fail("repair Java sources or compile-only stubs are incomplete")
    _run([str(javac), "-encoding", "UTF-8", "-source", "8", "-target", "8", "-classpath", str(android_jar),
          "-d", str(stub_classes), *map(str, stub_sources)], "compile Android API stubs")
    _run([str(javac), "-encoding", "UTF-8", "-source", "8", "-target", "8", "-classpath",
          os.pathsep.join((str(stub_classes), str(android_jar))), "-d", str(classes), *map(str, runtime_sources)],
         "compile directory OBB helper")
    classfiles = sorted(classes.rglob("*.class"))
    if not classfiles:
        _fail("javac produced no helper classes")
    _run([str(d8), "--min-api", "26", "--lib", str(android_jar), "--output", str(dex_out),
          *map(str, classfiles)], "convert helper classes to DEX")
    dex_files = sorted(dex_out.glob("classes*.dex"))
    if len(dex_files) != 1:
        _fail(f"D8 produced {len(dex_files)} DEX files; expected exactly one")
    definitions = dex_classes(dex_files[0].read_bytes())
    if set(definitions) != RUNTIME_CLASSES:
        _fail(f"helper DEX class set differs from the reviewed source: {sorted(definitions)}")
    return dex_files[0].read_bytes()


def _copy_entry(source: zipfile.ZipFile, destination: zipfile.ZipFile, info: zipfile.ZipInfo) -> str:
    new_info = zipfile.ZipInfo(info.filename, info.date_time)
    new_info.compress_type = info.compress_type
    new_info.comment = info.comment
    new_info.extra = info.extra
    new_info.create_system = info.create_system
    new_info.create_version = info.create_version
    new_info.extract_version = info.extract_version
    new_info.external_attr = info.external_attr
    new_info.internal_attr = info.internal_attr
    digest = hashlib.sha256()
    with source.open(info) as src, destination.open(new_info, "w") as dst:
        while block := src.read(1024 * 1024):
            digest.update(block)
            dst.write(block)
    return digest.hexdigest()


def _write_unsigned(source_path: Path, unsigned_path: Path, infos: list[zipfile.ZipInfo],
                    patched_manifest: bytes, new_dex_name: str, helper_dex: bytes) -> dict[str, str]:
    expected = {}
    with zipfile.ZipFile(source_path) as source, zipfile.ZipFile(unsigned_path, "w") as destination:
        for info in infos:
            if _signature_entry(info.filename):
                continue
            if info.filename == "AndroidManifest.xml":
                destination.writestr(info, patched_manifest)
                expected[info.filename] = hashlib.sha256(patched_manifest).hexdigest()
            else:
                expected[info.filename] = _copy_entry(source, destination, info)
        destination.writestr(new_dex_name, helper_dex, compress_type=zipfile.ZIP_DEFLATED)
        expected[new_dex_name] = hashlib.sha256(helper_dex).hexdigest()
    return expected


def _verify_payload(apk_path: Path, expected: dict[str, str]) -> None:
    actual = _entry_hashes(apk_path)
    if actual != expected:
        removed = sorted(set(expected) - set(actual))
        added = sorted(set(actual) - set(expected))
        changed = sorted(name for name in set(actual) & set(expected) if actual[name] != expected[name])
        _fail(f"signed APK payload differs outside the manifest and added DEX: removed={removed[:8]}, "
              f"added={added[:8]}, changed={changed[:8]}")


def _sign_with_key(unsigned: Path, signed: Path, apksigner: Path, keystore: Path, ks_pass: str,
                   alias: str, key_pass: str) -> None:
    _run([str(apksigner), "sign", "--ks", str(keystore), "--ks-pass", f"pass:{ks_pass}",
          "--ks-key-alias", alias, "--key-pass", f"pass:{key_pass}", "--alignment-preserved", "false",
          "--lib-page-alignment", "16", "--out", str(signed), str(unsigned)], "sign repaired APK")


def package(args: argparse.Namespace) -> dict[str, str]:
    input_path = args.input.expanduser().absolute()
    if input_path.is_symlink():
        _fail("input APK must be an ordinary file, not a symlink")
    source = input_path.resolve(strict=True)
    output = args.output.expanduser().absolute()
    if not source.is_file():
        _fail("input APK must be an ordinary file, not a symlink")
    if output == source or (output.exists() and output.resolve() == source):
        _fail("output path must differ from input APK")
    if output.is_symlink():
        _fail("output path must not be a symlink")
    if args.frameport_home is not None:
        os.environ["FRAMEPORT_HOME"] = str(args.frameport_home.expanduser().resolve())
    javac = _check_required_tool(args.javac, "javac")
    _configure_jdk(javac)
    android_jar = _check_required_tool(args.android_jar, "Android SDK android.jar")
    d8 = _check_required_tool(args.d8, "Android SDK d8")

    custom_signing = args.keystore is not None or args.apksigner is not None or args.ks_pass is not None \
        or args.ks_key_alias is not None or args.key_pass is not None
    frameport_signer = None
    apksigner = None
    keystore = None
    if custom_signing:
        if not all((args.keystore, args.apksigner, args.ks_pass, args.ks_key_alias, args.key_pass)):
            _fail("custom signing needs --apksigner, --keystore, --ks-pass, --ks-key-alias, and --key-pass")
        apksigner = _check_required_tool(args.apksigner, "apksigner")
        keystore = _check_required_tool(args.keystore, "signing keystore")
    else:
        # Load only the signer module here; absence of a saved per-package key is a hard error, never a new key.
        from frameport.apk import sign as frameport_sign
        from frameport.tools import overport

        keystore = overport.keystore(PACKAGE)
        if not keystore.is_file():
            _fail(f"FramePort has no existing signing key for {PACKAGE}: {keystore}")
        frameport_signer = frameport_sign

    source_cert = _certificate_digest(source, apksigner, frameport_signer)
    with zipfile.ZipFile(source) as apk:
        infos = apk.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            _fail("APK contains duplicate ZIP entry names")
        if "AndroidManifest.xml" not in names:
            _fail("APK is missing AndroidManifest.xml")
        manifest = apk.read("AndroidManifest.xml")
        dex_infos = _dex_infos(infos)
        if not dex_infos:
            _fail("APK has no classes*.dex entry")
        classes: dict[str, str | None] = {}
        for _, info in dex_infos:
            for descriptor, parent in dex_classes(apk.read(info)).items():
                if descriptor in classes:
                    _fail(f"APK has duplicate DEX class definition: {descriptor}")
                classes[descriptor] = parent
    _guard_repair_class_collisions(classes)
    patched_manifest = _patch_manifest(manifest)
    new_number = max(number for number, _ in dex_infos) + 1
    new_dex_name = f"classes{new_number}.dex" if new_number > 1 else "classes.dex"
    if new_dex_name in names:
        _fail(f"repair DEX entry collides with existing APK entry: {new_dex_name}")

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{output.name}.civ-repair-", dir=output.parent) as scratch:
        temp = Path(scratch)
        helper_dex = _compile_helper(javac, android_jar, d8, temp / "compile")
        unsigned = temp / "unsigned.apk"
        signed = temp / "signed.apk"
        expected = _write_unsigned(source, unsigned, infos, patched_manifest, new_dex_name, helper_dex)
        if custom_signing:
            _sign_with_key(unsigned, signed, apksigner, keystore, args.ks_pass, args.ks_key_alias, args.key_pass)
        else:
            frameport_signer.sign(unsigned, signed, PACKAGE)
        repaired_cert = _certificate_digest(signed, apksigner, frameport_signer)
        if repaired_cert != source_cert:
            _fail(f"signing certificate changed: input={source_cert}, output={repaired_cert}")
        _verify_payload(signed, expected)
        os.replace(signed, output)
    return {"input": str(source), "output": str(output), "package": PACKAGE, "activity": ACTIVITY,
            "added_dex": new_dex_name, "certificate_sha256": source_cert}


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="FramePort imported or built APK")
    parser.add_argument("--output", type=Path, required=True, help="new repaired APK path; input is read-only")
    parser.add_argument("--javac", type=Path, required=True, help="JDK javac executable")
    parser.add_argument("--android-jar", type=Path, required=True, help="SDK platform android.jar, API 26 or newer")
    parser.add_argument("--d8", type=Path, required=True, help="SDK build-tools d8 executable")
    parser.add_argument("--frameport-home", type=Path, help="FramePort data directory containing the existing package key")
    parser.add_argument("--apksigner", type=Path, help="apksigner executable for explicit existing-key signing")
    parser.add_argument("--keystore", type=Path, help="existing keystore matching the input APK certificate")
    parser.add_argument("--ks-pass", help="existing keystore password for explicit signing")
    parser.add_argument("--ks-key-alias", help="existing key alias for explicit signing")
    parser.add_argument("--key-pass", help="existing key password for explicit signing")
    return parser.parse_args()


if __name__ == "__main__":
    try:
        print(json.dumps(package(_arguments()), sort_keys=True))
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as error:
        print(f"package_directory_loader: {error}", file=sys.stderr)
        raise SystemExit(2)
