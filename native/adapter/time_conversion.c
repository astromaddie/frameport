// Included by frame_adapter.c. No timing policy changes: diagnostics identify the actual conversion route.
static void debug_time_route(int inverse, XrResult result) {
    static unsigned int calls[2];
    if (layer_debug && __atomic_fetch_add(&calls[inverse], 1, __ATOMIC_RELAXED) < 4)
        LOG("pose_time: conversion direction=%s runtime_result=%d route=%s",
            inverse ? "xr_to_mono" : "mono_to_xr", result,
            result == XR_ERROR_FUNCTION_UNSUPPORTED ? "wait-frame-estimate" : "runtime");
}

// XR_KHR_convert_timespec_time for runtimes that lack it (Frame): XrTime is derived from the monotonic clock
// using the offset measured at the last xrWaitFrame. The runtime's own implementation is preferred.
static PFN_xrConvertTimespecTimeToTimeKHR runtime_timespec_to_time;
static PFN_xrConvertTimeToTimespecTimeKHR runtime_time_to_timespec;

static XRAPI_ATTR XrResult XRAPI_CALL emu_timespec_to_time(XrInstance instance, const struct timespec *ts, XrTime *time) {
    if (runtime_timespec_to_time) {
        XrResult r = runtime_timespec_to_time(instance, ts, time);
        debug_time_route(0, r);
        if (r != XR_ERROR_FUNCTION_UNSUPPORTED) return r;
    }
    if (!runtime_timespec_to_time) debug_time_route(0, XR_ERROR_FUNCTION_UNSUPPORTED);
    if (!ts || !time) return XR_ERROR_VALIDATION_FAILURE;
    *time = (XrTime)((int64_t)ts->tv_sec * 1000000000ll + ts->tv_nsec + (xr_time_calibrated ? xr_time_offset : 0));
    return XR_SUCCESS;
}
static XRAPI_ATTR XrResult XRAPI_CALL emu_time_to_timespec(XrInstance instance, XrTime time, struct timespec *ts) {
    if (runtime_time_to_timespec) {
        XrResult r = runtime_time_to_timespec(instance, time, ts);
        debug_time_route(1, r);
        if (r != XR_ERROR_FUNCTION_UNSUPPORTED) return r;
    }
    if (!runtime_time_to_timespec) debug_time_route(1, XR_ERROR_FUNCTION_UNSUPPORTED);
    if (!ts) return XR_ERROR_VALIDATION_FAILURE;
    int64_t mono = (int64_t)time - (xr_time_calibrated ? xr_time_offset : 0);
    ts->tv_sec = (time_t)(mono / 1000000000ll);
    ts->tv_nsec = (long)(mono % 1000000000ll);
    return XR_SUCCESS;
}
