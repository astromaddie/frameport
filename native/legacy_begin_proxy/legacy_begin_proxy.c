// SPDX-License-Identifier: GPL-3.0-only
// IEYTD diagnostic companion for the exact installed FramePort shim.
// Keep the installed shim as libfp_base.so (SONAME-only rename); this facade
// owns just Begin/End. Other symbols resolve through its DT_NEEDED dependency.
#include <android/log.h>
#include <dlfcn.h>
#include <pthread.h>
#include <stdint.h>
#define EXPORT __attribute__((visibility("default")))
typedef int (*FrameCall)(uint64_t, uint64_t, uint64_t, uint64_t);
static FrameCall installed_begin, installed_end;
static pthread_once_t once = PTHREAD_ONCE_INIT;
static __thread int pending;
static __thread uint32_t begun_frame;
static unsigned injected;

static void resolve(void) {
    void *base = dlopen("libfp_base.so", RTLD_NOW | RTLD_NOLOAD);
    if (base) {
        installed_begin = (FrameCall)dlsym(base, "fpov_BeginFrame");
        installed_end = (FrameCall)dlsym(base, "fpov_EndFrame");
    }
}

// The installed ARM64 wrappers preserve x0..x3, including opaque trailing
// arguments. Keep those registers and the original result exactly intact.
EXPORT int fpov_BeginFrame(uint64_t frame, uint64_t a, uint64_t b, uint64_t c) {
    pthread_once(&once, resolve);
    int result = installed_begin ? installed_begin(frame, a, b, c) : -1000;
    if (installed_begin) { begun_frame = (uint32_t)frame; pending = 1; }
    return result;
}

EXPORT int fpov_EndFrame(uint64_t frame, uint64_t a, uint64_t b, uint64_t c) {
    pthread_once(&once, resolve);
    int matched = pending && begun_frame == (uint32_t)frame;
    pending = 0; // a stale Begin cannot satisfy a later End, even after failure
    int begin_result = -1000;
    if (!matched && installed_begin) begin_result = installed_begin(frame, a, b, c);
    int end_result = installed_end ? installed_end(frame, a, b, c) : -1000;
    if (!matched && __atomic_fetch_add(&injected, 1, __ATOMIC_RELAXED) < 16)
        __android_log_print(ANDROID_LOG_INFO, "FrameBridge",
            "ieytd conditional begin: frame=%u thread=%llu begin_result=%d end_result=%d",
            (uint32_t)frame, (unsigned long long)(uintptr_t)pthread_self(), begin_result, end_result);
    return end_result;
}
