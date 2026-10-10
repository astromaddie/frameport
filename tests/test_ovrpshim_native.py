"""Execute the production OVR shim against ABI-shaped fake OVRPlugin exports."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CC = shutil.which("cc") or shutil.which("clang")
pytestmark = pytest.mark.skipif(not CC, reason="a host C compiler is required")

HARNESS = r"""
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#define MAX_EVENTS 512
static char events[MAX_EVENTS][512];
static int event_count;
static int updates, waits, stages, begins, ends;
static int update_steps[16], update_frames[16], wait_frames[16];
static int begin_frames[16], end_frames[16];
static double update_predictions[16];

static void event(const char *format, ...) {
    va_list args;
    va_start(args, format);
    vsnprintf(events[event_count++], sizeof(events[0]), format, args);
    va_end(args);
}

int __android_log_print(int priority, const char *tag, const char *format, ...) {
    (void)priority;
    (void)tag;
    va_list args;
    va_start(args, format);
    char line[480];
    vsnprintf(line, sizeof(line), format, args);
    va_end(args);
    event("LOG:%s", line);
    return 0;
}

static int fake_update2(int step, int frame, double prediction) {
    update_steps[updates] = step;
    update_frames[updates] = frame;
    update_predictions[updates] = prediction;
    updates++;
    event("REAL:Update2:%d:%d:%.2f", step, frame, prediction);
    return step == 0 ? 1 : 0;  // ovrpBool returned by the native OVRPlugin wrapper
}

static int fake_wait(int frame) {
    wait_frames[waits++] = frame;
    event("REAL:WaitToBeginFrame:%d", frame);
    return frame == 0 ? 0 : -17;
}

static int fake_get_eye_texture_stage_count(void) {
    stages++;
    event("REAL:GetEyeTextureStageCount");
    return 3;
}

static int fake_begin_frame(int frame) {
    begin_frames[begins++] = frame;
    event("REAL:BeginFrame:%d", frame);
    return 0;  // preserve false unchanged
}

static int fake_end_frame(int frame) {
    end_frames[ends++] = frame;
    event("REAL:EndFrame:%d", frame);
    return 1;
}

void *dlopen(const char *name, int flags) {
    (void)name;
    (void)flags;
    return (void *)1;
}

void *dlsym(void *handle, const char *name) {
    (void)handle;
    if (!strcmp(name, "ovrp_Update2")) return (void *)fake_update2;
    if (!strcmp(name, "ovrp_WaitToBeginFrame")) return (void *)fake_wait;
    if (!strcmp(name, "ovrp_GetEyeTextureStageCount")) return (void *)fake_get_eye_texture_stage_count;
    if (!strcmp(name, "ovrp_BeginFrame")) return (void *)fake_begin_frame;
    if (!strcmp(name, "ovrp_EndFrame")) return (void *)fake_end_frame;
    return NULL;
}

#include "native/ovrpshim/ovrpshim.c"

#define CHECK(condition) do { \
    if (!(condition)) { \
        fprintf(stderr, "FAILED line %d: %s\n", __LINE__, #condition); \
        return 1; \
    } \
} while (0)

static int find_event(const char *needle) {
    for (int i = 0; i < event_count; ++i)
        if (strstr(events[i], needle)) return i;
    return -1;
}

static int trace_count(const char *kind, const char *api) {
    int count = 0;
    char prefix[64];
    snprintf(prefix, sizeof(prefix), "ovrp api trace: %s", kind);
    char api_name[96];
    snprintf(api_name, sizeof(api_name), "api=%s ", api);
    for (int i = 0; i < event_count; ++i)
        count += strstr(events[i], prefix) != NULL && strstr(events[i], api_name) != NULL;
    return count;
}

