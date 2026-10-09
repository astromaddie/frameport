import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from frameport import build as frame_build
from frameport.analysis import il2cpp

CLASS_FILE = "Oculus.VR/OVROverlay.cs"
CS = '''public class OVROverlay
{
\t[Address(RVA = "0x100", Offset = "0x100", Length = "0x20")]
\t[Token(Token = "0x1")]
\tprivate bool PopulateLayer() { }
}
'''


def _write_output(args):
    output = Path(args[args.index("--output-to") + 1]) / "DiffableCs" / CLASS_FILE
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(CS)


def test_legacy_error_cache_retries_through_tool_ensure_and_then_caches_success(monkeypatch, tmp_path):
    lib, metadata = b"legacy-lib", b"legacy-metadata"
    monkeypatch.setattr(il2cpp, "cache_dir", lambda: tmp_path / "cache")
    cache_file = il2cpp._cache_file(lib, metadata)
    cache_file.parent.mkdir(parents=True)
    cache_file.write_text(json.dumps({"unity_version": "2021.3.5f1", "error": "old no-output failure"}))
    ensure_calls = []
    monkeypatch.setattr("frameport.tools.cpp2il.ensure", lambda: ensure_calls.append(True) or tmp_path / "Cpp2IL")
    run_calls = []

    def cpp2il(args, **_kwargs):
        run_calls.append(args)
        _write_output(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(il2cpp.subprocess, "run", cpp2il)
    wanted = {CLASS_FILE: ["PopulateLayer"]}

    found = il2cpp.find_methods(lib, metadata, "2021.3.5f1", wanted)
    cached = il2cpp.find_methods(lib, metadata, "2021.3.5f1", wanted)

    assert found == cached == {CLASS_FILE: {"PopulateLayer": (0x100, 0x20)}}
    assert ensure_calls == [True]
    assert len(run_calls) == 1
    stored = json.loads(cache_file.read_text())
    assert "error" not in stored and stored["classes"][CLASS_FILE]["PopulateLayer"] == [0x100, 0x20]


def test_failed_finder_attempt_is_deduplicated_per_build_and_retried_on_next_build(monkeypatch, tmp_path):
    lib, metadata = b"build-lib", b"build-metadata"
    wanted = {CLASS_FILE: ["PopulateLayer"]}
    monkeypatch.setattr(il2cpp, "cache_dir", lambda: tmp_path / "cache")
    run_calls = []

    def cpp2il(args, **_kwargs):
        run_calls.append(args)
        if len(run_calls) == 1:
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        _write_output(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(il2cpp.subprocess, "run", cpp2il)

    def find_twice_in_frame_fix_pass(*_args):
        failures = []
        for _ in range(2):
            try:
                il2cpp.find_methods(lib, metadata, "2021.3.5f1", wanted, cpp2il=tmp_path / "Cpp2IL")
            except RuntimeError as exc:
                failures.append(str(exc))
        return failures

    source_apk = tmp_path / "source.apk"
    source_apk.write_bytes(b"test apk")

    def apply_fixes(apk_in, apk_out, *_args):
        apk_out.write_bytes(apk_in.read_bytes())
        return find_twice_in_frame_fix_pass(), [], {}

    monkeypatch.setattr(frame_build, "work_dir", lambda: tmp_path / "work")
    monkeypatch.setattr(frame_build, "apply_frame_fixes", apply_fixes)
    monkeypatch.setattr(frame_build.sign, "sign", lambda src, dst, _pkg: dst.write_bytes(src.read_bytes()))
    monkeypatch.setattr(frame_build, "check_apk", lambda *_args, **_kwargs: [])

    class Reporter:
        def stage(self, *_args):
            pass

        def log(self, *_args):
            pass

        def check(self, *_args):
            pass

    source = SimpleNamespace(apk=source_apk)
    analysis = SimpleNamespace(package="test.package", is_overport_output=False)
    recipe = SimpleNamespace(overport=False, alt_patches=[], patches={})
    results = []

    for build_number in range(2):
        results.append(frame_build.build(source, analysis, recipe, tmp_path / f"out-{build_number}", Reporter()))

    assert all(result.apk.is_file() for result in results)
    # One subprocess in the first build despite two selected-patch finder calls; a fresh attempt in build two,
    # then the successful method result is shared by the second finder call and persisted for later builds.
    assert len(run_calls) == 2
    assert results[0].applied == [
        "Cpp2IL couldn't read this game's code: Cpp2IL exited with code 1 and produced no output",
        "Cpp2IL couldn't read this game's code: Cpp2IL exited with code 1 and produced no output",
    ]
    assert results[1].applied == []
    assert json.loads(il2cpp._cache_file(lib, metadata).read_text())["classes"][CLASS_FILE]["PopulateLayer"] \
        == [0x100, 0x20]


def test_failure_message_includes_exit_code_when_cpp2il_is_silent(monkeypatch, tmp_path):
    lib, metadata = b"silent-lib", b"silent-metadata"
    monkeypatch.setattr(il2cpp, "cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(il2cpp.subprocess, "run",
                        lambda *_args, **_kwargs: SimpleNamespace(returncode=7, stdout="", stderr=""))

    with pytest.raises(RuntimeError, match="exited with code 7 and produced no output"):
        il2cpp.find_methods(lib, metadata, "2021.3.5f1", {CLASS_FILE: ["PopulateLayer"]},
                            cpp2il=tmp_path / "Cpp2IL")

    assert not il2cpp._cache_file(lib, metadata).exists()


def test_cpp2il_extract_directory_is_private_to_cache_and_writable(monkeypatch, tmp_path):
    cache = tmp_path / "cache"
    monkeypatch.setattr(il2cpp, "cache_dir", lambda: cache)
    monkeypatch.delenv("DOTNET_BUNDLE_EXTRACT_BASE_DIR", raising=False)
    monkeypatch.setenv("FRAMEPORT_TEST_INHERITED", "kept")
    observed = {}

    def cpp2il(args, **kwargs):
        observed.update(kwargs["env"])
        extract_dir = Path(observed["DOTNET_BUNDLE_EXTRACT_BASE_DIR"])
        assert extract_dir == cache / "il2cpp" / "dotnet-bundle"
        assert extract_dir.is_dir()
        _write_output(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(il2cpp.subprocess, "run", cpp2il)
    found = il2cpp.find_methods(b"lib", b"metadata", "2021.3.5f1", {CLASS_FILE: ["PopulateLayer"]},
                                cpp2il=tmp_path / "Cpp2IL")

    assert found == {CLASS_FILE: {"PopulateLayer": (0x100, 0x20)}}
    assert observed["FRAMEPORT_TEST_INHERITED"] == "kept"


def test_cpp2il_extract_directory_respects_explicit_override(monkeypatch, tmp_path):
    cache = tmp_path / "cache"
    override = tmp_path / "caller-selected-dotnet-cache"
    monkeypatch.setattr(il2cpp, "cache_dir", lambda: cache)
    monkeypatch.setenv("DOTNET_BUNDLE_EXTRACT_BASE_DIR", str(override))

    def cpp2il(args, **kwargs):
        assert kwargs["env"]["DOTNET_BUNDLE_EXTRACT_BASE_DIR"] == str(override)
        assert not (cache / "il2cpp" / "dotnet-bundle").exists()
        _write_output(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(il2cpp.subprocess, "run", cpp2il)
    found = il2cpp.find_methods(b"override-lib", b"override-metadata", "2021.3.5f1",
                                {CLASS_FILE: ["PopulateLayer"]}, cpp2il=tmp_path / "Cpp2IL")

    assert found == {CLASS_FILE: {"PopulateLayer": (0x100, 0x20)}}


def test_extract_directory_creation_failure_is_build_scoped_only(monkeypatch, tmp_path):
    cache = tmp_path / "cache"
    extract_dir = cache / "il2cpp" / "dotnet-bundle"
    monkeypatch.setattr(il2cpp, "cache_dir", lambda: cache)
    monkeypatch.delenv("DOTNET_BUNDLE_EXTRACT_BASE_DIR", raising=False)
    run_calls = []

    def cpp2il(args, **_kwargs):
        run_calls.append(args)
        _write_output(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(il2cpp.subprocess, "run", cpp2il)
    original_mkdir = Path.mkdir

    def deny_extract_dir(path, *args, **kwargs):
        if path == extract_dir:
            raise PermissionError("test extraction directory is not writable")
        return original_mkdir(path, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "mkdir", deny_extract_dir)
        with il2cpp.cpp2il_attempt_scope():
            with pytest.raises(PermissionError, match="not writable") as first:
                il2cpp.find_methods(b"blocked-lib", b"blocked-metadata", "2021.3.5f1",
                                    {CLASS_FILE: ["PopulateLayer"]}, cpp2il=tmp_path / "Cpp2IL")
            with pytest.raises(RuntimeError, match="not writable") as second:
                il2cpp.find_methods(b"blocked-lib", b"blocked-metadata", "2021.3.5f1",
                                    {CLASS_FILE: ["PopulateLayer"]}, cpp2il=tmp_path / "Cpp2IL")
            assert str(first.value) == str(second.value)

    cache_file = il2cpp._cache_file(b"blocked-lib", b"blocked-metadata")
    assert not cache_file.exists()
    found = il2cpp.find_methods(b"blocked-lib", b"blocked-metadata", "2021.3.5f1",
                                {CLASS_FILE: ["PopulateLayer"]}, cpp2il=tmp_path / "Cpp2IL")
    assert found == {CLASS_FILE: {"PopulateLayer": (0x100, 0x20)}}
    assert len(run_calls) == 1
