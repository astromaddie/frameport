#!/usr/bin/env python3
"""Build/stage the exact installed IEYTD conditional-Begin experiment.

Never installs, signs, launches or copies a game off the Frame. `stage` runs on
Frame and writes a separate unsigned APK; signing/booking/rollback remain explicit.
"""
import argparse
import hashlib
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

SHIM_SHA = '321da502e0f8f46f0880aad39fe6b84bdef88f925164568814cf704a1b31a577'
APK_SHA = '8f3a0beaae4ad8a660c96fe2e5525b34c40f4493cb10371f2115136e93d47f6e'
OLD = b'libfp_ovrp.so\0'
BASE = b'libfp_base.so\0'
LIB = 'lib/arm64-v8a/'


def digest(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def renamed_base(data):
    if hashlib.sha256(data).hexdigest() != SHIM_SHA:
        raise ValueError('unknown installed shim; refusing replacement')
    if len(OLD) != len(BASE) or data.count(OLD) != 1 or BASE in data:
        raise ValueError('unexpected SONAME string layout')
    return data.replace(OLD, BASE)


def build(shim, ndk, output):
    root = Path(__file__).resolve().parents[1]
    source = root / 'native/legacy_begin_proxy/legacy_begin_proxy.c'
    base = renamed_base(shim.read_bytes())
    output.mkdir(parents=True, exist_ok=False)
    (output / 'libfp_base.so').write_bytes(base)
    tc = ndk / 'toolchains/llvm/prebuilt/darwin-x86_64/bin'
    subprocess.run([str(tc / 'aarch64-linux-android29-clang'), '-shared', '-fPIC', '-O2', '-Wall', '-Wextra',
                    '-Werror', '-fvisibility=hidden', '-Wl,-soname,libfp_ovrp.so', '-Wl,-z,max-page-size=16384',
                    '-Wl,--no-as-needed', str(output / 'libfp_base.so'), str(source), '-ldl', '-llog',
                    '-o', str(output / 'libfp_ovrp.so')], check=True)
    dynamic = subprocess.check_output([str(tc / 'llvm-readelf'), '-d', str(output / 'libfp_ovrp.so')], text=True)
    assert '[libfp_base.so]' in dynamic and '[libfp_ovrp.so]' in dynamic
    shutil.copy2(root / 'src/frameport/apk/workspace.py', output / 'workspace.py')
    shutil.copy2(__file__, output / Path(__file__).name)
    manifest = {'original_apk_sha256':APK_SHA, 'original_shim_sha256':SHIM_SHA,
                'source_sha256':digest(source), 'ndk':'27.2.12479018',
                'files':{p.name:{'sha256':digest(p), 'bytes':p.stat().st_size} for p in output.iterdir()}}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))


def stage(apk, bundle, unsigned):
    import zipfile
    manifest = json.loads((bundle / 'manifest.json').read_text())
    assert manifest['original_apk_sha256'] == APK_SHA and digest(apk) == APK_SHA, 'original APK changed'
    assert not unsigned.exists() and unsigned.resolve() != apk.resolve()
    for name, info in manifest['files'].items():
        assert '/' not in name and digest(bundle / name) == info['sha256'], 'bundle identity changed'
    spec = importlib.util.spec_from_file_location('trial_workspace', bundle / 'workspace.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with module.ApkWorkspace(apk) as ws:
        original = ws.read(LIB + 'libfp_ovrp.so')
        assert renamed_base(original) == (bundle / 'libfp_base.so').read_bytes()
        assert not ws.has(LIB + 'libfp_base.so'), 'base already present'
        ws.put(LIB + 'libfp_ovrp.so', (bundle / 'libfp_ovrp.so').read_bytes())
        ws.put(LIB + 'libfp_base.so', (bundle / 'libfp_base.so').read_bytes())
        ws.write(unsigned)
        with zipfile.ZipFile(unsigned) as result:
            for info in ws.infos.values():
                if info.is_dir() or module.SIGNATURE_FILE.match(info.filename):
                    continue
                if info.filename == LIB + 'libfp_ovrp.so':
                    continue
                final = result.getinfo(info.filename)
                assert (final.CRC, final.file_size) == (info.CRC, info.file_size), info.filename
            for name in ('libfp_ovrp.so', 'libfp_base.so'):
                assert result.read(LIB + name) == (bundle / name).read_bytes()
    print(json.dumps({'unsigned_apk':str(unsigned), 'sha256':digest(unsigned),
                      'changed_entries':[LIB+'libfp_ovrp.so', LIB+'libfp_base.so'],
                      'other_entry_crc_and_sizes_preserved':True}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    b = sub.add_parser('build')
    b.add_argument('--shim', type=Path, required=True)
    b.add_argument('--ndk', type=Path, required=True)
    b.add_argument('--output', type=Path, required=True)
    s = sub.add_parser('stage')
    s.add_argument('--apk', type=Path, required=True)
    s.add_argument('--bundle', type=Path, required=True)
    s.add_argument('--unsigned', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'build':
        build(args.shim, args.ndk, args.output)
    else:
        stage(args.apk, args.bundle, args.unsigned)
