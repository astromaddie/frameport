from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
from unittest.mock import patch
import zipfile

import pytest

from scripts.civilization_directory_obb import package_directory_loader as repair

REPO = Path(__file__).resolve().parents[1]
SCRIPT_DIR = REPO / "scripts" / "civilization_directory_obb"
ANDROID_NS = "http://schemas.android.com/apk/res/android"
PACKAGE = "com.mrf.firaxis.invernessdemo"
OBB_OWNER = "com.firaxis.invernessdemo"


def _utf8(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return bytes((len(value), len(encoded))) + encoded + b"\0"


def _string_pool(strings: list[str]) -> bytes:
    data = bytearray()
    offsets = []
    for value in strings:
        offsets.append(len(data))
        data.extend(_utf8(value))
    data.extend(b"\0" * (-len(data) % 4))
    strings_start = 28 + 4 * len(strings)
    body = struct.pack(f"<{len(strings)}I", *offsets) + data
    return struct.pack("<HHIIIIII", 1, 28, 28 + len(body), len(strings), 0, 0x100, strings_start, 0) + body


def _namespace(chunk_type: int, prefix: int, uri: int) -> bytes:
    return struct.pack("<HHIII", chunk_type, 16, 24, 1, 0xFFFFFFFF) + struct.pack("<II", prefix, uri)


def _start(name_index: int, attrs: list[tuple[int, ...]]) -> bytes:
    size = 36 + 20 * len(attrs)
    node = struct.pack("<HHIII", 0x0102, 16, size, 1, 0xFFFFFFFF)
    node += struct.pack("<IIHHHHHH", 0xFFFFFFFF, name_index, 20, 20, len(attrs), 0, 0, 0)
    for attr in attrs:
        ns, name, value = attr[:3]
        dtype = attr[3] if len(attr) == 4 else repair.TYPE_STRING
        raw = 0xFFFFFFFF if dtype == repair.TYPE_INT_BOOLEAN else value
        node += struct.pack("<IIIHBBI", ns, name, raw, 8, 0, dtype, value)
    return node


def _end(name_index: int) -> bytes:
    return struct.pack("<HHIII", 0x0103, 16, 24, 1, 0xFFFFFFFF) + struct.pack("<II", 0xFFFFFFFF, name_index)


def _manifest(package: str = PACKAGE, activity_name: str = "android.app.NativeActivity",
              has_code: bool | None = False) -> bytes:
    strings = ["manifest", "application", "activity", "intent-filter", "action", "category", "package",
               "name", "android", ANDROID_NS, package, activity_name, "android.intent.action.MAIN",
               "android.intent.category.LAUNCHER", "hasCode"]
    ix = {value: index for index, value in enumerate(strings)}
    ns = ix[ANDROID_NS]
    chunks = [
        _namespace(0x0100, ix["android"], ns),
        _start(ix["manifest"], [(0xFFFFFFFF, ix["package"], ix[package])]),
        _start(ix["application"], [] if has_code is None else
               [(ns, ix["hasCode"], int(has_code), repair.TYPE_INT_BOOLEAN)]),
        _start(ix["activity"], [(ns, ix["name"], ix[activity_name])]),
        _start(ix["intent-filter"], []),
        _start(ix["action"], [(ns, ix["name"], ix["android.intent.action.MAIN"])]),
        _end(ix["action"]),
        _start(ix["category"], [(ns, ix["name"], ix["android.intent.category.LAUNCHER"])]),
        _end(ix["category"]),
        _end(ix["intent-filter"]),
        _end(ix["activity"]),
        _end(ix["application"]),
        _end(ix["manifest"]),
        _namespace(0x0101, ix["android"], ns),
    ]
    pool = _string_pool(strings)
    body = pool + b"".join(chunks)
    return struct.pack("<HHI", 0x0003, 8, 8 + len(body)) + body


def _run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    result = subprocess.run(command, capture_output=True, text=True, env=env)
    assert result.returncode == 0, f"{command[0]} failed: {result.stdout}\n{result.stderr}"


def _toolchain() -> dict[str, Path] | None:
    javac = _find_javac()
    roots = []
    for variable in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
        if os.environ.get(variable):
            roots.append(Path(os.environ[variable]))
    roots.append(Path.home() / "Library" / "Android" / "sdk")
    for sdk in roots:
        builds = sorted((sdk / "build-tools").glob("*"), reverse=True)
        platforms = sorted((sdk / "platforms").glob("android-*"), reverse=True)
        for build in builds:
            d8 = build / "d8"
            apksigner = build / "apksigner"
            if not d8.is_file() or not apksigner.is_file():
                continue
            for platform in platforms:
                android_jar = platform / "android.jar"
                if android_jar.is_file() and javac:
                    keytool = javac.resolve().parent / "keytool"
                    if not keytool.is_file():
                        return None
                    return {"javac": javac, "keytool": keytool, "android_jar": android_jar,
                            "d8": d8, "apksigner": apksigner}
    return None


def _find_javac() -> Path | None:
    candidates = []
    if os.environ.get("JAVA_HOME"):
        candidates.append(Path(os.environ["JAVA_HOME"]) / "bin" / "javac")
    candidates.append(Path("/opt/homebrew/opt/openjdk@17/bin/javac"))
    candidates.extend(Path("/opt/homebrew/Cellar/openjdk@17").glob("*/libexec/openjdk.jdk/Contents/Home/bin/javac"))
    if shutil.which("javac"):
        candidates.append(Path(shutil.which("javac")))
    for candidate in candidates:
        if candidate.is_file():
            version = subprocess.run([str(candidate), "-version"], capture_output=True, text=True)
            output = version.stdout + version.stderr
            match = re.search(r"(?:javac\s+)?(\d+)(?:\.|$)", output.strip())
            if version.returncode == 0 and match and int(match.group(1)) >= 17:
                return candidate
    return None


def _build_baseline_dex(temp: Path, tools: dict[str, Path]) -> bytes:
    source = temp / "KeepClass.java"
    source.write_text("package com.example;\npublic final class KeepClass {}\n")
    classes = temp / "native-classes"
    dex_dir = temp / "native-dex"
    classes.mkdir()
    dex_dir.mkdir()
    env = os.environ.copy()
    env["PATH"] = str(tools["javac"].resolve().parent) + os.pathsep + env.get("PATH", "")
    _run([str(tools["javac"]), "-source", "8", "-target", "8",
          "-d", str(classes), str(source)], env=env)
    _run([str(tools["d8"]), "--min-api", "26", "--lib", str(tools["android_jar"]), "--output", str(dex_dir),
          str(classes / "com" / "example" / "KeepClass.class")], env=env)
    return (dex_dir / "classes.dex").read_bytes()


def _signed_input(temp: Path, tools: dict[str, Path], keystore: Path, password: str) -> Path:
    manifest = _manifest()
    native_dex = _build_baseline_dex(temp, tools)
    unsigned = temp / "baseline-unsigned.apk"
    signed = temp / "baseline.apk"
    with zipfile.ZipFile(unsigned, "w") as apk:
        apk.writestr("AndroidManifest.xml", manifest, compress_type=zipfile.ZIP_DEFLATED)
        apk.writestr("classes.dex", native_dex, compress_type=zipfile.ZIP_DEFLATED)
        apk.writestr("assets/preserved.dat", bytes(range(256)) * 40, compress_type=zipfile.ZIP_STORED)
    env = os.environ.copy()
    env["PATH"] = str(tools["javac"].resolve().parent) + os.pathsep + env.get("PATH", "")
    _run([str(tools["apksigner"]), "sign", "--ks", str(keystore), "--ks-pass", f"pass:{password}",
          "--ks-key-alias", "frameport-test", "--key-pass", f"pass:{password}", "--out", str(signed),
          str(unsigned)], env=env)
    return signed


def test_manifest_guard_rejects_wrong_package_and_non_native_main_activity():
    with pytest.raises(ValueError, match="package must"):
        repair._main_activity(_manifest(OBB_OWNER))
    with pytest.raises(ValueError, match="package must"):
        repair._main_activity(_manifest("com.example.other"))
    with pytest.raises(ValueError, match="directly name android.app.NativeActivity"):
        repair._patch_manifest(_manifest(activity_name="com.mrf.firaxis.invernessdemo.CustomNativeActivity"))


def test_real_baseline_manifest_package_activity_and_hascode_repair():
    assert repair.PACKAGE == PACKAGE
    assert repair.OBB_OWNER == OBB_OWNER
    baseline = _manifest()
    before, _, activity, _ = repair._main_activity(baseline)
    assert activity == "android.app.NativeActivity"
    assert before.get_bool("application", "hasCode") is False
    repaired = repair.Axml(repair._patch_manifest(baseline))
    assert repaired.get_bool("application", "hasCode") is True
    activity_element = next(element for element in repaired.elements() if element.name == "activity")
    assert repaired.attr_str(activity_element, "name") == repair.ACTIVITY


def test_manifest_requires_existing_editable_hascode_boolean():
    with pytest.raises(ValueError, match="existing android:hasCode boolean"):
        repair._patch_manifest(_manifest(has_code=None))


def test_packager_rejects_repair_namespace_collision_and_changed_payload(tmp_path: Path):
    with pytest.raises(ValueError, match="already defines repair namespace"):
        repair._guard_repair_class_collisions({"Lframeport/civ/DirectoryMount;": "Ljava/lang/Object;"})
    apk = tmp_path / "unexpected-change.apk"
    entries = {"AndroidManifest.xml": b"manifest", "classes2.dex": b"helper", "assets/keep.bin": b"original"}
    with zipfile.ZipFile(apk, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    expected = {name: hashlib.sha256(data).hexdigest() for name, data in entries.items()}
    expected["assets/keep.bin"] = hashlib.sha256(b"changed").hexdigest()
    with pytest.raises(ValueError, match="payload differs"):
        repair._verify_payload(apk, expected)


def test_packager_rejects_symlink_input_before_tool_use(tmp_path: Path):
    source = tmp_path / "source.apk"
    source.write_bytes(b"synthetic")
    link = tmp_path / "source-link.apk"
    link.symlink_to(source)
    args = type("Args", (), {
        "input": link, "output": tmp_path / "output.apk", "javac": tmp_path / "missing-javac",
        "android_jar": tmp_path / "missing-android.jar", "d8": tmp_path / "missing-d8",
    })()
    with pytest.raises(ValueError, match="not a symlink"):
        repair.package(args)


def test_real_java_mount_and_classloader_regressions(tmp_path: Path):
    javac = _find_javac()
    if javac is None:
        pytest.skip("JDK required for the host Java regression tests")
    java = javac.resolve().parent / "java"
    if not java.is_file():
        pytest.skip("matching JDK java executable not found")
    classes = tmp_path / "host-classes"
    fixture_classes = tmp_path / "fixture-classes"
    classes.mkdir()
    fixture_classes.mkdir()
    sources = sorted((SCRIPT_DIR / "stubs").rglob("*.java"))
    sources += sorted((SCRIPT_DIR / "java").rglob("*.java"))
    sources += [SCRIPT_DIR / "tests" / "DirectoryMountTest.java", SCRIPT_DIR / "tests" / "ClassLoaderTest.java"]
    _run([str(javac), "-source", "8", "-target", "8", "-d", str(classes), *map(str, sources)])
    fixture = SCRIPT_DIR / "tests" / "fixtures" / "com" / "firaxis" / "obb" / "LoaderFixture.java"
    _run([str(javac), "-source", "8", "-target", "8", "-d", str(fixture_classes), str(fixture)])
    _run([str(java), "-cp", str(classes), "DirectoryMountTest"])
    _run([str(java), "-cp", str(classes), "ClassLoaderTest", str(fixture_classes)])


def test_real_packager_preserves_signed_payload_and_certificate(tmp_path: Path):
    tools = _toolchain()
    if tools is None:
        pytest.skip("JDK and Android SDK build-tools required for the package integration test")
    password = "test-pass-91"
    keystore = tmp_path / "test.keystore"
    env = os.environ.copy()
    env["PATH"] = str(tools["javac"].resolve().parent) + os.pathsep + env.get("PATH", "")
    _run([str(tools["keytool"]), "-genkeypair", "-keystore", str(keystore), "-storetype", "JKS",
          "-storepass", password, "-keypass", password, "-alias", "frameport-test", "-keyalg", "RSA",
          "-keysize", "2048", "-validity", "10000", "-dname", "CN=FramePort test"], env=env)
    source = _signed_input(tmp_path, tools, keystore, password)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    output = tmp_path / "civilization-directory-loader.apk"
    args = type("Args", (), {
        "input": source, "output": output, "javac": tools["javac"], "android_jar": tools["android_jar"],
        "d8": tools["d8"], "frameport_home": None, "apksigner": tools["apksigner"], "keystore": keystore,
        "ks_pass": password, "ks_key_alias": "frameport-test", "key_pass": password,
    })()
    result = repair.package(args)
    assert result["package"] == PACKAGE
    assert result["activity"] == repair.ACTIVITY
    assert result["added_dex"] == "classes2.dex"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(output) as repaired:
        assert original.read("assets/preserved.dat") == repaired.read("assets/preserved.dat")
        assert set(original.namelist()) - {name for name in original.namelist() if repair._signature_entry(name)} \
            | {"classes2.dex"} == {
                name for name in repaired.namelist() if not repair._signature_entry(name)
            }
        manifest = repair.Axml(repaired.read("AndroidManifest.xml"))
        activity = next(element for element in manifest.elements() if element.name == "activity")
        assert manifest.attr_str(activity, "name") == repair.ACTIVITY
        assert set(repair.dex_classes(repaired.read("classes2.dex"))) == repair.RUNTIME_CLASSES
    assert repair._certificate_digest(source, tools["apksigner"]) == \
        repair._certificate_digest(output, tools["apksigner"])


def _extractor():
    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        return importlib.import_module("extract_obb")
    except ModuleNotFoundError as error:
        if error.name and error.name.startswith("pyfatfs"):
            pytest.skip("pyfatfs host dependency is not installed")
        raise


def _run_extractor_cli(root: Path, source: Path, alias_name: str | None = None) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(SCRIPT_DIR / "extract_obb.py"), "--root", str(root),
               "--owner", OBB_OWNER]
    if alias_name is not None:
        command.extend(("--alias", alias_name))
    command.append(str(source))
    return subprocess.run(command, capture_output=True, text=True)


def _make_obb(root: Path, PyFat, PyFatFS, name: str = "main.obb") -> Path:
    source = root / name
    source.touch()
    formatter = PyFat()
    formatter.mkfs(str(source), PyFat.FAT_TYPE_FAT16, size=8 * 1024 * 1024)
    formatter.close()
    with PyFatFS(str(source)) as fat:
        fat.makedirs("/Game/Assets")
        fat.writebytes("/Game/Assets/Example.Data", b"verified game bytes\0" * 100)
        fat.writebytes("/Empty", b"")
    owner = OBB_OWNER.encode("ascii")
    footer = struct.pack("<III8sI", 1, 147794511, 0, b"12345678", len(owner)) + owner
    with source.open("ab") as stream:
        stream.write(footer + struct.pack("<II", len(footer), 0x01059983))
    return source


def test_extractor_requires_matching_owner_and_reuses_only_safe_complete_output(tmp_path: Path):
    extractor = _extractor()
    PyFat = pytest.importorskip("pyfatfs.PyFat").PyFat
    PyFatFS = pytest.importorskip("pyfatfs.PyFatFS").PyFatFS
    source = _make_obb(tmp_path, PyFat, PyFatFS)
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="unexpected OBB owner"):
        extractor.extract(source, tmp_path, PACKAGE)
    extracted = extractor.extract(source, tmp_path, OBB_OWNER)
    output = Path(str(source) + extractor.SUFFIX)
    assert not extracted["reused"]
    assert (output / "Game/Assets/Example.Data").read_bytes() == b"verified game bytes\0" * 100
    assert extractor.extract(source, tmp_path, OBB_OWNER)["reused"]
    marker = output / extractor.MARKER
    ready_text = marker.read_text()
    marker.write_text("stale\n")
    alias_path = tmp_path / "base.obb"
    with pytest.raises(ValueError, match="lacks matching ready marker"):
        extractor.extract(source, tmp_path, OBB_OWNER, alias_name="base.obb")
    assert not alias_path.exists() and not alias_path.is_symlink()
    marker.write_text(ready_text)
    reused = extractor.extract(source, tmp_path, OBB_OWNER, alias_name="base.obb")
    assert reused["reused"] is True
    assert reused["alias"] == "base.obb"
    assert alias_path.is_symlink() and os.readlink(alias_path) == source.name
    assert alias_path.resolve(strict=True) == source.resolve(strict=True)
    assert extractor.extract(source, tmp_path, OBB_OWNER, alias_name="base.obb")["reused"] is True
    outside = tmp_path / "outside.txt"
    outside.write_text("must not be trusted")
    asset = output / "Game/Assets/Example.Data"
    asset.unlink()
    asset.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink or special file"):
        extractor.extract(source, tmp_path, OBB_OWNER)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash


def test_extractor_sets_android_readable_permissions_under_restrictive_umask(tmp_path: Path):
    extractor = _extractor()
    PyFat = pytest.importorskip("pyfatfs.PyFat").PyFat
    PyFatFS = pytest.importorskip("pyfatfs.PyFatFS").PyFatFS
    source = _make_obb(tmp_path, PyFat, PyFatFS)
    previous_umask = os.umask(0o077)
    try:
        extractor.extract(source, tmp_path, OBB_OWNER)
    finally:
        os.umask(previous_umask)
    output = Path(str(source) + extractor.SUFFIX)
    assert (output / "Empty").read_bytes() == b""
    for path in (output, *output.rglob("*")):
        expected = 0o755 if path.is_dir() else 0o644
        assert stat.S_IMODE(path.stat().st_mode) == expected, path


def test_extractor_rejects_unsafe_paths_symlinks_and_wrong_root(tmp_path: Path):
    extractor = _extractor()
    PyFat = pytest.importorskip("pyfatfs.PyFat").PyFat
    PyFatFS = pytest.importorskip("pyfatfs.PyFatFS").PyFatFS
    source = _make_obb(tmp_path, PyFat, PyFatFS)
    for raw in ("/../outside", "/Game/../outside", "/foo\\bar", f"/{extractor.MARKER}"):
        with pytest.raises(ValueError, match="unsafe FAT filename|reserved marker"):
            extractor.relative_path(raw)
    link = tmp_path / "linked.obb"
    link.symlink_to(source)
    with pytest.raises(ValueError, match="symlink"):
        extractor.extract(link, tmp_path, OBB_OWNER, alias_name="base.obb")
    assert not (tmp_path / "base.obb").exists()
    wrong_root = tmp_path / "Game"
    wrong_root.mkdir()
    with pytest.raises(ValueError, match="outside selected OBB folder"):
        extractor.extract(source, wrong_root, OBB_OWNER)


