"""Tests for the manual built-in Unity legacy BeginFrame compatibility patch."""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from frameport.analysis import elf
from frameport.analysis.stubgen import build_stub_library
from frameport.apk.workspace import ApkWorkspace
from frameport.core.events import Reporter
from frameport.core.models import Analysis
from frameport.patches import base
from frameport.patches.frame import unity_legacy_begin_frame as patch_module

PATCH_ID = "frame.unity_legacy_begin_frame"
FIXTURES = Path(__file__).with_name("fixtures")


def _analysis(**overrides) -> Analysis:
    fields = dict(package="com.example.legacy", version="1.0", label="Legacy Unity", abis=["arm64-v8a"],
                  engine="Unity", xr="OpenXR", graphics="GLES", direct_vrapi=False,
                  libs=["libunity.so", "libOVRPlugin.so"], launcher_activity="MainActivity",
                  has_info_category=True, meta_permissions=[], uses_glad_gl=False, unity_msaa_levels=0,
                  oculus_os_classes=False, is_overport_output=True, debuggable=True,
                  extra={"unity_version": "2018.4.11f1", "unity_oculus_check": True})
    fields.update(overrides)
    return Analysis(**fields)


def _fixture() -> bytes:
    return (FIXTURES / "libfakeovrplugin_arm64.so").read_bytes()


def _unity_with_lookups(*, duplicate: str | None = None, missing: str | None = None) -> bytes:
    """Use existing whole .rodata strings in the AArch64 ELF fixture as the two Unity lookup names."""
    data = _fixture()
    data, count = elf.replace_rodata_string(data, "libopenxr_loader.so", patch_module.BEGIN)
    assert count == 1
    data, count = elf.replace_rodata_string(data, "xrGetInstanceProcAddr", patch_module.END)
    assert count == 1
    if duplicate == patch_module.BEGIN:
        data, count = elf.replace_rodata_string(data, "xrLoadRenderModelFB", patch_module.BEGIN)
        assert count == 1
    elif duplicate == patch_module.END:
        data, count = elf.replace_rodata_string(data, "xrLoadRenderModelFB", patch_module.END)
        assert count == 1
    if missing:
        data, count = elf.replace_rodata_string(data, missing, "ovrp_Missing")
        assert count == 1
    return data


def _apk(tmp_path: Path, unity: bytes, *, include_plugin: bool = True,
        plugin_exports: tuple[str, ...] = (patch_module.BEGIN, patch_module.END)) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    apk = tmp_path / "legacy.apk"
    with zipfile.ZipFile(apk, "w") as archive:
        archive.writestr("AndroidManifest.xml", b"test manifest")
        archive.writestr("lib/arm64-v8a/libunity.so", unity)
        if include_plugin:
            archive.writestr("lib/arm64-v8a/libOVRPlugin.so",
                             build_stub_library(list(plugin_exports), soname="libOVRPlugin.so"))
    return apk


def _context(ws: ApkWorkspace, analysis: Analysis | None = None, recipe: dict | None = None):
    patch = base.get(PATCH_ID)
    recipe = recipe if recipe is not None else {"frame.unity_oculus_check": {}, PATCH_ID: {}}
    return base.ApkContext(ws, analysis or _analysis(), {}, Reporter(), recipe)


def test_patch_is_experimental_manual_only_and_gated_to_builtin_arm64_unity():
    patch = base.get(PATCH_ID)
    legacy = _analysis()
    assert patch.experimental and not patch.default_on
    assert patch.requires == ("frame.unity_oculus_check",)
    assert patch.applies(legacy) and patch.detect(legacy) is None

    from frameport.recommend.engine import suggest

    assert PATCH_ID not in suggest(legacy, use_catalog=False).patches
    assert not patch.applies(_analysis(engine="Unreal"))
    assert not patch.applies(_analysis(abis=["armeabi-v7a"]))
    assert not patch.applies(_analysis(extra={"unity_version": "2018.4.11f1"}))
    assert not patch.applies(_analysis(extra={"unity_version": "2022.3.8f1", "unity_oculus_check": True}))
    assert not patch.applies(_analysis(
        libs=["libunity.so", "libOVRPlugin.so", "libOculusXRPlugin.so"],
        extra={"unity_version": "2019.4.11f1", "unity_oculus_check": True},
    ))


