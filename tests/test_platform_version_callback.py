"""The opt-in Application_GetVersion repair is guarded by one exact loader function body."""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

from elftools.elf.elffile import ELFFile

from frameport.analysis import elf
from frameport.apk.workspace import ApkWorkspace
from frameport.core.events import Reporter
from frameport.core.models import Analysis
from frameport.parity import classify
from frameport.patches import base
from frameport.patches.frame import langpack
from frameport.patches.frame import platform_version_callback as version

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/libfakeplatformversion_arm64.so"
LOADER = "libovrplatformloader.so"
LIB = "libfp_langpack.so"
FUNCTION = "ovr_Application_GetVersion"


def _analysis(**kw):
    data = dict(package="com.example.csmash", version="1.2.3", label="C-Smash VRS", abis=["arm64-v8a"],
                engine="Unity", xr="OpenXR", graphics="Vulkan", direct_vrapi=False, libs=[LOADER],
                launcher_activity=None, has_info_category=True, meta_permissions=[], uses_glad_gl=False,
                unity_msaa_levels=4, oculus_os_classes=False, is_overport_output=True, debuggable=True, extra={})
    data.update(kw)
    return Analysis(**data)


def _apk(tmp_path, loader, library=None):
    path = tmp_path / "input.apk"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("AndroidManifest.xml", b"manifest")
        z.writestr("classes.dex", b"dex\n035\0")
        z.writestr(f"lib/arm64-v8a/{LOADER}", loader)
        if library is not None:
            z.writestr(f"lib/arm64-v8a/{LIB}", library)
    return path


def _binding(data, name):
    table = ELFFile(io.BytesIO(data)).get_section_by_name(".dynsym")
    symbols = table.get_symbol_by_name(name)
    assert symbols and len(symbols) == 1
    return symbols[0]["st_info"]["bind"]


def _function_offset(data, name):
    image = ELFFile(io.BytesIO(data))
    symbol, = image.get_section_by_name(".dynsym").get_symbol_by_name(name)
    for segment in image.iter_segments():
        if segment["p_type"] == "PT_LOAD" and segment["p_vaddr"] <= symbol["st_value"] < \
                segment["p_vaddr"] + segment["p_filesz"]:
            return segment["p_offset"] + symbol["st_value"] - segment["p_vaddr"]
    raise AssertionError(f"no file mapping for {name}")


def test_patch_is_opt_in_and_does_not_extend_language_asset_interposition():
    patch = base.get("frame.platform_version_callback")
    assert patch.summary and patch.experimental and not patch.default_on
    assert "ovr_Application_GetVersion" in langpack.EXPORTS
    assert "ovr_Application_GetVersion" not in langpack.LANGPACK_EXPORTS
    assert not patch.applies(_analysis(libs=[]))
    assert not patch.applies(_analysis(abis=["armeabi-v7a"]))
    assert patch.detect(_analysis()) is None


def test_exact_loader_transform_is_idempotent_and_preserves_other_exports():
    loader = FIXTURE.read_bytes()
    assert version.zero_return_stub(loader)
    fixed = version.patch_loader(loader)
    assert fixed is not None
    assert elf.needed(fixed)[0] == LIB
    assert all(_binding(fixed, name) == "STB_LOCAL" for name in version.INTERPOSE_EXPORTS)
    assert _binding(fixed, "ovr_Application_GetCurrentVersion") == "STB_GLOBAL"
    assert _binding(fixed, "ovr_LanguagePack_GetCurrent") == "STB_GLOBAL"
    assert _binding(fixed, "ovr_AssetFile_GetList") == "STB_GLOBAL"
    assert elf.dyn_symbols(fixed, True) == elf.dyn_symbols(loader, True)
    assert version.callback_linked(fixed)
    assert version.patch_loader(fixed) is None


def test_loader_guard_rejects_changed_stub_and_wrong_architecture():
    loader = FIXTURE.read_bytes()
    off, _ = version._function(loader)
    changed_code = bytearray(loader)
    changed_code[off] ^= 1
    assert not version.zero_return_stub(bytes(changed_code))
    assert version.patch_loader(bytes(changed_code)) is None

    wrong_arch = bytearray(loader)
    wrong_arch[18:20] = (62).to_bytes(2, "little")  # EM_X86_64
    assert not version.zero_return_stub(wrong_arch)
    assert version.patch_loader(wrong_arch) is None