@pytest.mark.parametrize("alias_name", [
    "", ".obb", "../base.obb", "/tmp/base.obb", "sub/base.obb", "..\\base.obb", "base.txt", "base.obb\0",
])
def test_extractor_rejects_non_basename_aliases(tmp_path: Path, alias_name: str):
    extractor = _extractor()
    with pytest.raises(ValueError, match="direct .obb basename"):
        extractor.validate_alias_name(alias_name, "Base.obb")


@pytest.mark.parametrize("conflict", [
    "regular", "directory", "dangling", "other-target", "escape", "absolute-escape",
])
def test_extractor_never_replaces_alias_collisions(tmp_path: Path, conflict: str):
    extractor = _extractor()
    PyFat = pytest.importorskip("pyfatfs.PyFat").PyFat
    PyFatFS = pytest.importorskip("pyfatfs.PyFatFS").PyFatFS
    source = _make_obb(tmp_path, PyFat, PyFatFS, "Canonical.obb")
    alias = tmp_path / "base.obb"
    original_source = (hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_size,
                       source.stat().st_mtime_ns)
    other = tmp_path / "other.obb"
    other.write_bytes(b"different target")
    if conflict == "regular":
        alias.write_bytes(b"keep regular file")
    elif conflict == "directory":
        alias.mkdir()
        (alias / "keep.txt").write_text("keep directory")
    elif conflict == "dangling":
        alias.symlink_to("missing.obb")
    elif conflict == "escape":
        alias.symlink_to("../outside.obb")
    elif conflict == "absolute-escape":
        outside = tmp_path / "outside.obb"
        outside.write_bytes(b"outside root")
        alias.symlink_to(outside)
    else:
        alias.symlink_to(other.name)
    with pytest.raises(ValueError, match="alias path is already occupied|existing alias"):
        extractor.extract(source, tmp_path, OBB_OWNER, alias_name="base.obb")
    assert not Path(str(source) + extractor.SUFFIX).exists()
    assert (hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_size,
            source.stat().st_mtime_ns) == original_source
    if conflict == "regular":
        assert alias.read_bytes() == b"keep regular file"
    elif conflict == "directory":
        assert (alias / "keep.txt").read_text() == "keep directory"
    elif conflict == "dangling":
        assert alias.is_symlink() and os.readlink(alias) == "missing.obb"
    elif conflict == "escape":
        assert alias.is_symlink() and os.readlink(alias) == "../outside.obb"
    elif conflict == "absolute-escape":
        assert alias.is_symlink() and Path(os.readlink(alias)).is_absolute()
    else:
        assert alias.is_symlink() and os.readlink(alias) == other.name


