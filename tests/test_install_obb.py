import hashlib
import struct
import zipfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from frameport.core.events import Reporter
from frameport.core.models import Recipe
from frameport.install import installer, obb


def _obb(payload=b"payload", owner="com.firaxis.invernessdemo", *, flags=0, version=1, owner_bytes=None):
    owner_bytes = owner.encode() if owner_bytes is None else owner_bytes
    footer_size = 24 + len(owner_bytes)
    footer = (struct.pack("<III8sI", version, 147794511, flags, b"12345678", len(owner_bytes)) + owner_bytes
              + struct.pack("<II", footer_size, obb.OBB_SIGNATURE))
    return payload + footer


def test_rebase_changes_only_obb_v1_footer_and_preserves_source(tmp_path):
    source = tmp_path / "main.obb"
    destination = tmp_path / "staged/main.obb"
    original = _obb(b"fixture-payload\0\x02", flags=obb.OBB_OVERLAY)
    source.write_bytes(original)
    original_hash = hashlib.sha256(original).hexdigest()

    rewrite = obb.plan_owner_rewrite(source, "com.mrf.firaxis.invernessdemo")
    assert rewrite is not None
    assert obb.write_owner_rewrite(source, destination, rewrite) == len(original) + 4

    result = destination.read_bytes()
    footer = obb.inspect_footer(destination)
    assert footer is not None
    assert footer.owner == "com.mrf.firaxis.invernessdemo"
    assert footer.version == 1 and footer.package_version == 147794511
    assert footer.flags == obb.OBB_OVERLAY and footer.salt == b"12345678"
    assert result[:rewrite.footer_start] == b"fixture-payload\0\x02"
    assert len(result) == len(original) + 4
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash


def test_matching_owner_and_ordinary_zip_need_no_rewrite(tmp_path):
    matching = tmp_path / "matching.obb"
    matching.write_bytes(_obb(owner="com.target.game"))
    assert obb.plan_owner_rewrite(matching, "com.target.game") is None

    ordinary_zip = tmp_path / "ordinary.obb"
    with zipfile.ZipFile(ordinary_zip, "w") as archive:
        archive.writestr("content.bin", b"ordinary zip expansion")
    assert obb.inspect_footer(ordinary_zip) is None
    assert obb.plan_owner_rewrite(ordinary_zip, "com.target.game") is None


def test_unrecognized_terminal_signature_is_left_untouched(tmp_path):
    source = tmp_path / "unknown.obb"
    original = _obb(b"fixture", owner="com.old.game")[:-4] + struct.pack("<I", 0xDEADBEEF)
    source.write_bytes(original)
    assert obb.inspect_footer(source) is None
    assert obb.plan_owner_rewrite(source, "com.target.game") is None
    assert source.read_bytes() == original


@pytest.mark.parametrize(("contents", "message"), [
    (_obb(b"x")[:-8] + struct.pack("<II", 0xFFFFFFFF, obb.OBB_SIGNATURE), "footer bounds"),
    (_obb(b"x", owner_bytes=b"\xff"), "not valid UTF-8"),
    (_obb(b"x", version=2), "unsupported Android OBB footer version"),
])
def test_malformed_and_unknown_aosp_footers_are_rejected(tmp_path, contents, message):
    source = tmp_path / "bad.obb"
    source.write_bytes(contents)
    with pytest.raises(obb.ObbFormatError, match=message):
        obb.plan_owner_rewrite(source, "com.target.game")


@pytest.mark.parametrize(("flags", "message"), [
    (obb.OBB_SALTED, "encrypted OBBs cannot be retargeted safely"),
    (1 << 7, "include unsupported bits"),
])
def test_encrypted_or_unknown_flagged_obb_is_not_rewritten(tmp_path, flags, message):
    source = tmp_path / "flagged.obb"
    source.write_bytes(_obb(flags=flags))
    with pytest.raises(obb.ObbFormatError, match=message):
        obb.plan_owner_rewrite(source, "com.target.game")


class _InstallFrame:
    def __init__(self, apk_size, obb_sizes=None, same_apk=False):
        self.prep = {"lepton": True, "incoming": "/base/incoming", "existing_obb": obb_sizes or {},
                     "same_apk": same_apk, "free_bytes": 10 << 30, "base": "/base"}
        self.apk_size = apk_size
        self.uploaded = {}
        self.finalize = None

    def agent(self, command, **args):
        if command == "prepare":
            return self.prep
        assert command == "finalize"
        self.finalize = args
        return {"base": "/base", "appid": 42}

    def put(self, local, remote, progress=None, **kwargs):
        payload = Path(local).read_bytes()
        self.uploaded[remote] = payload
        if progress:
            progress(len(payload), len(payload))

    def mkdirs(self, _dirs):
        pass

    def put_tar(self, files, _root, progress=None):
        for local, rel in files:
            payload = Path(local).read_bytes()
            self.uploaded[rel] = payload
            if progress:
                progress(len(payload), rel)