def test_real_patch_redirects_only_frame_lookups_links_checked_shim_and_is_idempotent(tmp_path, monkeypatch):
    original_unity = _unity_with_lookups()
    apk = _apk(tmp_path, original_unity)
    patch = base.get(PATCH_ID)
    with ApkWorkspace(apk) as ws:
        ctx = _context(ws)
        assert patch.apply(ctx)
        patched_unity = ws.read_lib("libunity.so")
        plugin = ws.read_lib("libOVRPlugin.so")
        shim = ws.read_lib(patch_module.SHIM)
        assert len(patched_unity) == len(original_unity)
        assert elf.replace_rodata_string(patched_unity, patch_module.BEGIN, patch_module.BEGIN)[1] == 0
        assert elf.replace_rodata_string(patched_unity, patch_module.END, patch_module.END)[1] == 0
        assert elf.replace_rodata_string(patched_unity, patch_module.SHIM_BEGIN, patch_module.SHIM_BEGIN)[1] == 1
        assert elf.replace_rodata_string(patched_unity, patch_module.SHIM_END, patch_module.SHIM_END)[1] == 1
        assert patch_module.SHIM in elf.needed(plugin)
        assert patch_module.REQUIRED_EXPORTS <= elf.dyn_symbols(shim, defined=True)
        assert patch.validate(ctx) == [
            ("OVRPlugin BeginFrame and EndFrame exports present", True, "libOVRPlugin.so"),
            ("Unity BeginFrame and EndFrame lookups redirected", True, "libunity.so"),
            ("Conditional BeginFrame shim loaded", True, patch_module.SHIM),
        ]
        actual_dyn_symbols = elf.dyn_symbols

        def missing_real_end(data: bytes, defined: bool):
            symbols = actual_dyn_symbols(data, defined)
            return symbols - {patch_module.END} if data == plugin and defined else symbols

        monkeypatch.setattr(patch_module.elf, "dyn_symbols", missing_real_end)
        assert patch.validate(ctx)[0] == (
            "OVRPlugin BeginFrame and EndFrame exports present", False, "libOVRPlugin.so"
        )
        monkeypatch.undo()
        before = dict(ws.replace), dict(ws.add)
        assert not patch.apply(ctx)
        assert (ws.replace, ws.add) == before
        output = ws.write(tmp_path / "patched.apk")

    with zipfile.ZipFile(output) as archive:
        assert archive.read("lib/arm64-v8a/libunity.so") == patched_unity
        assert patch_module.SHIM in elf.needed(archive.read("lib/arm64-v8a/libOVRPlugin.so"))
        assert archive.read("lib/arm64-v8a/libfp_ovrp.so") == shim


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"missing": patch_module.END}, "expected exactly one Unity ovrp_EndFrame"),
        ({"duplicate": patch_module.BEGIN}, "expected exactly one Unity ovrp_BeginFrame"),
        ({"duplicate": patch_module.END}, "expected exactly one Unity ovrp_EndFrame"),
    ],
)
def test_lookup_shape_failures_are_rejected_before_workspace_mutation(tmp_path, kwargs, expected):
    original_unity = _unity_with_lookups(**kwargs)
    apk = _apk(tmp_path, original_unity)
    patch = base.get(PATCH_ID)
    with ApkWorkspace(apk) as ws:
        original_plugin = ws.read_lib("libOVRPlugin.so")
        ctx = _context(ws)
        with pytest.raises(ValueError, match=expected):
            patch.apply(ctx)
        assert not ws.changed()
        assert ws.read_lib("libunity.so") == original_unity
        assert ws.read_lib("libOVRPlugin.so") == original_plugin
        assert not ws.has(ws.lib(patch_module.SHIM))


def test_missing_required_shim_export_is_rejected_before_workspace_mutation(tmp_path, monkeypatch):
    original_unity = _unity_with_lookups()
    apk = _apk(tmp_path, original_unity)
    patch = base.get(PATCH_ID)
    monkeypatch.setattr(patch_module, "artifact", lambda _abi, _name: b"not an ELF")
    with ApkWorkspace(apk) as ws:
        ctx = _context(ws)
        with pytest.raises(ValueError, match="must be a 64-bit ELF"):
            patch.apply(ctx)
        assert not ws.changed()
        assert not ws.has(ws.lib(patch_module.SHIM))


def test_missing_conditional_wrapper_export_is_rejected_before_workspace_mutation(tmp_path, monkeypatch):
    original_unity = _unity_with_lookups()
    apk = _apk(tmp_path, original_unity)
    patch = base.get(PATCH_ID)
    shim = patch_module.artifact("arm64-v8a", patch_module.SHIM)
    actual_dyn_symbols = elf.dyn_symbols

    def missing_conditional_end(data: bytes, defined: bool):
        symbols = actual_dyn_symbols(data, defined)
        return symbols - {patch_module.SHIM_END} if data == shim and defined else symbols

    monkeypatch.setattr(patch_module.elf, "dyn_symbols", missing_conditional_end)
    with ApkWorkspace(apk) as ws:
        ctx = _context(ws)
        with pytest.raises(ValueError, match="lacks required exports: fpob_EndFrame"):
            patch.apply(ctx)
        assert not ws.changed()
        assert not ws.has(ws.lib(patch_module.SHIM))


@pytest.mark.parametrize(
    ("plugin_exports", "missing"),
    [
        ((patch_module.END,), patch_module.BEGIN),
        ((patch_module.BEGIN,), patch_module.END),
    ],
)
def test_missing_real_ovrplugin_target_is_rejected_before_workspace_mutation(tmp_path, plugin_exports, missing):
    original_unity = _unity_with_lookups()
    apk = _apk(tmp_path, original_unity, plugin_exports=plugin_exports)
    patch = base.get(PATCH_ID)
    with ApkWorkspace(apk) as ws:
        original_plugin = ws.read_lib("libOVRPlugin.so")
        ctx = _context(ws)
        with pytest.raises(ValueError, match=f"lacks required real exports: {missing}"):
            patch.apply(ctx)
        assert not ws.changed()
        assert ws.read_lib("libunity.so") == original_unity
        assert ws.read_lib("libOVRPlugin.so") == original_plugin
        assert not ws.has(ws.lib(patch_module.SHIM))


def test_missing_plugin_or_prerequisite_never_mutates_apk(tmp_path):
    patch = base.get(PATCH_ID)
    unsupported_apk = _apk(tmp_path, _unity_with_lookups(), include_plugin=False)
    with ApkWorkspace(unsupported_apk) as ws:
        assert not patch.apply(_context(ws))
        assert not ws.changed()

    no_prerequisite_apk = _apk(tmp_path / "no-prerequisite", _unity_with_lookups())
    with ApkWorkspace(no_prerequisite_apk) as ws:
        assert not patch.apply(_context(ws, recipe={PATCH_ID: {}}))
        assert not ws.changed()