def test_extractor_cli_creates_relative_alias_for_real_fat_fixture(tmp_path: Path):
    extractor = _extractor()
    PyFat = pytest.importorskip("pyfatfs.PyFat").PyFat
    PyFatFS = pytest.importorskip("pyfatfs.PyFatFS").PyFatFS
    source = _make_obb(tmp_path, PyFat, PyFatFS, "Base.obb")
    alias_name = "base.obb"
    assert extractor.validate_alias_name(alias_name, source.name) == alias_name
    alias_path = tmp_path / alias_name
    if alias_path.exists():
        # Default macOS volumes are case-insensitive, so use a distinct direct basename there.
        with pytest.raises(ValueError, match="collides with the selected source"):
            extractor.extract(source, tmp_path, OBB_OWNER, alias_name=alias_name)
        alias_name = "base-alias.obb"
        alias_path = tmp_path / alias_name
    before = source.stat()
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    first_run = _run_extractor_cli(tmp_path, source)
    assert first_run.returncode == 0, first_run.stderr
    first_report = json.loads(first_run.stdout)
    assert first_report["reused"] is False
    assert "alias" not in first_report
    assert not alias_path.exists() and not alias_path.is_symlink()
    result = _run_extractor_cli(tmp_path, source, alias_name)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    extraction = Path(report["output"])
    assert extraction == Path(str(source) + extractor.SUFFIX)
    assert report["alias"] == alias_name
    assert report["reused"] is True
    assert (extraction / "Game/Assets/Example.Data").read_bytes() == b"verified game bytes\0" * 100
    assert alias_path.is_symlink()
    assert os.readlink(alias_path) == source.name
    assert alias_path.resolve(strict=True) == source.resolve(strict=True)
    reused_run = _run_extractor_cli(tmp_path, source, alias_name)
    assert reused_run.returncode == 0, reused_run.stderr
    reused_report = json.loads(reused_run.stdout)
    assert reused_report["reused"] is True
    assert reused_report["alias"] == alias_name
    after = source.stat()
    assert (after.st_size, after.st_mtime_ns, after.st_ino) == (before.st_size, before.st_mtime_ns, before.st_ino)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash


