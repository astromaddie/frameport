"""Asset-free safety checks; full bundle parity is also checked by the opt-in tool."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/prepare_csmash_video_trial.py"
spec = importlib.util.spec_from_file_location("csmash_video_trial", SCRIPT)
trial = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trial)


@pytest.mark.parametrize("offset,size", [(0, 16), (31, 21), (240, 16)])
def test_replacement_preserves_every_byte_outside_selected_clip(offset, size):
    stream = bytes(range(256))
    other_extents = []
    if offset:
        other_extents.append((0, offset))
    if offset + size < len(stream):
        other_extents.append((offset + size, len(stream) - offset - size))
    result = trial.replace_extent(stream, offset, size, b"video", other_extents)
    assert len(result) == len(stream)
    assert result[:offset] == stream[:offset]
    assert result[offset + size:] == stream[offset + size:]
    assert result[offset:offset + size] == b"video" + bytes(size - 5)


@pytest.mark.parametrize("offset,size,replacement,other", [
    (-1, 5, b"v", []), (0, 0, b"v", []), (250, 7, b"v", []),
    (1, 5, b"", []), (1, 5, b"toolong", []),
    (16, 8, b"v", [(15, 2)]), (16, 8, b"v", [(23, 2)]),
    (16, 8, b"v", [(17, 1)]), (16, 8, b"v", [(0, 256)]),
    (16, 8, b"v", [(0, 0)]), (16, 8, b"v", [(255, 2)]),
])
def test_invalid_or_shared_extents_fail_closed(offset, size, replacement, other):
    with pytest.raises(ValueError):
        trial.replace_extent(bytes(range(256)), offset, size, replacement, other)


def test_wrong_bundle_rejected_without_creating_output(tmp_path, monkeypatch):
    source = tmp_path / "bundle"
    source.write_bytes(b"different game or version")
    output = tmp_path / "output"
    monkeypatch.setattr(trial.subprocess, "run", lambda *a, **kw: pytest.fail("must not transcode"))
    with pytest.raises(ValueError, match="unsupported videos bundle"):
        trial.build(source, output)
    assert not output.exists()
    assert source.read_bytes() == b"different game or version"


def test_existing_output_is_never_overwritten(tmp_path, monkeypatch):
    source = tmp_path / "bundle"
    source.write_bytes(b"input")
    monkeypatch.setattr(trial, "INPUT_SHA256", trial.sha(source.read_bytes()))
    output = tmp_path / "output"
    output.mkdir()
    saved = output / "receipt.json"
    saved.write_bytes(b"prior result")
    with pytest.raises(FileExistsError):
        trial.build(source, output)
    assert saved.read_bytes() == b"prior result"