static int check_real_abi_forwarding_and_order(void) {
    CHECK(fpov_GetEyeTextureStageCount() == 3);
    CHECK(fpov_BeginFrame(41) == 0);
    CHECK(fpov_EndFrame(41) == 1);

    CHECK(fpov_Update2(0, 0, 0.25) == 1);   // update does not pace
    CHECK(fpov_Update2(-1, 0, 0.50) == 0);  // render paces this frame once
    CHECK(fpov_Update2(-1, 0, 0.75) == 0);  // duplicate render step is deduplicated
    CHECK(fpov_Update2(0, 1, 1.00) == 1);
    CHECK(fpov_Update2(-1, 1, 1.25) == 0);

    CHECK(stages == 1 && begins == 1 && ends == 1);
    CHECK(begin_frames[0] == 41 && end_frames[0] == 41);
    CHECK(updates == 5 && waits == 2);
    CHECK(update_steps[0] == 0 && update_frames[0] == 0 && update_predictions[0] == 0.25);
    CHECK(update_steps[1] == -1 && update_frames[1] == 0 && update_predictions[1] == 0.50);
    CHECK(update_steps[2] == -1 && update_frames[2] == 0 && update_predictions[2] == 0.75);
    CHECK(update_steps[3] == 0 && update_frames[3] == 1 && update_predictions[3] == 1.00);
    CHECK(update_steps[4] == -1 && update_frames[4] == 1 && update_predictions[4] == 1.25);
    CHECK(wait_frames[0] == 0 && wait_frames[1] == 1);

    int stage_enter = find_event("ovrp api trace: enter call=1 api_call=1 api=GetEyeTextureStageCount "
                                 "args=none result=pending");
    int stage_real = find_event("REAL:GetEyeTextureStageCount");
    int stage_exit = find_event("ovrp api trace: exit call=1 api_call=1 api=GetEyeTextureStageCount result=3");
    CHECK(stage_enter >= 0 && stage_real > stage_enter && stage_exit > stage_real);

    int begin_enter = find_event("ovrp api trace: enter call=2 api_call=1 api=BeginFrame frame=41 result=pending");
    int begin_real = find_event("REAL:BeginFrame:41");
    int begin_exit = find_event("ovrp api trace: exit call=2 api_call=1 api=BeginFrame result=0");
    CHECK(begin_enter > stage_exit && begin_real > begin_enter && begin_exit > begin_real);

    int end_enter = find_event("ovrp api trace: enter call=3 api_call=1 api=EndFrame frame=41 result=pending");
    int end_real = find_event("REAL:EndFrame:41");
    int end_exit = find_event("ovrp api trace: exit call=3 api_call=1 api=EndFrame result=1");
    CHECK(end_enter > begin_exit && end_real > end_enter && end_exit > end_real);

    int update_enter = find_event("ovrp api trace: enter call=4 api_call=1 api=Update2 step=0 frame=0 "
                                  "prediction=0.250000 render=0 wait_phase=0 last_waited=-1 dedup=0 "
                                  "wait_available=1 wait_called=0 result=pending");
    int update_real = find_event("REAL:Update2:0:0:0.25");
    int update_exit = find_event("ovrp api trace: exit call=4 api_call=1 api=Update2 last_waited=-1 wait_called=0 "
                                 "wait_result=not-called updateresult=1");
    CHECK(update_enter > end_exit && update_real > update_enter && update_exit > update_real);

    int render_enter = find_event("ovrp api trace: enter call=5 api_call=2 api=Update2 step=-1 frame=0 "
                                  "prediction=0.500000 render=1 wait_phase=1 last_waited=-1 dedup=0 "
                                  "wait_available=1 wait_called=1 result=pending");
    int wait = find_event("REAL:WaitToBeginFrame:0");
    int render_real = find_event("REAL:Update2:-1:0:0.50");
    int render_exit = find_event("ovrp api trace: exit call=5 api_call=2 api=Update2 last_waited=0 wait_called=1 "
                                 "wait_result=0 updateresult=0");
    CHECK(render_enter > update_exit && wait > render_enter && render_real > wait && render_exit > render_real);
    CHECK(find_event("ovrp api trace: enter call=6 api_call=3 api=Update2 step=-1 frame=0 prediction=0.750000 "
                     "render=1 wait_phase=1 last_waited=0 dedup=1 wait_available=1 "
                     "wait_called=0 result=pending") > render_exit);
    CHECK(find_event("ovrp api trace: exit call=6 api_call=3 api=Update2 last_waited=0 wait_called=0 "
                     "wait_result=not-called updateresult=0") > render_exit);
    CHECK(find_event("REAL:WaitToBeginFrame:0") == wait);  // no second wait for duplicate render step

    CHECK(find_event("ovrp api trace: exit call=8 api_call=5 api=Update2 last_waited=1 wait_called=1 "
                     "wait_result=-17 updateresult=0") >= 0);

    CHECK(trace_count("enter", "Update2") == 5 && trace_count("exit", "Update2") == 5);
    CHECK(trace_count("enter", "BeginFrame") == 1 && trace_count("exit", "BeginFrame") == 1);
    CHECK(trace_count("enter", "EndFrame") == 1 && trace_count("exit", "EndFrame") == 1);

    // Exactly the Unity-owned Begin/End calls were forwarded; the shim did not add any such calls.
    CHECK(begins == 1 && ends == 1 && waits == 2 && updates == 5);
    return 0;
}