def test_extractor_cli_refuses_conflict_and_stale_ready_alias(tmp_path: Path):
    PyFat = pytest.importorskip("pyfatfs.PyFat").PyFat
    PyFatFS = pytest.importorskip("pyfatfs.PyFatFS").PyFatFS
    conflict_root = tmp_path / "conflict"
    conflict_root.mkdir()
    conflict_source = _make_obb(conflict_root, PyFat, PyFatFS, "Canonical.obb")
    conflict_alias = conflict_root / "base.obb"
    conflict_alias.write_bytes(b"do not replace")
    conflict_hash = hashlib.sha256(conflict_source.read_bytes()).hexdigest()
    conflict_stat = conflict_source.stat()
    failed_conflict = _run_extractor_cli(conflict_root, conflict_source, "base.obb")
    assert failed_conflict.returncode != 0
    assert "alias path is already occupied" in failed_conflict.stderr
    assert conflict_alias.read_bytes() == b"do not replace"
    assert not Path(str(conflict_source) + ".frameport-extracted").exists()
    assert hashlib.sha256(conflict_source.read_bytes()).hexdigest() == conflict_hash
    assert (conflict_source.stat().st_size, conflict_source.stat().st_mtime_ns) == (
        conflict_stat.st_size, conflict_stat.st_mtime_ns
    )

    stale_root = tmp_path / "stale"
    stale_root.mkdir()
    stale_source = _make_obb(stale_root, PyFat, PyFatFS, "Base.obb")
    stale_alias_name = "base.obb"
    if (stale_root / stale_alias_name).exists():
        stale_alias_name = "base-alias.obb"
    first_run = _run_extractor_cli(stale_root, stale_source)
    assert first_run.returncode == 0, first_run.stderr
    stale_report = json.loads(first_run.stdout)
    ready_marker = Path(stale_report["output"]) / ".frameport-obb-ready"
    ready_marker.write_text("stale\n")
    source_hash = hashlib.sha256(stale_source.read_bytes()).hexdigest()
    source_stat = stale_source.stat()
    failed_stale = _run_extractor_cli(stale_root, stale_source, stale_alias_name)
    assert failed_stale.returncode != 0
    assert "lacks matching ready marker" in failed_stale.stderr
    assert not (stale_root / stale_alias_name).exists()
    assert not (stale_root / stale_alias_name).is_symlink()
    assert ready_marker.read_text() == "stale\n"
    assert hashlib.sha256(stale_source.read_bytes()).hexdigest() == source_hash
    assert (stale_source.stat().st_size, stale_source.stat().st_mtime_ns) == (
        source_stat.st_size, source_stat.st_mtime_ns
    )


