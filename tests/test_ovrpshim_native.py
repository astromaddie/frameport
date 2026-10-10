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

    int stage_enter = find_event("ovrp api trace: enter call=1 api_call=1 api=GetEyeTextureStageCount args=none result=pending");
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

    int update_enter = find_event("ovrp api trace: enter call=4 api_call=1 api=Update2 step=0 frame=0 prediction=0.250000 "
                                   "render=0 wait_phase=0 last_waited=-1 dedup=0 wait_available=1 wait_called=0 result=pending");
    int update_real = find_event("REAL:Update2:0:0:0.25");
    int update_exit = find_event("ovrp api trace: exit call=4 api_call=1 api=Update2 last_waited=-1 wait_called=0 "
                                 "wait_result=not-called updateresult=1");
    CHECK(update_enter > end_exit && update_real > update_enter && update_exit > update_real);

    int render_enter = find_event("ovrp api trace: enter call=5 api_call=2 api=Update2 step=-1 frame=0 prediction=0.500000 "
                                  "render=1 wait_phase=1 last_waited=-1 dedup=0 wait_available=1 wait_called=1 result=pending");
    int wait = find_event("REAL:WaitToBeginFrame:0");
    int render_real = find_event("REAL:Update2:-1:0:0.50");
    int render_exit = find_event("ovrp api trace: exit call=5 api_call=2 api=Update2 last_waited=0 wait_called=1 "
                                 "wait_result=0 updateresult=0");
    CHECK(render_enter > update_exit && wait > render_enter && render_real > wait && render_exit > render_real);
    CHECK(find_event("ovrp api trace: enter call=6 api_call=3 api=Update2 step=-1 frame=0 prediction=0.750000 "
                     "render=1 wait_phase=1 last_waited=0 dedup=1 wait_available=1 wait_called=0 result=pending") > render_exit);
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