static int check_trace_is_bounded(void) {
    for (int i = 0; i < 16; ++i) CHECK(fpov_GetEyeTextureStageCount() == 3);
    CHECK(trace_count("enter", "GetEyeTextureStageCount") == 16);
    CHECK(trace_count("exit", "GetEyeTextureStageCount") == 16);
    CHECK(find_event("ovrp api trace: enter call=23 api_call=16 api=GetEyeTextureStageCount") >= 0);
    CHECK(find_event("ovrp api trace: exit call=23 api_call=16 api=GetEyeTextureStageCount result=3") >= 0);
    CHECK(find_event("ovrp api trace: enter call=24 api_call=17 api=GetEyeTextureStageCount") < 0);
    CHECK(find_event("ovrp api trace: exit call=24 api_call=17 api=GetEyeTextureStageCount") < 0);
    CHECK(stages == 17);  // all calls reach the production function after diagnostics stop
    return 0;
}

int main(void) {
    CHECK(check_real_abi_forwarding_and_order() == 0);
    CHECK(check_trace_is_bounded() == 0);
    return 0;
}
"""


def test_legacy_ovr_apis_keep_abi_forwarding_and_bounded_trace(tmp_path):
    include = tmp_path / "include/android"
    include.mkdir(parents=True)
    (include / "log.h").write_text(
        "#pragma once\n#define ANDROID_LOG_INFO 4\n"
        "int __android_log_print(int, const char *, const char *, ...);\n"
    )
    source = tmp_path / "ovrpshim_harness.c"
    source.write_text(HARNESS)
    executable = tmp_path / "ovrpshim_harness"
    subprocess.run(
        [CC, "-std=c11", "-Wall", "-Wextra", "-Werror", "-pthread", "-I", str(tmp_path / "include"),
         "-I", str(ROOT), str(source), "-o", str(executable), "-ldl"],
        check=True,
    )
    subprocess.run([str(executable)], check=True)

CONDITIONAL_HARNESS = r"""
#define _GNU_SOURCE
#include <dlfcn.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#define MAX_EVENTS 2048
#define MAX_FRAME_CALLS 64
static char events[MAX_EVENTS][512];
static int event_count;
static int begin_count, end_count, begin_result = 0, end_result = 1;
static int begin_frames[MAX_FRAME_CALLS], end_frames[MAX_FRAME_CALLS];
static pthread_t begin_threads[MAX_FRAME_CALLS], end_threads[MAX_FRAME_CALLS];

static void event(const char *format, ...) {
    va_list args;
    va_start(args, format);
    vsnprintf(events[event_count++], sizeof(events[0]), format, args);
    va_end(args);
}

int __android_log_print(int priority, const char *tag, const char *format, ...) {
    (void)priority;
    (void)tag;
    va_list args;
    va_start(args, format);
    char line[480];
    vsnprintf(line, sizeof(line), format, args);
    va_end(args);
    event("LOG:%s", line);
    return 0;
}

static int fake_begin_frame(int frame) {
    begin_frames[begin_count] = frame;
    begin_threads[begin_count] = pthread_self();
    begin_count++;
    event("REAL:BeginFrame:%d", frame);
    return begin_result;
}

static int fake_end_frame(int frame) {
    end_frames[end_count] = frame;
    end_threads[end_count] = pthread_self();
    end_count++;
    event("REAL:EndFrame:%d", frame);
    return end_result;
}