def test_extractor_rejects_encrypted_or_invalid_fat_images(tmp_path: Path):
    extractor = _extractor()
    PyFat = pytest.importorskip("pyfatfs.PyFat").PyFat
    PyFatFS = pytest.importorskip("pyfatfs.PyFatFS").PyFatFS
    source = _make_obb(tmp_path, PyFat, PyFatFS)
    payload_length = extractor.metadata(source)[1]["payload_length"]
    with source.open("r+b") as image:
        image.seek(payload_length + 8)
        image.write(struct.pack("<I", 1))
    with pytest.raises(ValueError, match="unencrypted v1 OBB"):
        extractor.extract(source, tmp_path, OBB_OWNER)
    with source.open("r+b") as image:
        image.seek(payload_length + 8)
        image.write(struct.pack("<I", 0))
        image.seek(510)
        image.write(b"\x00\x00")
    with pytest.raises(ValueError, match="FAT size/signature"):
        extractor.extract(source, tmp_path, OBB_OWNER)


def test_extractor_walks_entries_without_high_level_path_lookup(tmp_path: Path):
    extractor = _extractor()
    PyFat = pytest.importorskip("pyfatfs.PyFat").PyFat
    PyFatFS = pytest.importorskip("pyfatfs.PyFatFS").PyFatFS
    source = _make_obb(tmp_path, PyFat, PyFatFS)
    with patch.object(PyFatFS, "getinfo", side_effect=AssertionError("high-level path lookup")), \
            patch.object(PyFatFS, "openbin", side_effect=AssertionError("high-level file lookup")):
        result = extractor.extract(source, tmp_path, OBB_OWNER)
    assert result["files"] == 2


def test_cluster_chain_guard_rejects_cycle():
    extractor = _extractor()

    class CyclicFat:
        def get_cluster_chain(self, first_cluster):
            yield first_cluster
            yield first_cluster + 1
            yield first_cluster

    fat = CyclicFat()
    extractor.checked_cluster_chains(fat)
    with pytest.raises(ValueError, match="cyclic FAT"):
        list(fat.get_cluster_chain(2))
