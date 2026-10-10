"""Manual compatibility shim for built-in Unity Oculus titles with an early EndFrame call.

This experimental patch is only for a title whose trace shows EndFrame before Unity forwards a matching BeginFrame.
It records Unity's real BeginFrame call and inserts a real BeginFrame immediately before EndFrame only when that
matching same-thread/frame call was absent. It always forwards the original EndFrame and returns its result unchanged.
"""
from __future__ import annotations

from ...analysis import elf
from ..base import ApkContext, Patch, register
from . import artifact
from .unity_oculus_check import SHIM, UnityOculusCheck

BEGIN, SHIM_BEGIN = "ovrp_BeginFrame", "fpov_BeginFrame"
END, SHIM_END = "ovrp_EndFrame", "fpob_EndFrame"
LOOKUPS = ((BEGIN, SHIM_BEGIN), (END, SHIM_END))
REQUIRED_EXPORTS = {"fpov_Update2", SHIM_BEGIN, SHIM_END}
REAL_REQUIRED_EXPORTS = {BEGIN, END}


def redirect_legacy_frame_lookups(data: bytes) -> tuple[bytes, list[tuple[str, str]]]:
    """Redirect only the two unique legacy frame lookups, failing closed before changing either one."""
    if not elf.is_elf(data) or not elf.is_64bit(data):
        raise ValueError("Unity legacy frame lookup input must be a 64-bit ELF")
    for original, wrapper in LOOKUPS:
        if len(original) != len(wrapper):
            raise ValueError(f"lookup replacement changes string length: {original} -> {wrapper}")
        original_count = elf.replace_rodata_string(data, original, original)[1]
        wrapper_count = elf.replace_rodata_string(data, wrapper, wrapper)[1]
        if original_count != 1:
            raise ValueError(f"expected exactly one Unity {original} lookup in .rodata, found {original_count}")
        if wrapper_count:
            raise ValueError(f"Unity already contains {wrapper} in .rodata; refusing an ambiguous redirect")

    redirected = data
    changes = []
    for original, wrapper in LOOKUPS:
        redirected, count = elf.replace_rodata_string(redirected, original, wrapper)
        if count != 1:
            raise ValueError(f"Unity {original} redirect changed after preflight: {count}")
        changes.append((original, wrapper))
    return redirected, changes


def _validate_shim(data: bytes) -> None:
    if not elf.is_elf(data) or not elf.is_64bit(data):
        raise ValueError(f"{SHIM} must be a 64-bit ELF")
    if elf.soname(data) != SHIM:
        raise ValueError(f"{SHIM} SONAME does not match its packaged filename")
    missing = REQUIRED_EXPORTS - elf.dyn_symbols(data, defined=True)
    if missing:
        raise ValueError(f"{SHIM} lacks required exports: {', '.join(sorted(missing))}")


