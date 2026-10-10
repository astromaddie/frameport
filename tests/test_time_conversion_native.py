"""Exercise the adapter's actual time-conversion functions with a controlled runtime."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INC = next(iter((ROOT / "native/.cache").glob("openxr-*")), None)
CC = shutil.which("cc")
pytestmark = [pytest.mark.native, pytest.mark.skipif(
    not os.environ.get("FRAMEPORT_NATIVE_TESTS") or not INC or not CC,
    reason="needs FRAMEPORT_NATIVE_TESTS=1, cached OpenXR headers and host cc")]

HARNESS = r'''
#define XR_USE_TIMESPEC
#include <time.h>
#include <openxr/openxr.h>
#include <openxr/openxr_platform.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stdarg.h>
static int layer_debug, xr_time_calibrated;
static int64_t xr_time_offset;
static char logs[4096];
static void record(const char *fmt, ...) {
    va_list args; va_start(args, fmt);
    vsnprintf(logs + strlen(logs), sizeof(logs) - strlen(logs), fmt, args);
    va_end(args);
}
#define LOG(...) record(__VA_ARGS__)
#include "time_conversion.c"
#define CHECK(x) do { if (!(x)) { fprintf(stderr,"line %d: %s\n", __LINE__, #x); return 1; } } while(0)
static XrResult reply;
static XrResult to_xr(XrInstance i, const struct timespec *ts, XrTime *out) {
    (void)i; (void)ts; if (reply == XR_SUCCESS) *out = 123; return reply;
}
static XrResult to_mono(XrInstance i, XrTime t, struct timespec *out) {
    (void)i; (void)t; if (reply == XR_SUCCESS) *out = (struct timespec){42, 99}; return reply;
}
int main(void) {
    struct timespec ts = {8, 750000000}, back = {0, 0}; XrTime t = 0;
    CHECK(emu_timespec_to_time(0, &ts, &t) == XR_SUCCESS && t == 8750000000ll);
    CHECK(logs[0] == 0); // diagnostics off by default
    xr_time_calibrated = 1; xr_time_offset = 2000000000ll;
    layer_debug = 1;
    CHECK(emu_timespec_to_time(0, &ts, &t) == XR_SUCCESS && t == 10750000000ll);
    CHECK(emu_time_to_timespec(0, t, &back) == XR_SUCCESS && back.tv_sec == 8 && back.tv_nsec == 750000000);
    CHECK(strstr(logs, "wait-frame-estimate"));
    runtime_timespec_to_time = to_xr; runtime_time_to_timespec = to_mono;
    reply = XR_SUCCESS;
    CHECK(emu_timespec_to_time(0, &ts, &t) == XR_SUCCESS && t == 123);
    CHECK(emu_time_to_timespec(0, t, &back) == XR_SUCCESS && back.tv_sec == 42 && back.tv_nsec == 99);
    CHECK(strstr(logs, "route=runtime"));
    reply = XR_ERROR_TIME_INVALID; t = 678;
    CHECK(emu_timespec_to_time(0, &ts, &t) == XR_ERROR_TIME_INVALID && t == 678);
    CHECK(emu_time_to_timespec(0, t, &back) == XR_ERROR_TIME_INVALID && back.tv_sec == 42);
    reply = XR_ERROR_FUNCTION_UNSUPPORTED;
    CHECK(emu_timespec_to_time(0, &ts, &t) == XR_SUCCESS && t == 10750000000ll);
    CHECK(emu_time_to_timespec(0, t, &back) == XR_SUCCESS && back.tv_sec == 8);
    CHECK(emu_timespec_to_time(0, NULL, &t) == XR_ERROR_VALIDATION_FAILURE);
    CHECK(emu_time_to_timespec(0, t, NULL) == XR_ERROR_VALIDATION_FAILURE);
    puts("ok"); return 0;
}
'''


def test_production_conversion_passthrough_and_fallback(tmp_path):
    source = tmp_path / "harness.c"
    source.write_text(HARNESS)
    exe = tmp_path / "harness"
    subprocess.run([CC, "-std=c11", "-Wall", "-Wextra", "-Werror", "-I", str(INC),
                    "-I", str(ROOT / "native/adapter"), str(source), "-o", str(exe)], check=True)
    result = subprocess.run([str(exe)], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
