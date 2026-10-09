from pathlib import Path
from types import SimpleNamespace

import pytest

from frameport.tools import cpp2il


def _result(code=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


def _managed_tool(root: Path) -> Path:
    exe = root / "cpp2il-2022.1.0-pre-release.21" / "Cpp2IL"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"private test executable")
    return exe


def test_existing_managed_macos_tool_is_ad_hoc_signed_and_verified(monkeypatch, tmp_path):
    exe = _managed_tool(tmp_path / "tools")
    monkeypatch.setattr(cpp2il, "tools_dir", lambda: tmp_path / "tools")
    monkeypatch.setattr(cpp2il.sys, "platform", "darwin")
    monkeypatch.delenv("FRAMEPORT_CPP2IL", raising=False)
    calls = []
    results = [_result(1, stderr="code object is not signed at all"), _result(), _result()]

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return results.pop(0)

    monkeypatch.setattr(cpp2il.subprocess, "run", run)

    assert cpp2il.ensure() == exe
    assert [call[0][1] for call in calls] == ["--verify", "--force", "--verify"]
    assert calls[1][0][2:5] == ["--sign", "-", "--timestamp=none"]
    assert all(call[0][-1] == str(exe) for call in calls)


def test_existing_valid_managed_macos_signature_is_preserved(monkeypatch, tmp_path):
    exe = _managed_tool(tmp_path / "tools")
    monkeypatch.setattr(cpp2il, "tools_dir", lambda: tmp_path / "tools")
    monkeypatch.setattr(cpp2il.sys, "platform", "darwin")
    monkeypatch.delenv("FRAMEPORT_CPP2IL", raising=False)
    before = exe.read_bytes()
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return _result()

    monkeypatch.setattr(cpp2il.subprocess, "run", run)

    assert cpp2il.ensure() == exe
    assert len(calls) == 1 and calls[0][0][1] == "--verify"
    assert exe.read_bytes() == before


def test_fresh_macos_download_is_signed_after_download_and_chmod(monkeypatch, tmp_path):
    tools = tmp_path / "tools"
    monkeypatch.setattr(cpp2il, "tools_dir", lambda: tools)
    monkeypatch.setattr(cpp2il, "asset_suffix", lambda: "OSX-ARM64")
    monkeypatch.setattr(cpp2il.sys, "platform", "darwin")
    monkeypatch.delenv("FRAMEPORT_CPP2IL", raising=False)
    monkeypatch.setattr(cpp2il.cache, "cached_json", lambda *_args, **_kwargs: [
        {"tag_name": "2022.1.0-pre-release.21", "assets": [
            {"name": "Cpp2IL-2022.1.0-pre-release.21-OSX-ARM64", "browser_download_url": "https://example/tool"}]}])
    downloads = []

    def download(url, dest, progress):
        downloads.append((url, dest, progress))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"downloaded test executable")

    monkeypatch.setattr(cpp2il.cache, "download", download)
    calls = []
    results = [_result(1, stderr="code object is not signed at all"), _result(), _result()]

    def run(args, **kwargs):
        calls.append((args, kwargs))
        assert downloads and downloads[0][1] == Path(args[-1])
        return results.pop(0)

    monkeypatch.setattr(cpp2il.subprocess, "run", run)
    progress = object()

    exe = cpp2il.ensure(progress)

    assert exe == tools / "cpp2il-2022.1.0-pre-release.21" / "Cpp2IL"
    assert downloads == [("https://example/tool", exe, progress)]
    assert exe.stat().st_mode & 0o111
    assert len(calls) == 3 and calls[1][0][2:5] == ["--sign", "-", "--timestamp=none"]


def test_explicit_override_is_returned_without_codesign_or_rewrite(monkeypatch, tmp_path):
    override = tmp_path / "user-owned-cpp2il"
    override.write_bytes(b"caller-owned executable")
    before = override.read_bytes()
    monkeypatch.setenv("FRAMEPORT_CPP2IL", str(override))
    monkeypatch.setattr(cpp2il.sys, "platform", "darwin")
    monkeypatch.setattr(cpp2il.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("must not sign override"))

    assert cpp2il.ensure() == override
    assert override.read_bytes() == before


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_non_macos_cached_tool_is_not_signed(monkeypatch, tmp_path, platform):
    exe = _managed_tool(tmp_path / "tools")
    monkeypatch.setattr(cpp2il, "tools_dir", lambda: tmp_path / "tools")
    monkeypatch.setattr(cpp2il.sys, "platform", platform)
    monkeypatch.delenv("FRAMEPORT_CPP2IL", raising=False)
    monkeypatch.setattr(cpp2il.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("not a Mac"))

    assert cpp2il.ensure() == exe


def test_signing_failure_is_actionable_and_does_not_return_tool(monkeypatch, tmp_path):
    _managed_tool(tmp_path / "tools")
    monkeypatch.setattr(cpp2il, "tools_dir", lambda: tmp_path / "tools")
    monkeypatch.setattr(cpp2il.sys, "platform", "darwin")
    monkeypatch.delenv("FRAMEPORT_CPP2IL", raising=False)
    calls = iter([_result(1, stderr="unsigned"), _result(1, stderr="resource fork rejected")])
    monkeypatch.setattr(cpp2il.subprocess, "run", lambda *_args, **_kwargs: next(calls))

    with pytest.raises(RuntimeError, match="ad-hoc signing failed: resource fork rejected"):
        cpp2il.ensure()


def test_missing_codesign_has_actionable_error(monkeypatch, tmp_path):
    _managed_tool(tmp_path / "tools")
    monkeypatch.setattr(cpp2il, "tools_dir", lambda: tmp_path / "tools")
    monkeypatch.setattr(cpp2il.sys, "platform", "darwin")
    monkeypatch.delenv("FRAMEPORT_CPP2IL", raising=False)

    def run(*_args, **_kwargs):
        raise FileNotFoundError("codesign")

    monkeypatch.setattr(cpp2il.subprocess, "run", run)

    with pytest.raises(RuntimeError, match="codesign command is required.*Command Line Tools"):
        cpp2il.ensure()


def test_bad_signature_after_signing_fails_closed(monkeypatch, tmp_path):
    _managed_tool(tmp_path / "tools")
    monkeypatch.setattr(cpp2il, "tools_dir", lambda: tmp_path / "tools")
    monkeypatch.setattr(cpp2il.sys, "platform", "darwin")
    monkeypatch.delenv("FRAMEPORT_CPP2IL", raising=False)
    calls = iter([_result(1, stderr="unsigned"), _result(), _result(1, stderr="invalid signature")])
    monkeypatch.setattr(cpp2il.subprocess, "run", lambda *_args, **_kwargs: next(calls))

    with pytest.raises(RuntimeError, match="signature verification failed: invalid signature"):
        cpp2il.ensure()