void *dlopen(const char *name, int flags) {
    (void)name;
    (void)flags;
    return (void *)1;
}

void *dlsym(void *handle, const char *name) {
    (void)handle;
    if (!strcmp(name, "ovrp_BeginFrame")) return (void *)fake_begin_frame;
    if (!strcmp(name, "ovrp_EndFrame")) return (void *)fake_end_frame;
    return NULL;
}

#include "native/ovrpshim/ovrpshim.c"

#define CHECK(condition) do { \
    if (!(condition)) { \
        fprintf(stderr, "FAILED line %d: %s\n", __LINE__, #condition); \
        return 1; \
    } \
} while (0)

static int exact_event_count(const char *needle) {
    int count = 0;
    for (int i = 0; i < event_count; ++i) count += !strcmp(events[i], needle);
    return count;
}

static int trace_count(const char *kind, const char *api) {
    int count = 0;
    char prefix[64], api_name[96];
    snprintf(prefix, sizeof(prefix), "ovrp api trace: %s", kind);
    snprintf(api_name, sizeof(api_name), "api=%s ", api);
    for (int i = 0; i < event_count; ++i)
        count += strstr(events[i], prefix) != NULL && strstr(events[i], api_name) != NULL;
    return count;
}

static void *begin_on_other_thread(void *arg) {
    int frame = *(int *)arg;
    return (void *)(long)fpov_BeginFrame(frame);
}

