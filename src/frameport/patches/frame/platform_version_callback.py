"""Complete OVRPort's zero-return Application_GetVersion requests for one verified loader stub."""
from __future__ import annotations

from ...analysis import elf
from ..base import ApkContext, Patch, register
from . import artifact, langpack

LOADER = "libovrplatformloader.so"
LIB = langpack.LIB
FUNCTION = "ovr_Application_GetVersion"
# C-Smash's installed AArch64 dispatcher is exactly `mov x0, xzr; ret`.
ZERO_RETURN_STUB = bytes.fromhex("e0031faac0035fd6")
# The version wrapper shares its synthetic queue with these managed SDK calls. Hide only
# this common message/error path; language-pack and asset-file APIs remain on the loader.
INTERPOSE_EXPORTS = (
    FUNCTION,
    "ovr_PopMessage",
    "ovr_FreeMessage",
    "ovr_Message_GetType",
    "ovr_Message_GetRequestID",
    "ovr_Message_IsError",
    "ovr_Message_GetError",
    "ovr_Error_GetCode",
    "ovr_Error_GetHttpCode",
    "ovr_Error_GetMessage",
    "ovr_Error_GetDisplayableMessage",
)


def _symbols(data: bytes):
    """Return the required, unique function symbols from a little-endian AArch64 ELF."""
    if not elf.is_elf(data) or not elf.is_64bit(data) or data[5] != 1:
        return None
    try:
        image = elf._elf(data)
        if image["e_machine"] != "EM_AARCH64":
            return None
        dynsym = image.get_section_by_name(".dynsym")
        if dynsym is None:
            return None
        found = {name: [] for name in INTERPOSE_EXPORTS}
        for sym in dynsym.iter_symbols():
            if sym.name in found:
                found[sym.name].append(sym)
        if any(len(matches) != 1 or matches[0]["st_shndx"] == "SHN_UNDEF"
               or matches[0]["st_info"]["type"] != "STT_FUNC" for matches in found.values()):
            return None
        return {name: matches[0] for name, matches in found.items()}
    except Exception:  # malformed or unsupported ELF: fail closed
        return None


def _function(data: bytes) -> tuple[int, str] | None:
    """Return the exact export's file offset and binding for a little-endian AArch64 ELF."""
    symbols = _symbols(data)
    if not symbols or symbols[FUNCTION]["st_size"] != len(ZERO_RETURN_STUB):
        return None
    try:
        sym = symbols[FUNCTION]
        address = sym["st_value"]
        for start, offset, size in elf.load_segments(data):
            if start <= address and address + len(ZERO_RETURN_STUB) <= start + size:
                return offset + address - start, sym["st_info"]["bind"]
    except Exception:  # malformed or unsupported ELF: fail closed
        return None
    return None


def zero_return_stub(data: bytes) -> bool:
    symbol = _function(data)
    return bool(symbol and data[symbol[0]:symbol[0] + len(ZERO_RETURN_STUB)] == ZERO_RETURN_STUB)


def callback_linked(data: bytes) -> bool:
    symbols = _symbols(data)
    return bool(zero_return_stub(data) and symbols
                and all(sym["st_info"]["bind"] == "STB_LOCAL" for sym in symbols.values())
                and LIB in elf.needed(data))


def patch_loader(data: bytes) -> bytes | None:
    """Interpose the callback queue only for the verified zero-return export."""
    symbol = _function(data)
    if not symbol or data[symbol[0]:symbol[0] + len(ZERO_RETURN_STUB)] != ZERO_RETURN_STUB:
        return None
    out, hidden = elf.hide_exports(data, INTERPOSE_EXPORTS)
    changed = bool(hidden)
    if LIB not in elf.needed(out):
        out = elf.add_needed(out, LIB)
        changed = True
    return out if changed else None


class PlatformVersionCallback(Patch):
    id = "frame.platform_version_callback"
    title = "Finish unsupported version checks"
    description = ("Some OVRPort platform loaders return request id 0 from Application_GetVersion without posting a "
                   "callback, leaving the game's version-check screen stuck. For the verified C-Smash loader stub "
                   "only, routes the shared message/error API through the callback shim and queues an explicit "
                   "unsupported-version error. Real requests and foreign messages are forwarded unchanged; no "
                   "version, login, or entitlement is invented.")
    order = 31
    experimental = True  # exact loader and runtime behavior are title/version-specific

    def applies(self, analysis):
        return LOADER in analysis.libs and "arm64-v8a" in analysis.abis

    def detect(self, analysis):
        return None  # opt-in until the exact installed loader has been checked

    def apply(self, ctx: ApkContext) -> bool:
        ws = ctx.ws
        if ws.abi != "arm64-v8a" or not ws.has(ws.lib(LOADER)):
            return False
        loader = ws.read_lib(LOADER)
        if not zero_return_stub(loader):
            ctx.notes.append(f"{LOADER}: Application_GetVersion is not the verified zero-return stub")
            return False

        lib_path = ws.lib(LIB)
        previous_lib = ws.read_lib(LIB) if ws.has(lib_path) else None
        rebuilt_lib = artifact(ws.abi, LIB)
        if FUNCTION not in elf.dyn_symbols(rebuilt_lib, True):
            ctx.notes.append(f"{LIB}: the built library does not export {FUNCTION}")
            return False

        patched = patch_loader(loader)
        if patched is None and not callback_linked(loader):
            ctx.notes.append(f"{LOADER}: the version callback exports or its message API cannot be interposed")
            return False

        changed = False
        if previous_lib is None or FUNCTION not in elf.dyn_symbols(previous_lib, True):
            if previous_lib is not None:
                rebuilt_lib = langpack.with_configuration_from(rebuilt_lib, previous_lib)
            ws.put(lib_path, rebuilt_lib)
            changed = True
        if patched is not None:
            ws.put(ws.lib(LOADER), patched)
            changed = True
        if changed:
            ctx.notes.append(f"{LOADER}: {FUNCTION} and its message API route to an unsupported-version error callback")
        return changed

    def validate(self, ctx: ApkContext):
        ws = ctx.ws
        if not ws.has(ws.lib(LOADER)):
            return []
        loader = ws.read_lib(LOADER)
        lib_ok = ws.has(ws.lib(LIB)) and FUNCTION in elf.dyn_symbols(ws.read_lib(LIB), True)
        return [("Verified version callback and message API are linked", callback_linked(loader) and lib_ok, LIB)]


register(PlatformVersionCallback)