def _stub_install_side_effects(monkeypatch):
    @contextmanager
    def transfer_link(frame, _reporter, need):
        transfer_link.need = need
        yield frame

    monkeypatch.setattr(installer, "transfer_link", transfer_link)
    monkeypatch.setattr(installer.artwork, "fetch", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(installer, "upload_steam_art", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(installer, "install_context", lambda _recipe: SimpleNamespace(
        adapter_settings={}, files={}, env={}, display="vr", flatscreen=False))
    monkeypatch.setattr(installer, "show_window", lambda *_args: False)
    monkeypatch.setattr(installer, "_flatscreen", lambda _pkg: False)
    return transfer_link


@pytest.mark.parametrize(("old_owner", "package", "size_delta"), [
    ("com.firaxis.invernessdemo", "com.mrf.firaxis.invernessdemo", 4),
    ("com.foo.bar", "com.bar.foo", 0),  # equal-size owner changes must still replace incoming bytes.
])
def test_install_uploads_rebased_obb_bytes_and_manifest_size_without_mutating_source(
        monkeypatch, tmp_path, old_owner, package, size_delta):
    apk = tmp_path / "game.apk"
    apk.write_bytes(b"apk")
    data = tmp_path / "obb"
    data.mkdir()
    original = _obb(b"small private fixture", owner=old_owner)
    source = data / "main.obb"
    source.write_bytes(original)
    frame = _InstallFrame(apk.stat().st_size, {"main.obb": len(original)})
    transfer_link = _stub_install_side_effects(monkeypatch)

    installer.install(frame, installer.InstallPlan(package, "Test", apk, data, Recipe(package)), Reporter())

    uploaded = frame.uploaded["obb/main.obb"]
    staged = tmp_path / "uploaded.obb"
    staged.write_bytes(uploaded)
    info = obb.inspect_footer(staged)
    assert info is not None and info.owner == package
    assert uploaded[:len(b"small private fixture")] == b"small private fixture"
    assert len(uploaded) == len(original) + size_delta
    assert source.read_bytes() == original
    assert frame.finalize["obb_manifest"] == {"main.obb": len(uploaded)}
    assert transfer_link.need == apk.stat().st_size + len(uploaded)


def test_apk_only_reuses_remote_data_without_assuming_local_obb_files(monkeypatch, tmp_path):
    apk = tmp_path / "game.apk"
    apk.write_bytes(b"apk")
    frame = _InstallFrame(apk.stat().st_size, {"old.obb": 77}, same_apk=True)
    _stub_install_side_effects(monkeypatch)
    monkeypatch.setattr(installer, "local_data_manifest", lambda *_args, **_kwargs: pytest.fail(
        "apk_only must not inspect a local data directory"))
    reporter = Reporter()
    logs = []
    reporter.subscribe(lambda event: logs.append(event.message) if event.kind == "log" else None)

    installer.install(frame, installer.InstallPlan("com.target.game", "Test", apk, None, Recipe("com.target.game"),
                                                    apk_only=True), reporter)

    assert not frame.uploaded
    assert frame.finalize["obb_manifest"] is None
    assert any("local OBB owner correction was skipped" in message for message in logs)


def test_install_reports_encrypted_owner_mismatch_before_upload(monkeypatch, tmp_path):
    apk = tmp_path / "game.apk"
    apk.write_bytes(b"apk")
    data = tmp_path / "obb"
    data.mkdir()
    source = data / "main.obb"
    original = _obb(flags=obb.OBB_SALTED)
    source.write_bytes(original)
    frame = _InstallFrame(apk.stat().st_size)
    _stub_install_side_effects(monkeypatch)

    with pytest.raises(RuntimeError, match="can't safely correct OBB main.obb: .*encrypted OBBs"):
        installer.install(frame, installer.InstallPlan("com.target.game", "Test", apk, data, Recipe("com.target.game")),
                          Reporter())

    assert not frame.uploaded and source.read_bytes() == original


def test_large_rewritten_file_does_not_resume_a_same_size_stale_part(tmp_path):
    class Frame:
        def __init__(self):
            self.resume = None

        def mkdirs(self, _dirs):
            pass

        def put(self, _local, _remote, progress=None, resume=True, **_kwargs):
            self.resume = resume
            if progress:
                progress(installer.BIG_FILE + 1, installer.BIG_FILE + 1)

    large = tmp_path / "large.obb"
    with large.open("wb") as stream:
        stream.truncate(installer.BIG_FILE + 1)
    frame = Frame()
    installer.upload_files(frame, [(large, "obb/large.obb", large.stat().st_size)], "/incoming", Reporter(),
                           large.stat().st_size, restart={"obb/large.obb"})
    assert frame.resume is False