int main(void) {
    pthread_t main_thread = pthread_self();

    // The established wrappers still forward their raw results and arguments.
    end_result = -23;
    CHECK(fpov_EndFrame(7) == -23);
    begin_result = 0;
    CHECK(fpov_BeginFrame(40) == 0);
    int begin_before_direct_end = begin_count;
    CHECK(fpob_EndFrame(40) == -23);  // matching Unity begin: only real EndFrame follows
    CHECK(begin_count == begin_before_direct_end && end_count == 2 && end_frames[1] == 40);
    CHECK(pthread_equal(begin_threads[0], main_thread) && pthread_equal(end_threads[1], main_thread));

    // Startup frames with no Unity Begin get one real Begin immediately before the original End.
    end_result = 1;
    int begin_before_missing = begin_count;
    int before_missing = event_count;
    CHECK(fpob_EndFrame(41) == 1);
    CHECK(begin_count == begin_before_missing + 1 && end_frames[2] == 41);
    int begin41 = -1, end41 = -1;
    for (int i = before_missing; i < event_count; ++i) {
        if (!strcmp(events[i], "REAL:BeginFrame:41")) begin41 = i;
        if (!strcmp(events[i], "REAL:EndFrame:41")) end41 = i;
    }
    CHECK(begin41 >= 0 && end41 > begin41);
    CHECK(begin_frames[begin_before_missing] == 41);
    CHECK(pthread_equal(begin_threads[begin_before_missing], main_thread));
    CHECK(pthread_equal(end_threads[2], main_thread));

    // A stale or different frame marker cannot suppress the Begin needed by this End.
    CHECK(fpov_BeginFrame(42) == 0);
    int begin_before_mismatch = begin_count;
    CHECK(fpob_EndFrame(43) == 1);
    CHECK(begin_count == begin_before_mismatch + 1 && begin_frames[begin_before_mismatch] == 43);
    CHECK(end_frames[3] == 43);

    // A failed real Begin is preserved in diagnostics but does not skip original End or replace its result.
    begin_result = -17;
    end_result = -29;
    int before_failed = event_count;
    CHECK(fpob_EndFrame(45) == -29);
    CHECK(begin_frames[begin_count - 1] == 45 && end_frames[end_count - 1] == 45);
    int begin45 = -1, end45 = -1, result_log = -1;
    for (int i = before_failed; i < event_count; ++i) {
        if (!strcmp(events[i], "REAL:BeginFrame:45")) begin45 = i;
        if (!strcmp(events[i], "REAL:EndFrame:45")) end45 = i;
        if (strstr(events[i], "begin_called=1 begin_result=-17 end_result=-29")) result_log = i;
    }
    CHECK(begin45 >= 0 && end45 > begin45 && result_log > end45);

    // Begin from another thread is not accepted as the matching same-thread Begin.
    int frame50 = 50;
    pthread_t other_thread;
    CHECK(pthread_create(&other_thread, NULL, begin_on_other_thread, &frame50) == 0);
    void *thread_result = NULL;
    CHECK(pthread_join(other_thread, &thread_result) == 0 && (long)thread_result == -17);
    begin_result = 0;
    int prior50 = exact_event_count("REAL:BeginFrame:50");
    CHECK(fpob_EndFrame(50) == -29);
    CHECK(exact_event_count("REAL:BeginFrame:50") == prior50 + 1);
    CHECK(begin_frames[begin_count - 1] == 50 && end_frames[end_count - 1] == 50);
    CHECK(pthread_equal(begin_threads[begin_count - 1], main_thread));
    CHECK(pthread_equal(end_threads[end_count - 1], main_thread));

    // The new wrapper's entry, decision/real results, and return logs stop after 16 calls while forwarding continues.
    end_result = 1;
    for (int frame = 100; frame < 111; ++frame) CHECK(fpob_EndFrame(frame) == 1);
    CHECK(trace_count("enter", "ConditionalEndFrame") == 16);
    CHECK(trace_count("exit", "ConditionalEndFrame") == 16);
    int logs_before_limit = 0;
    for (int i = 0; i < event_count; ++i)
        logs_before_limit += strstr(events[i], "ovrp conditional end-begin:") != NULL;
    CHECK(logs_before_limit == 16);
    int before_17th = event_count;
    CHECK(fpob_EndFrame(111) == 1);
    for (int i = before_17th; i < event_count; ++i) {
        CHECK(!strstr(events[i], "api=ConditionalEndFrame"));
        CHECK(!strstr(events[i], "ovrp conditional end-begin:"));
    }
    CHECK(end_frames[end_count - 1] == 111 && begin_frames[begin_count - 1] == 111);
    return 0;
}
"""


def test_conditional_begin_is_same_thread_frame_aware_and_bounded(tmp_path):
    include = tmp_path / "include/android"
    include.mkdir(parents=True)
    (include / "log.h").write_text(
        "#pragma once\n#define ANDROID_LOG_INFO 4\n"
        "int __android_log_print(int, const char *, const char *, ...);\n"
    )
    source = tmp_path / "conditional_ovrpshim_harness.c"
    source.write_text(CONDITIONAL_HARNESS)
    executable = tmp_path / "conditional_ovrpshim_harness"
    subprocess.run(
        [CC, "-std=c11", "-Wall", "-Wextra", "-Werror", "-pthread", "-I", str(tmp_path / "include"),
         "-I", str(ROOT), str(source), "-o", str(executable), "-ldl"],
        check=True,
    )
    subprocess.run([str(executable)], check=True)


EARLY_MAIN = r'''
static void *duplicate_update(void *arg) {
    (void)arg;
    fpow_Update2(-1, 2, 0.75);
    return NULL;
}
int main(void) {
    CHECK(fpow_Update2(0, 0, 0.25) == 1);
    CHECK(find_event("REAL:WaitToBeginFrame:0") < find_event("REAL:Update2:0:0:0.25"));
    CHECK(fpow_Update2(-1, 0, 0.50) == 0 && waits == 1);
    // The failed wait on frame 1 is retried by the render phase; neither update result/prediction is invented.
    CHECK(fpow_Update2(0, 1, 1.00) == 1 && waits == 2);
    CHECK(fpow_Update2(-1, 1, 1.25) == 0 && waits == 3);
    CHECK(update_predictions[2] == 1.00 && update_predictions[3] == 1.25);
    CHECK(fpow_Update2(0, 2, 0.50) == 1 && waits == 4);
    // A second caller for the already-waited frame must not issue another runtime wait.
    pthread_t a, b;
    CHECK(pthread_create(&a, NULL, duplicate_update, NULL) == 0);
    CHECK(pthread_create(&b, NULL, duplicate_update, NULL) == 0);
    CHECK(pthread_join(a, NULL) == 0 && pthread_join(b, NULL) == 0);
    CHECK(waits == 4 && updates == 7);
    // A late update for an older frame cannot roll the runtime wait backwards.
    CHECK(fpow_Update2(-1, 0, 0.75) == 0 && waits == 4);
    // Render-first callers still work, and their later simulation update is deduplicated.
    CHECK(fpow_Update2(-1, 3, 0.75) == 0 && waits == 5);
    CHECK(fpow_Update2(0, 3, 0.75) == 1 && waits == 5);
    CHECK(begins == 0 && ends == 0);
    CHECK(trace_count("enter", "Update2") == 10);
    // Simulate Wait(N+1) depending on the render thread's Begin(N). That API must not share the update lock.
    CHECK(fpow_Update2(0, 4, 0.75) == 1 && waits == 6 && begins == 1);
    CHECK(begin_frames[0] == 3);
    return 0;
}
'''


def test_early_wait_precedes_physics_and_deduplicates_across_threads(tmp_path):
    source_text = HARNESS[:HARNESS.index("static int check_real_abi_forwarding_and_order")]
    source_text = source_text.replace("events[event_count++]",
                                      "events[__atomic_fetch_add(&event_count, 1, __ATOMIC_RELAXED)]")
    source_text = source_text.replace("update_steps[updates]", "update_steps[update_index]").replace(
        "update_frames[updates]", "update_frames[update_index]").replace(
        "update_predictions[updates]", "update_predictions[update_index]")
    source_text = source_text.replace("    updates++;", "")
    source_text = source_text.replace(
        "    update_steps[update_index]",
        "    int update_index = __atomic_fetch_add(&updates, 1, __ATOMIC_RELAXED);\n    update_steps[update_index]")
    source_text = source_text.replace("static int fake_wait(int frame) {", r"""
