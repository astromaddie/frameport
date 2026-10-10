"""Manual early wait for legacy Unity's simulation/controller update phase."""
from __future__ import annotations

from ...analysis import elf
from ..base import ApkContext, Patch, register
from . import artifact
from .adapter import GENERIC, ORIGINAL, SETTINGS
from .unity_oculus_check import SHIM, UnityOculusCheck

BRIDGE_EXPORTS = {"framebridge_pose_wait_reset_v1", "framebridge_pose_wait_time_v1",
                  "framebridge_pose_push_v1", "framebridge_pose_pop_v1"}

OLD, EARLY = "fpov_Update2", "fpow_Update2"


class UnityEarlyFrameWait(Patch):
    id = "frame.unity_early_frame_wait"
    title = "Unity: wait before legacy controller updates"
    description = ("Experimental and manual. For built-in Oculus titles whose simulation Update2 runs before the "
                   "frame wait, waits once at the first simulation or render Update2 for that frame. The adapter uses "
                   "that waited frame time only for the physics cache's invalid view sentinel and controller "
                   "monotonic fallback. Warnings can remain; wearer validation is required. No smoothing or "
                   "BeginFrame/EndFrame injection.")
    order = 47
    requires = ("frame.unity_oculus_check",)
    experimental = True
    revision = 2

    def applies(self, analysis):
        return (analysis.engine == "Unity" and "arm64-v8a" in analysis.abis
                and "libOVRPlugin.so" in analysis.libs and UnityOculusCheck.legacy_loop(analysis))

    def detect(self, analysis):
        return None

    def apply(self, ctx: ApkContext) -> bool:
        ws = ctx.ws
        if not self.applies(ctx.analysis) or ws.abi != "arm64-v8a":
            return False
        if "frame.unity_oculus_check" not in ctx.recipe_patches:
            return False
        unity, plugin, shim = (ws.lib(n) for n in ("libunity.so", "libOVRPlugin.so", SHIM))
        if not ws.has(unity) or not ws.has(plugin):
            return False
        data, plugin_data = ws.read(unity), ws.read(plugin)
        if not all(elf.is_elf(d) and elf.is_64bit(d) for d in (data, plugin_data)):
            raise ValueError("early frame wait requires 64-bit Unity and OVRPlugin ELF libraries")
        if not {"ovrp_Update2", "ovrp_WaitToBeginFrame"} <= elf.dyn_symbols(plugin_data, defined=True):
            raise ValueError("OVRPlugin lacks Update2 or WaitToBeginFrame")
        old_count = elf.replace_rodata_string(data, OLD, OLD)[1]
        early_count = elf.replace_rodata_string(data, EARLY, EARLY)[1]
        original_count = elf.replace_rodata_string(data, "ovrp_Update2", "ovrp_Update2")[1]
        if (old_count, early_count, original_count) not in ((1, 0, 0), (0, 1, 0)):
            raise ValueError("expected exactly one existing legacy Update2 shim lookup")
        shim_data = artifact(ws.abi, SHIM)
        if (not elf.is_elf(shim_data) or not elf.is_64bit(shim_data)
                or elf.soname(shim_data) != SHIM
                or not {OLD, EARLY, "fpow_PoseTimeVersion"} <= elf.dyn_symbols(shim_data, defined=True)):
            raise ValueError("OVR shim lacks the original and early-wait exports")
        if SHIM not in elf.needed(plugin_data):
            raise ValueError("OVRPlugin does not load the legacy frame shim")
        if not ws.has(ws.lib(ORIGINAL)) or not ws.has(ws.lib(SETTINGS)):
            raise ValueError("early pose timing requires an existing FrameBridge adapter and settings")
        adapter_data = artifact(ws.abi, GENERIC)
        if (not elf.is_elf(adapter_data) or not elf.is_64bit(adapter_data)
                or elf.soname(adapter_data) != GENERIC
                or not BRIDGE_EXPORTS <= elf.dyn_symbols(adapter_data, defined=True)):
            raise ValueError("FrameBridge lacks the versioned legacy pose timing ABI")
        settings = ws.read(ws.lib(SETTINGS))
        # Preserve the title's settings, replacing only this manual patch's dedicated key.
        settings = b"\n".join(line for line in settings.splitlines()
                              if not line.strip().startswith(b"legacy_pose_time=")) + b"\nlegacy_pose_time=1\n"
        changed = False
        if old_count:
            data, count = elf.replace_rodata_string(data, OLD, EARLY)
            if count != 1:
                raise ValueError("Update2 lookup changed after validation")
            ws.put(unity, data)
            changed = True
        if not ws.has(shim) or ws.read(shim) != shim_data:
            ws.put(shim, shim_data)
            changed = True
        for path, payload in ((ws.lib(GENERIC), adapter_data), (ws.lib(SETTINGS), settings)):
            if not ws.has(path) or ws.read(path) != payload:
                ws.put(path, payload)
                changed = True
        ctx.notes.append("Unity: wait before the first Update2 phase; scoped physics pose times (manual experiment)")
        return changed

    def validate(self, ctx: ApkContext):
        ws = ctx.ws
        unity, plugin, shim = (ws.lib(n) for n in ("libunity.so", "libOVRPlugin.so", SHIM))
        ok = all(ws.has(n) for n in (unity, plugin, shim))
        if ok:
            data = ws.read(unity)
            ok = (elf.replace_rodata_string(data, EARLY, EARLY)[1] == 1
                  and not elf.replace_rodata_string(data, OLD, OLD)[1]
                  and not elf.replace_rodata_string(data, "ovrp_Update2", "ovrp_Update2")[1]
                  and EARLY in elf.dyn_symbols(ws.read(shim), defined=True)
                  and SHIM in elf.needed(ws.read(plugin))
                  and ws.has(ws.lib(GENERIC)) and ws.has(ws.lib(SETTINGS))
                  and BRIDGE_EXPORTS <= elf.dyn_symbols(ws.read(ws.lib(GENERIC)), defined=True)
                  and b"legacy_pose_time=1" in ws.read(ws.lib(SETTINGS)).splitlines())
        return [("Legacy controller update waits before prediction", ok, "manual early-wait shim")]


register(UnityEarlyFrameWait)