class UnityLegacyBeginFrame(Patch):
    id = "frame.unity_legacy_begin_frame"
    title = "Unity: begin legacy frames before submission"
    description = ("Experimental and manual. Use only when a device trace shows a built-in Unity Oculus title calling "
                   "EndFrame before Unity has forwarded BeginFrame for the same frame. Tracks Unity's real "
                   "BeginFrame on that render thread; immediately before EndFrame, calls the real BeginFrame only if "
                   "the matching call was absent. It always forwards EndFrame and preserves its raw result. It adds "
                   "no frame waits or layers and is never recommended automatically.")
    order = 46
    requires = ("frame.unity_oculus_check",)
    experimental = True
    revision = 1

    def applies(self, analysis):
        extra = analysis.extra or {}
        return (analysis.engine == "Unity" and "arm64-v8a" in analysis.abis
                and "libOVRPlugin.so" in analysis.libs and bool(extra.get("unity_oculus_check"))
                and UnityOculusCheck.legacy_loop(analysis))

    def detect(self, analysis):
        return None  # a specific EndFrame/BeginFrame trace is required; never auto-recommend this experiment

    def apply(self, ctx: ApkContext) -> bool:
        ws = ctx.ws
        if not self.applies(ctx.analysis) or ws.abi != "arm64-v8a":
            return False
        if "frame.unity_oculus_check" not in ctx.recipe_patches:
            return False
        unity, plugin = ws.lib("libunity.so"), ws.lib("libOVRPlugin.so")
        if not ws.has(unity) or not ws.has(plugin):
            return False
        unity_data, plugin_data = ws.read(unity), ws.read(plugin)
        if not elf.is_elf(plugin_data) or not elf.is_64bit(plugin_data):
            raise ValueError("Unity OVRPlugin input must be a 64-bit ELF")
        missing_real = REAL_REQUIRED_EXPORTS - elf.dyn_symbols(plugin_data, defined=True)
        if missing_real:
            raise ValueError(f"{plugin} lacks required real exports: {', '.join(sorted(missing_real))}")

        shim_data = artifact(ws.abi, SHIM)
        _validate_shim(shim_data)

        # Accept an already-applied binary only when both redirects are present together. Partial or ambiguous states
        # fail closed before any workspace mutation.
        original_counts = {name: elf.replace_rodata_string(unity_data, name, name)[1]
                           for name, _wrapper in LOOKUPS}
        wrapper_counts = {wrapper: elf.replace_rodata_string(unity_data, wrapper, wrapper)[1]
                          for _name, wrapper in LOOKUPS}
        if original_counts == {BEGIN: 0, END: 0} and wrapper_counts == {SHIM_BEGIN: 1, SHIM_END: 1}:
            redirected_unity, changes = unity_data, []
        else:
            redirected_unity, changes = redirect_legacy_frame_lookups(unity_data)

        needed = elf.needed(plugin_data)
        linked_plugin = plugin_data if SHIM in needed else elf.add_needed(plugin_data, SHIM)
        if SHIM not in elf.needed(linked_plugin):
            raise ValueError(f"{plugin} cannot resolve its {SHIM} dependency")
        shim_path = ws.lib(SHIM)
        shim_changed = not ws.has(shim_path) or ws.read(shim_path) != shim_data

        # Stage changes only after all lookup cardinality, ELF, dependency, and export checks have passed.
        if changes:
            ws.put(unity, redirected_unity)
        if linked_plugin != plugin_data:
            ws.put(plugin, linked_plugin)
        if shim_changed:
            ws.put(shim_path, shim_data)
        if changes:
            ctx.notes.append(f"libunity.so: {BEGIN} -> {SHIM_BEGIN}; {END} -> {SHIM_END}")
        ctx.notes.append(f"{plugin}: loads {SHIM} for conditional BeginFrame forwarding")
        return bool(changes or linked_plugin != plugin_data or shim_changed)

    def validate(self, ctx: ApkContext):
        ws = ctx.ws
        unity, plugin, shim = (ws.lib(name) for name in ("libunity.so", "libOVRPlugin.so", SHIM))
        if not all(ws.has(name) for name in (unity, plugin, shim)):
            return [("Conditional legacy-frame shim linked", False, "Unity, OVRPlugin, or shim library missing")]
        unity_data, plugin_data, shim_data = ws.read(unity), ws.read(plugin), ws.read(shim)
        try:
            _validate_shim(shim_data)
            real_exports = REAL_REQUIRED_EXPORTS <= elf.dyn_symbols(plugin_data, defined=True)
            redirected = all(
                elf.replace_rodata_string(unity_data, original, original)[1] == 0
                and elf.replace_rodata_string(unity_data, wrapper, wrapper)[1] == 1
                for original, wrapper in LOOKUPS
            )
            linked = SHIM in elf.needed(plugin_data)
        except (ValueError, KeyError):
            real_exports, redirected, linked = False, False, False
        return [("OVRPlugin BeginFrame and EndFrame exports present", real_exports, "libOVRPlugin.so"),
                ("Unity BeginFrame and EndFrame lookups redirected", redirected, "libunity.so"),
                ("Conditional BeginFrame shim loaded", linked, SHIM)]


register(UnityLegacyBeginFrame)