#include <pthread.h>
int fpov_BeginFrame(int frame);
int fpow_Update2(int step, int frame, double prediction);
static void *render_previous(void *arg) {
    (void)arg; fpow_Update2(-1, 3, 0.75); fpov_BeginFrame(3); return NULL;
}
static int fake_wait(int frame) {
    if (frame == 4) {
        pthread_t render;
        if (pthread_create(&render, NULL, render_previous, NULL) || pthread_join(render, NULL)) return -17;
    }
""")
    source_text = source_text.replace("return frame == 0 ? 0 : -17;", "return frame == 1 ? -17 : 0;")
    source = tmp_path / "early.c"
    source.write_text(source_text + EARLY_MAIN)
    executable = tmp_path / "early"
    subprocess.run([CC, "-std=c11", "-Wall", "-Wextra", "-Werror", "-pthread",
                    "-I", str(ROOT / "tests/fixtures/include"), "-I", str(ROOT), str(source),
                    "-o", str(executable), "-ldl"], check=True)
    subprocess.run([str(executable)], check=True)


POSE_BRIDGE_PRELUDE = r"""
#include <stdint.h>
#include <pthread.h>
#include <stdlib.h>
typedef uint64_t XrAction;
typedef uint64_t XrSpace;
typedef int64_t XrTime;
static int legacy_pose_time;
static __thread int64_t mono = 1000;
static int64_t monotonic_ns(void) { return ++mono; }
#define TAG "FrameBridge"
#define ANDROID_LOG_INFO 4
#define LOG(...) __android_log_print(ANDROID_LOG_INFO, TAG, __VA_ARGS__)
#include "native/adapter/legacy_pose_time.c"
static int64_t view_times[8], controller_times[8];
int fpow_Update2(int, int, double);
static void *next_frame(void *unused) {
    (void)unused;
    if (legacy_query_time(100000000, 0) != 100000000) abort();
    fpow_Update2(0, 2, 0);
    if (legacy_scope.predicted) abort();
    return NULL;
}
"""

POSE_BRIDGE_MAIN = r"""
int main(void) {
    // Registry follows actual binding + space registration with diagnostics off.
    legacy_note_binding(1, "/user/hand/left/input/aim/pose");
    legacy_note_binding(2, "/user/eyes_ext/input/aim/pose");
    legacy_note_space(1, 101); legacy_note_space(2, 102); legacy_note_space(3, 103);
    CHECK(is_controller_space(101) && !is_controller_space(102) && !is_controller_space(103));
    // Default path and a disabled adapter are exact pass-throughs.
    fpov_Update2(0, 0, 0);
    CHECK(view_times[0] == 100000000 && controller_times[0] < 2000);
    FrameportPoseScope saved;
    framebridge_pose_push_v1(123456789, &saved);
    CHECK(legacy_query_time(100000000, 0) == 100000000);
    framebridge_pose_pop_v1(&saved);
    legacy_pose_time = 1;
    // Frame 2 completes on another thread while frame 1 is in its original update.
    fpow_Update2(0, 1, 0);
    CHECK(view_times[1] == 1001000000000ll && controller_times[1] == view_times[1]);
    CHECK(view_times[2] == 1002000000000ll && controller_times[2] == view_times[2]);
    CHECK(!legacy_scope.predicted && legacy_query_time(100000000, 0) == 100000000);
    fpow_Update2(0, 3, 0); // failed wait cannot use frame 2's time
    fpow_Update2(0, 4, 0); // success without an actual xrWaitFrame cannot reuse a time
    CHECK(view_times[3] == 100000000 && view_times[4] == 100000000);
    CHECK(find_event("legacy_pose_time: source=views-physics-sentinel") >= 0);
    CHECK(find_event("legacy_pose_time: source=controller-dispatcher-mono") >= 0);
    CHECK(trace_count("enter", "Update2") == 5);
    forget_pose_space(101); CHECK(!is_controller_space(101));
    return 0;
}
"""


def test_production_pose_bridge_preserves_frame_ownership_and_default(tmp_path):
    source_text = HARNESS[:HARNESS.index("static int check_real_abi_forwarding_and_order")]
    source_text = source_text.replace("static int fake_update2", POSE_BRIDGE_PRELUDE + "\nstatic int fake_update2")
    source_text = source_text.replace('    update_steps[updates] = step;', r'''
    if (step == 0 && frame == 1) {
        pthread_t next;
        if (pthread_create(&next, NULL, next_frame, NULL) || pthread_join(next, NULL)) abort();
    }
    view_times[frame] = legacy_query_time(100000000, 0);
    mono += 10;
    controller_times[frame] = legacy_space_time(101, mono);
    if (legacy_space_time(102, mono) != mono || legacy_space_time(101, 42) != 42 ||
        legacy_query_time(999999999999ll, 0) != 999999999999ll ||
        legacy_space_time(101, 999999999999ll) != 999999999999ll) abort();
    update_steps[updates] = step;''')
    source_text = source_text.replace('    wait_frames[waits++] = frame;',
                                      '    if (frame != 4) legacy_wait_time = 1000000000000ll + frame * 1000000000ll;\n'
                                      '    wait_frames[waits++] = frame;')
    source_text = source_text.replace('return frame == 0 ? 0 : -17;', 'return frame == 3 ? -17 : 0;')
    source_text = source_text.replace('    (void)handle;', r'''
    (void)handle;
    if (!strcmp(name, "framebridge_pose_wait_reset_v1")) return framebridge_pose_wait_reset_v1;
    if (!strcmp(name, "framebridge_pose_wait_time_v1")) return framebridge_pose_wait_time_v1;
    if (!strcmp(name, "framebridge_pose_push_v1")) return framebridge_pose_push_v1;
    if (!strcmp(name, "framebridge_pose_pop_v1")) return framebridge_pose_pop_v1;''')
    include = tmp_path / "include/android"
    include.mkdir(parents=True)
    (include / "log.h").write_text("#pragma once\n#define ANDROID_LOG_INFO 4\n"
                                 "int __android_log_print(int, const char *, const char *, ...);\n")
    source = tmp_path / "pose.c"
    source.write_text(source_text + POSE_BRIDGE_MAIN)
    executable = tmp_path / "pose"
    subprocess.run([CC, "-std=c11", "-Wall", "-Wextra", "-Werror", "-pthread", "-I", str(tmp_path / "include"),
                    "-I", str(ROOT), str(source), "-o", str(executable), "-ldl"], check=True)
    subprocess.run([str(executable)], check=True, timeout=10)
