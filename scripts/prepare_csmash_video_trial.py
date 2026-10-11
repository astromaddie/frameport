"""Opt-in C-Smash 1.2.0.7 video trial. Requires privately supplied game data.

The sole 4K tutorial clip feeds an existing fixed 1280x720 RenderTexture. Resize
that clip offline, preserving its cadence and effective Android BT2020 color.
This is experimental: codec throughput is not in-game frame-rate acceptance.
No device operations, APK changes, signing keys, or game assets are included.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import subprocess
from pathlib import Path

INPUT_SHA256 = "cfcd39939ec752b5151fc82af2d1a92dabeea72d5e85c36f3eac8a0882e42a7c"
CLIP = "CSVRS - Ball Coaxing"
FILTER = ("setparams=colorspace=bt2020nc:color_primaries=bt2020:color_trc=bt709:range=limited,"
          "scale=1280:720:flags=bilinear:in_color_matrix=bt2020:out_color_matrix=bt2020:in_range=tv:out_range=tv")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def replace_extent(stream: bytes, offset: int, size: int, replacement: bytes,
                   other_extents: list[tuple[int, int]]) -> bytes:
    """Keep other clips at their original offsets; zero-pad the selected extent."""
    if offset < 0 or size <= 0 or offset + size > len(stream) or not 0 < len(replacement) <= size:
        raise ValueError("invalid replacement extent or replacement is too large")
    for start, length in other_extents:
        if start < 0 or length <= 0 or start + length > len(stream):
            raise ValueError("invalid other clip extent")
        if start < offset + size and start + length > offset:
            raise ValueError("selected clip shares bytes with another clip")
    return stream[:offset] + replacement + bytes(size - len(replacement)) + stream[offset + size:]


def build(source: Path, directory: Path) -> dict:
    raw = source.read_bytes()
    if sha(raw) != INPUT_SHA256:
        raise ValueError("unsupported videos bundle; expected C-Smash 1.2.0.7 exact input hash")
    if directory.exists():
        raise FileExistsError("output directory must not already exist")

    import UnityPy
    from UnityPy.helpers.ResourceReader import get_resource_data
    from UnityPy.streams import EndianBinaryReader

    if UnityPy.__version__ != "1.25.4":
        raise ValueError("this reproducible trial requires UnityPy 1.25.4")
    directory.mkdir(parents=True)
    env = UnityPy.load(raw)
    bundle = next(iter(env.files.values()))
    objects = {(o.assets_file.name, o.path_id): o for o in env.objects}
    original_raw = {k: o.get_raw_data() for k, o in objects.items()}
    clips = [(k, o, o.read_typetree()) for k, o in objects.items() if o.type.name == "VideoClip"]
    selected = [(k, o, d) for k, o, d in clips if d["m_Name"] == CLIP]
    if len(clips) != 12 or len(selected) != 1:
        raise ValueError("unexpected clip inventory")
    key, obj, before = selected[0]
    if (before["Width"], before["Height"], before["m_ProxyWidth"], before["m_ProxyHeight"]) != (3840, 2160, 3840, 2160):
        raise ValueError("unexpected selected clip dimensions")
    resource = before["m_ExternalResources"]
    name = resource["m_Source"].rsplit("/", 1)[1]
    stream = bundle.files[name].bytes
    original = directory / "original.mp4"
    candidate = directory / "candidate.mp4"
    original.write_bytes(get_resource_data(resource["m_Source"], obj.assets_file,
                                          resource["m_Offset"], resource["m_Size"]))
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(original), "-vf", FILTER,
               "-an", "-c:v", "libx264", "-preset", "slow", "-crf", "12", "-profile:v", "main",
               "-bf", "0", "-refs", "1", "-fps_mode", "passthrough", "-colorspace", "bt2020nc",
               "-color_primaries", "bt2020", "-color_trc", "bt709", "-color_range", "tv",
               "-movflags", "+faststart", str(candidate)]
    subprocess.run(command, check=True)
    probe = lambda p: json.loads(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(p)]))  # noqa: E731
    a, b = probe(original), probe(candidate)
    if len(a["streams"]) != 1 or len(b["streams"]) != 1:
        raise ValueError("expected exactly one video stream")
    for field in ("codec_name", "pix_fmt", "avg_frame_rate", "nb_frames", "duration"):
        if a["streams"][0][field] != b["streams"][0][field]:
            raise ValueError(f"candidate changed {field}")
    expected = {"width": 1280, "height": 720, "color_range": "tv", "color_space": "bt2020nc",
                "color_transfer": "bt709", "color_primaries": "bt2020"}
    if any(b["streams"][0].get(k) != v for k, v in expected.items()):
        raise ValueError("candidate dimensions/color metadata differ from the tested path")
    other_extents = [(d["m_ExternalResources"]["m_Offset"], d["m_ExternalResources"]["m_Size"])
                     for k, _, d in clips if k != key]
    replacement = candidate.read_bytes()
    new_stream = replace_extent(stream, resource["m_Offset"], resource["m_Size"], replacement, other_extents)
    flags = bundle.files[name].flags
    bundle.files[name] = EndianBinaryReader(new_stream)
    bundle.files[name].flags = flags
    after = copy.deepcopy(before)
    after.update(Width=1280, Height=720, m_ProxyWidth=1280, m_ProxyHeight=720)
    after["m_ExternalResources"]["m_Size"] = len(replacement)
    obj.save_typetree(after)
    output = directory / "videos_assets_all.bundle"
    output.write_bytes(bundle.save(packer="lz4"))
    checked_env = UnityPy.load(str(output))
    checked = {(o.assets_file.name, o.path_id): o for o in checked_env.objects}
    if checked.keys() != objects.keys():
        raise ValueError("serialized object inventory changed")
    for k, o in checked.items():
        good = o.read_typetree() == after if k == key else o.get_raw_data() == original_raw[k]
        if not good:
            raise ValueError(f"unexpected serialized object change: {k}")
    if next(iter(checked_env.files.values())).files[name].bytes != new_stream:
        raise ValueError("reopened resource bytes differ")
    clip_parity = {}
    for k, _, d in clips:
        s = d["m_ExternalResources"]
        prior = stream[s["m_Offset"]:s["m_Offset"] + s["m_Size"]]
        c = checked[k].read_typetree()["m_ExternalResources"]
        result = get_resource_data(c["m_Source"], checked[k].assets_file, c["m_Offset"], c["m_Size"])
        if result != (replacement if k == key else prior):
            raise ValueError(f"unexpected clip payload change: {d['m_Name']}")
        clip_parity[d["m_Name"]] = {"before": sha(prior), "after": sha(result)}
    receipt = {"status": "experimental; needs real tutorial playback QA", "input_sha256": INPUT_SHA256,
               "output_sha256": sha(output.read_bytes()), "command": command,
               "ffmpeg": subprocess.check_output(["ffmpeg", "-version"], text=True).splitlines()[0],
               "UnityPy": UnityPy.__version__, "before": before, "after": after,
               "ffprobe_original": a, "ffprobe_candidate": b, "clips": clip_parity,
               "parity": "all other serialized objects and 11 other clip payloads byte-identical",
               "color": "explicit identical BT2020 limited-range input/output matrix; no gamut conversion",
               "apk_and_rendering": "unchanged; preserve existing callback repair and original MSAA"}
    (directory / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_bundle", type=Path)
    parser.add_argument("output_directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.source_bundle, args.output_directory), indent=2))