def test_apply_links_exact_artifact_and_preserves_existing_library_configuration(tmp_path):
    loader = FIXTURE.read_bytes()
    target = version.artifact("arm64-v8a", LIB)
    patch = base.get("frame.platform_version_callback")
    analysis = _analysis()
    assert patch.applies(analysis) and patch.detect(analysis) is None

    with ApkWorkspace(_apk(tmp_path, loader)) as ws:
        ctx = base.ApkContext(ws, analysis, {}, Reporter(), {patch.id: {}})
        assert patch.apply(ctx)
        fixed_loader = ws.read_lib(LOADER)
        assert fixed_loader == version.patch_loader(loader)
        assert ws.read_lib(LIB) == target  # callback-only installs retain the artifact's exact bytes
        assert version.callback_linked(fixed_loader)
        assert all(ok for _, ok, _ in patch.validate(ctx))
        assert not patch.apply(ctx)
        assert ws.read_lib(LOADER) == fixed_loader and ws.read_lib(LIB) == target

    old_library = (b"legacy library\0" + langpack.META_MARK + b"old-version".ljust(langpack.META_LEN, b"\0")
                   + langpack.ASSETS_MARK + b"1")
    expected_library = langpack.with_configuration_from(target, old_library)

    legacy_linked, hidden = elf.hide_exports(loader, langpack.LANGPACK_EXPORTS)
    assert set(version.INTERPOSE_EXPORTS[1:]).issubset(hidden)
    legacy_linked = elf.add_needed(legacy_linked, LIB)
    callback_linked = version.patch_loader(legacy_linked)
    assert callback_linked is not None
    assert version.callback_linked(callback_linked)
    assert version.patch_loader(callback_linked) is None
    assert elf.needed(callback_linked).count(LIB) == 1
    for name in version.INTERPOSE_EXPORTS[1:]:
        assert _binding(callback_linked, name) == "STB_LOCAL"  # prior langpack interposition stays in place

    with ApkWorkspace(_apk(tmp_path, callback_linked, old_library)) as ws:
        ctx = base.ApkContext(ws, analysis, {}, Reporter(), {patch.id: {}})
        assert patch.apply(ctx)
        updated = ws.read_lib(LIB)
        assert updated == expected_library
        assert FUNCTION in elf.dyn_symbols(updated, True)
        assert elf.needed(ws.read_lib(LOADER)).count(LIB) == 1
        meta_at = updated.index(langpack.META_MARK) + len(langpack.META_MARK)
        assert updated[meta_at:meta_at + langpack.META_LEN] == old_library[
            old_library.index(langpack.META_MARK) + len(langpack.META_MARK):
            old_library.index(langpack.META_MARK) + len(langpack.META_MARK) + langpack.META_LEN]
        assert langpack.ASSETS_MARK + b"1" in updated
        fixed_loader = ws.read_lib(LOADER)
        assert not patch.apply(ctx)
        assert ws.read_lib(LOADER) == fixed_loader
        assert ws.read_lib(LIB) == updated


def test_non_matching_loader_is_left_alone(tmp_path):
    loader = bytearray(FIXTURE.read_bytes())
    loader[_function_offset(bytes(loader), FUNCTION)] ^= 1
    patch = base.get("frame.platform_version_callback")
    with ApkWorkspace(_apk(tmp_path, bytes(loader))) as ws:
        ctx = base.ApkContext(ws, _analysis(), {}, Reporter(), {patch.id: {}})
        assert not patch.apply(ctx)
        assert ws.read_lib(LOADER) == bytes(loader)
        assert not ws.has(ws.lib(LIB))
        assert any("not the verified zero-return stub" in note for note in ctx.notes)


def test_parity_accepts_only_the_exact_version_callback_transform():
    loader = FIXTURE.read_bytes()
    fixed = version.patch_loader(loader)
    assert fixed is not None
    path = f"lib/arm64-v8a/{LOADER}"
    kind, reason = classify(path, fixed, loader)
    assert kind == "expected" and "verified zero-return" in reason
    assert classify(f"lib/arm64-v8a/{LIB}", version.artifact("arm64-v8a", LIB), b"old library")[0] == "expected"

    extra_change = bytearray(fixed)
    extra_change[_function_offset(extra_change, "ovr_Application_GetCurrentVersion")] ^= 1
    assert classify(path, bytes(extra_change), loader)[0] == "UNEXPLAINED"
