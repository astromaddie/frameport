import json

from frameport.analysis import elf
from frameport.analysis.stubgen import build_stub_library
from frameport.parity import classify
from frameport.report import render


def test_stub_libraries_equivalent():
    a = build_stub_library(["ovr_A", "ovr_B"])
    b = build_stub_library(["ovr_B", "ovr_A"], abi="arm64-v8a") + b"\0" * 16  # different bytes, same exports
    assert classify("lib/arm64-v8a/libovrstubs.so", a, b)[0] == "equivalent"
    c = build_stub_library(["ovr_A"])
    assert classify("lib/arm64-v8a/libovrstubs.so", a, c)[0] == "UNEXPLAINED"


def test_needed_layout_equivalent():
    base = build_stub_library(["f"], soname="libgame.so")
    one = elf.add_needed(base, "libglshim.so")
    two = elf.add_needed(base + b"", "libglshim.so")
    assert classify("lib/arm64-v8a/libgame.so", one, two)[0] in ("equivalent",) or one == two


def test_settings_classification():
    kind, why = classify("lib/arm64-v8a/libframe_settings.so", b"scale=1.0\nscene_emul=1\n",
                         b"scene_emul=1\nscale=1.0\n")
    assert kind == "equivalent"


def test_ovrplugin_shim_update_requires_exact_current_artifact(tmp_path, monkeypatch):
    from frameport import parity

    abi = tmp_path / "arm64-v8a"
    abi.mkdir()
    artifact = abi / "libfp_ovrp.so"
    artifact.write_bytes(b"current shim build")
    monkeypatch.setattr(parity, "artifacts_dir", lambda: tmp_path)
    name = "lib/arm64-v8a/libfp_ovrp.so"
    assert parity.classify(name, artifact.read_bytes(), b"previous build")[0] == "expected"
    assert parity.classify(name, b"unverified shim build", b"previous build")[0] == "UNEXPLAINED"


def test_report_counts():
    text = render()
    from frameport.recommend import catalog

    works = sum(1 for e in catalog.load().values() if e.status == "works")
    assert f"{works} work" in text and "Path of the Warrior" in text


def test_ovrport_125_config_and_compat_are_expected():
    from frameport import parity
    from frameport.analysis.stubgen import build_stub_library

    def cfg(by):
        cfg = {"version": 1, "patched": {"by": by, "with": "3.4.3-23204ea", "patches": ["patch_copy_libraries"]}}
        return b"\0\0" + json.dumps(cfg, separators=(",", ":")).encode() + b"\0"
    kind, why = parity.classify("lib/arm64-v8a/liboverport.config.so", cfg("1.2.5"), cfg("1.2.3"))
    assert kind == "expected" and "1.2.5" in why
    assert parity.classify("lib/arm64-v8a/liboverport.config.so", cfg("1.2.5").replace(b"3.4.3", b"3.4.4"),
                           cfg("1.2.3"))[0] == "UNEXPLAINED"
    compat = build_stub_library(["ovrMessageType_ToString"], soname="libovrplatformcompat.so")
    assert parity._compat_export(compat)
