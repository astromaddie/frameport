"""Run the actual companion wrappers with ABI-shaped installed-shim stand-ins."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_installed_shim_companion(tmp_path):
    cc = shutil.which("cc")
    if not cc:
        pytest.skip("host C compiler required")
    (tmp_path / "android").mkdir()
    (tmp_path / "android/log.h").write_text("#define ANDROID_LOG_INFO 4\nint __android_log_print(int,const char*,const char*,...);\n")
    source = r'''
#include <assert.h>
#include <stdint.h>
#include <string.h>
#include <pthread.h>
static __thread int begin_count, end_count;
static __thread uint64_t args[4];
static int begin_result=-17, end_result=-29;
static int begin(uint64_t f,uint64_t a,uint64_t b,uint64_t c) {
    ++begin_count; args[0]=f;args[1]=a;args[2]=b;args[3]=c;return begin_result;
}
static int end(uint64_t f,uint64_t a,uint64_t b,uint64_t c) {
    ++end_count;assert(args[0]==f && args[1]==a && args[2]==b && args[3]==c);return end_result;
}
void *dlopen(const char *name,int flags) {(void)flags;assert(!strcmp(name,"libfp_base.so"));return (void*)1;}
void *dlsym(void *h,const char *name) {(void)h;return !strcmp(name,"fpov_BeginFrame")?(void*)begin:(void*)end;}
int __android_log_print(int p,const char *t,const char *f,...) {(void)p;(void)t;(void)f;return 0;}
#include "native/legacy_begin_proxy/legacy_begin_proxy.c"
static void *other(void *unused) {
    (void)unused;
    assert(fpov_EndFrame(50,1,2,3)==-29);
    assert(begin_count==1 && end_count==1); // main thread's matching Begin is irrelevant
    return 0;
}
int main(void) {
    assert(fpov_BeginFrame(40,1,2,3)==-17);
    assert(fpov_EndFrame(40,1,2,3)==-29);
    assert(begin_count==1 && end_count==1); // failed Begin still counts as a forwarded call
    assert(fpov_EndFrame(41,4,5,6)==-29);
    assert(begin_count==2 && end_count==2); // missing Begin inserted before End
    assert(fpov_BeginFrame(42,7,8,9)==-17);
    assert(fpov_EndFrame(43,7,8,9)==-29);
    assert(begin_count==4); // mismatched frame doesn't satisfy End
    assert(fpov_EndFrame(43,7,8,9)==-29 && begin_count==5); // consumed once
    assert(fpov_BeginFrame(50,1,2,3)==-17);
    pthread_t thread;assert(!pthread_create(&thread,0,other,0));assert(!pthread_join(thread,0));
    assert(fpov_EndFrame(50,1,2,3)==-29 && begin_count==6);
    assert(fpov_EndFrame(UINT64_C(0xabcdef0000000033),UINT64_MAX,0x456789,0x765432)==-29);
    assert(args[0]==UINT64_C(0xabcdef0000000033) && args[1]==UINT64_MAX);
    begin_result=1; end_result=0;
    int before=begin_count;
    assert(fpov_BeginFrame(59,1,2,3)==1);
    assert(fpov_EndFrame(59,1,2,3)==0 && begin_count==before+1);
    installed_begin=0; // no imaginary matching call recorded when export unavailable
    assert(fpov_BeginFrame(60,1,2,3)==-1000 && !pending);
    installed_end=0;assert(fpov_EndFrame(60,1,2,3)==-1000);
    return 0;
}
'''
    harness = tmp_path / "harness.c"
    harness.write_text(source)
    exe = tmp_path / "harness"
    subprocess.run([cc, "-std=c11", "-Wall", "-Wextra", "-Werror", "-pthread", "-I", str(tmp_path),
                    "-I", str(ROOT), str(harness), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)


def test_unknown_installed_shim_refused():
    import importlib.util
    path = ROOT / "scripts/prepare_ieytd_begin_trial.py"
    spec = importlib.util.spec_from_file_location("prepare_trial", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(ValueError, match="unknown installed shim"):
        module.renamed_base(b"unrecognized libfp_ovrp.so\0")


def test_stage_preserves_unrelated_entries_and_original(tmp_path, monkeypatch):
    import hashlib
    import importlib.util
    import json
    import zipfile
    spec = importlib.util.spec_from_file_location("prepare_trial", ROOT / "scripts/prepare_ieytd_begin_trial.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    old = b"fixture-prefix\0libfp_ovrp.so\0fixture-suffix"
    monkeypatch.setattr(module, "SHIM_SHA", hashlib.sha256(old).hexdigest())
    (bundle / "libfp_base.so").write_bytes(module.renamed_base(old))
    (bundle / "libfp_ovrp.so").write_bytes(b"fixture-proxy")
    shutil.copy2(ROOT / "src/frameport/apk/workspace.py", bundle / "workspace.py")
    apk = tmp_path / "installed.apk"
    with zipfile.ZipFile(apk, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr(module.LIB + "libfp_ovrp.so", old)
        z.writestr(module.LIB + "libunity.so", b"Unity unchanged")
        z.writestr("assets/game.data", b"user game assets" * 1000)
        z.writestr("META-INF/ORIGINAL.RSA", b"stale signature")
    original = apk.read_bytes()
    monkeypatch.setattr(module, "APK_SHA", hashlib.sha256(original).hexdigest())
    (bundle / "manifest.json").write_text(json.dumps({"original_apk_sha256":module.APK_SHA,
        "files":{f.name:{"sha256":module.digest(f)} for f in bundle.iterdir()}}))
    out = tmp_path / "unsigned.apk"
    module.stage(apk, bundle, out)
    assert apk.read_bytes() == original
    with zipfile.ZipFile(out) as z:
        assert z.read(module.LIB + "libunity.so") == b"Unity unchanged"
        assert z.read("assets/game.data") == b"user game assets" * 1000
        assert z.read(module.LIB + "libfp_base.so") == module.renamed_base(old)
        assert "META-INF/ORIGINAL.RSA" not in z.namelist()
    with pytest.raises(AssertionError):
        module.stage(apk, bundle, out)  # never overwrite a prior candidate
