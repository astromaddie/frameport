"""Find methods in an IL2CPP Unity game: Cpp2IL lists every method with its address ([Address(... Offset = "0x...")]
in its "diffable C#" output with the attribute injector). Offset is the method's position in the libil2cpp.so FILE
(not the virtual address: those differ, e.g. by 0x4000 in Stremio VR's library), which is what a byte patch needs.

Results are cached per game build (its global-metadata.dat), so a rebuild of the same game doesn't run Cpp2IL again,
and neither does the next patch of the same build (an earlier one has already changed libil2cpp.so's bytes, never its
layout). Each cache entry records which methods were searched in each class: adding a patch target to a class already
in the cache triggers one new Cpp2IL run, while searched-but-absent methods and classes remain cached as missing.
Failed attempts are shared within one build but are not persisted, so a later build can retry them."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from ..core.cache import cache_dir

_ADDRESS = re.compile(r'Offset = "0x([0-9A-Fa-f]+)", Length = "0x([0-9A-Fa-f]+)"')
_attempt_failures: ContextVar[dict[Path, str] | None] = ContextVar("cpp2il_attempt_failures", default=None)


@contextmanager
def cpp2il_attempt_scope() -> Iterator[None]:
    """Deduplicate failed Cpp2IL runs during one APK build, then discard failures so a later build retries."""
    token = _attempt_failures.set({})
    try:
        yield
    finally:
        _attempt_failures.reset(token)


def _remember_failure(cache_file: Path, error: str) -> None:
    failures = _attempt_failures.get()
    if failures is not None:
        failures[cache_file] = error


def parse_methods(cs_text: str, names: list[str]) -> dict[str, tuple[int, int]]:
    """{method name: (file offset, length)} for the named methods of one Cpp2IL diffable-C# class file. A name
    "field:<f>" gives the instance field's offset instead: (offset, 0)."""
    lines = cs_text.splitlines()
    found: dict[str, tuple[int, int]] = {}
    for name in [n for n in names if n.startswith("field:")]:
        m = re.search(rf"\s{re.escape(name[6:])};\s*//Field offset: 0x([0-9A-Fa-f]+)", cs_text)
        if m:
            found[name] = (int(m.group(1), 16), 0)
    names = [n for n in names if not n.startswith("field:")]
    for i, line in enumerate(lines):
        for name in names:
            if name in found or not re.search(rf"\s{re.escape(name)}\s*\(", line) or "Token" in line:
                continue
            for back in range(i - 1, max(i - 4, -1), -1):
                m = _ADDRESS.search(lines[back])
                if m:
                    found[name] = (int(m.group(1), 16), int(m.group(2), 16))
                    break
    return found


def _last_error(output: str) -> str:
    """The exception line of Cpp2IL's output (its stack trace isn't useful to a user)."""
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    msg = next((ln for ln in reversed(lines) if "Exception" in ln and not ln.startswith("at ")), None)
    return (msg or (lines[-1] if lines else "no output"))[:300]


def _plain_version(version: str) -> str:
    """6000.2.7f2 -> 6000.2.7 (what Cpp2IL's --force-unity-version takes)."""
    m = re.match(r"\d+\.\d+\.\d+", version)
    return m.group(0) if m else version


def _cache_file(lib: bytes, metadata: bytes) -> Path:
    key = hashlib.sha256(metadata + len(lib).to_bytes(8, "little")).hexdigest()[:32]
    return cache_dir() / "il2cpp" / (key + ".json")


def find_methods(lib: bytes, metadata: bytes, unity_version: str, wanted: dict[str, list[str]],
                 cpp2il: Path | None = None, timeout: float = 900) -> dict[str, dict[str, tuple[int, int]]]:
    """wanted: {class file (relative to Cpp2IL's DiffableCs folder, e.g. "Unity.TextMeshPro/TMPro/TMP_InputField.cs"):
    [method names]} -> {class file: {method: (file offset, length)}}; classes or methods not in the game are left
    out."""
    cache_file = _cache_file(lib, metadata)
    cached = {}
    try:
        cached = json.loads(cache_file.read_text())
        if "error" in cached:  # Legacy entries cached a failed attempt as if it were a permanent result.
            cached.pop("error")
            if cached:
                cache_file.write_text(json.dumps(cached))
            else:
                cache_file.unlink(missing_ok=True)
        classes = cached.get("classes", {})
        missing = set(cached.get("missing", []))
        searched = cached.get("searched", {})
        if all(cls in classes or cls in missing for cls in wanted) and all(
                cls in missing or set(names) <= set(classes.get(cls, {})) | set(searched.get(cls, []))
                for cls, names in wanted.items()):
            return {cls: {m: tuple(v) for m, v in cached["classes"][cls].items() if m in names}
                    for cls, names in wanted.items() if cls in cached.get("classes", {})}
    except (OSError, ValueError, KeyError):
        pass
    failures = _attempt_failures.get()
    if failures is not None and cache_file in failures:
        raise RuntimeError(failures[cache_file])

    out = {c: {m: tuple(v) for m, v in methods.items()} for c, methods in cached.get("classes", {}).items()}
    missing = set(cached.get("missing", []))
    searched = {c: set(names) for c, names in cached.get("searched", {}).items()}
    try:
        if cpp2il is None:
            from ..tools import cpp2il as tool

            cpp2il = tool.ensure()
        env = os.environ.copy()
        if "DOTNET_BUNDLE_EXTRACT_BASE_DIR" not in env:
            extract_dir = cache_dir() / "il2cpp" / "dotnet-bundle"
            extract_dir.mkdir(parents=True, exist_ok=True)
            env["DOTNET_BUNDLE_EXTRACT_BASE_DIR"] = str(extract_dir)
        with tempfile.TemporaryDirectory(prefix="frameport-il2cpp-") as tmp:
            t = Path(tmp)
            (t / "libil2cpp.so").write_bytes(lib)
            (t / "global-metadata.dat").write_bytes(metadata)
            proc = subprocess.run([str(cpp2il), "--force-binary-path", str(t / "libil2cpp.so"),
                                   "--force-metadata-path", str(t / "global-metadata.dat"),
                                   "--force-unity-version", _plain_version(unity_version),
                                   "--use-processor", "attributeinjector", "--output-as", "diffable-cs",
                                   "--output-to", str(t / "out")], capture_output=True, text=True, errors="replace",
                                  timeout=timeout, cwd=tmp, env=env)
            root = t / "out" / "DiffableCs"
            if not root.is_dir():
                output = proc.stdout + proc.stderr
                detail = _last_error(output)
                if not output.strip():
                    detail = f"Cpp2IL exited with code {proc.returncode} and produced no output"
                error = "Cpp2IL couldn't read this game's code: " + detail
                raise RuntimeError(error)
            for cls, names in wanted.items():
                searched.setdefault(cls, set()).update(names)
                f = root / cls
                if f.exists():
                    text = f.read_text(encoding="utf-8", errors="replace")
                    out.setdefault(cls, {}).update(parse_methods(text, names))
                    missing.discard(cls)
                else:
                    out.pop(cls, None)
                    missing.add(cls)
    except Exception as exc:
        _remember_failure(cache_file, str(exc))
        raise
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps({"unity_version": unity_version, "missing": sorted(missing),
                                      "searched": {c: sorted(names) for c, names in searched.items()},
                                      "classes": {c: {m: list(v) for m, v in ms.items()} for c, ms in out.items()}}))
    return {cls: {m: out[cls][m] for m in names if m in out.get(cls, {})} for cls, names in wanted.items()
            if cls in out}
