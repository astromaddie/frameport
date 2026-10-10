"""Guard and exercise the manual early-wait patch through ApkWorkspace."""
import zipfile
from pathlib import Path

import pytest

from frameport.analysis import elf
from frameport.analysis.stubgen import build_stub_library
from frameport.apk.workspace import ApkWorkspace
from frameport.core.events import Reporter
from frameport.core.models import Analysis
from frameport.patches import base
from frameport.patches.frame import unity_early_frame_wait as patch


def analysis():
    return Analysis(package="com.test.early", version="1", label="Test", abis=["arm64-v8a"], engine="Unity",
                    xr="OpenXR", graphics="GLES", direct_vrapi=False, libs=["libunity.so", "libOVRPlugin.so"],
                    launcher_activity="Main", has_info_category=True, meta_permissions=[], uses_glad_gl=False,
                    unity_msaa_levels=0, oculus_os_classes=False, is_overport_output=True, debuggable=True,
                    extra={"unity_version": "2018.4"})


def test_manual_only_and_no_modern_plugin_recommendation():
    fix = base.get("frame.unity_early_frame_wait")
    a = analysis()
    assert fix.applies(a) and fix.detect(a) is None and fix.experimental and not fix.default_on
    a.extra["unity_version"] = "2020.1"
    assert not fix.applies(a)


@pytest.mark.parametrize("invalid", [None, "missing_lookup", "both_lookups", "missing_wait",
                                   "missing_shim_export", "missing_bridge_export"])
def test_workspace_guarded_update_and_idempotence(tmp_path, monkeypatch, invalid):
    data = (Path(__file__).with_name("fixtures") / "libfakeovrplugin_arm64.so").read_bytes()
    data, n = elf.replace_rodata_string(data, "libopenxr_loader.so",
                                      "missing" if invalid == "missing_lookup" else patch.OLD)
    assert n == 1
    if invalid == "both_lookups":
        data, n = elf.replace_rodata_string(data, "xrGetInstanceProcAddr", patch.EARLY)
        assert n == 1
    real = ["ovrp_Update2"] + ([] if invalid == "missing_wait" else ["ovrp_WaitToBeginFrame"])
    plugin = elf.add_needed(build_stub_library(real, soname="libOVRPlugin.so"), patch.SHIM)
    exports = [patch.OLD, "fpow_PoseTimeVersion"] + ([] if invalid == "missing_shim_export" else [patch.EARLY])
    shim = build_stub_library(exports, soname=patch.SHIM)
    missing_bridge = {"framebridge_pose_push_v1"} if invalid == "missing_bridge_export" else set()
    bridge_exports = patch.BRIDGE_EXPORTS - missing_bridge
    adapter = build_stub_library(sorted(bridge_exports), soname=patch.GENERIC)
    monkeypatch.setattr(patch, "artifact", lambda abi, name: shim if name == patch.SHIM else adapter)
    apk = tmp_path / "base.apk"
    with zipfile.ZipFile(apk, "w") as z:
        z.writestr("AndroidManifest.xml", b"manifest")
        z.writestr("lib/arm64-v8a/libunity.so", data)
        z.writestr("lib/arm64-v8a/libOVRPlugin.so", plugin)
        z.writestr("lib/arm64-v8a/" + patch.ORIGINAL, b"original")
        z.writestr("lib/arm64-v8a/" + patch.SETTINGS, b"scale=1.25\n")
    fix = base.get("frame.unity_early_frame_wait")
    with ApkWorkspace(apk) as ws:
        ctx = base.ApkContext(ws, analysis(), {}, Reporter(),
                             {"frame.unity_oculus_check": {}, fix.id: {}})
        if invalid:
            with pytest.raises(ValueError):
                fix.apply(ctx)
            assert ws.read(ws.lib("libunity.so")) == data
            assert not ws.has(ws.lib(patch.SHIM))
        else:
            assert fix.apply(ctx)
            assert ws.read(ws.lib("libOVRPlugin.so")) == plugin
            assert all(ok for _, ok, _ in fix.validate(ctx))
            assert ws.read(ws.lib(patch.SETTINGS)) == b"scale=1.25\nlegacy_pose_time=1\n"
            assert not fix.apply(ctx)


def test_recipe_enables_bridge_only_with_manual_patch():
    from frameport.patches.settings import adapter_settings

    assert "legacy_pose_time" not in adapter_settings({"frame.unity_oculus_check": {}})
    assert adapter_settings({"frame.unity_early_frame_wait": {}})["legacy_pose_time"] == 1
