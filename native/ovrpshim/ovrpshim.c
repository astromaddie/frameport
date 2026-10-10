// SPDX-License-Identifier: GPL-3.0-only
// FramePort OVRPlugin shim: the frame loop for Unity 2017-2018 built-in Oculus support, and Unity's user presence.
//
// That Unity drives OVRPlugin with the legacy frame loop: ovrp_Update2(render step, frame index, ...) on the main
// thread, then ovrp_BeginFrame / ovrp_EndFrame on the render thread. It never calls ovrp_WaitToBeginFrame, which
// newer OVRPlugin builds (OVRPort's OpenXR OVRPlugin) need to call xrWaitFrame: no frame ever begins, OVRPlugin logs
// "CompositorOpenXR::Update called for frame N outside of frame bounds" thousands of times a second and the Frame's
// dashboard freezes (Accounting+, Unity 2017.4).
//
// FramePort renames selected OVRPlugin lookup names in libunity.so to their same-length fpov_ wrappers and adds this
// library to libOVRPlugin.so's DT_NEEDED, so Unity's dlsym on the plugin finds the wrappers. Update2 paces the legacy
// render step once per frame index; the remaining wrappers are exact pass-throughs used to trace which frame APIs
// Unity calls and what OVRPlugin returns.
#include <android/log.h>
#include <dlfcn.h>
#include <pthread.h>
#include <stdint.h>
#include "../legacy_pose_time.h"
#include <stdio.h>

#define TAG "FrameBridge"
#define EXPORT __attribute__((visibility("default")))
#define LOG(...) __android_log_print(ANDROID_LOG_INFO, TAG, __VA_ARGS__)

typedef int (*PFN_Update2)(int step, int frame_index, double prediction_seconds);
typedef int (*PFN_WaitToBeginFrame)(int frame_index);
typedef int (*PFN_GetEyeTextureStageCount)(void);
typedef int (*PFN_BeginFrame)(int frame_index);
typedef int (*PFN_EndFrame)(int frame_index);

static PFN_Update2 real_update2;
static PFN_WaitToBeginFrame real_wait;
static PFN_GetEyeTextureStageCount real_get_eye_texture_stage_count;
static PFN_BeginFrame real_begin_frame;
static PFN_EndFrame real_end_frame;
static pthread_once_t once = PTHREAD_ONCE_INIT;
static unsigned int trace_calls;
enum {
    TRACE_UPDATE2,
    TRACE_STAGE_COUNT,
    TRACE_BEGIN_FRAME,
    TRACE_END_FRAME,
    TRACE_CONDITIONAL_END_FRAME,
    TRACE_API_COUNT
};
static unsigned int traced_api_calls[TRACE_API_COUNT];

// The experimental legacy-frame patch only injects BeginFrame when Unity has not forwarded a matching frame on this
// render thread since the preceding EndFrame. Unity's legacy render calls are serialized on that thread.
static __thread int unity_begin_pending;
static __thread int unity_begin_frame;
static __thread int unity_begin_result;

typedef struct {
    unsigned int call;
    unsigned int api_call;
    int enabled;
} ApiTrace;

static void init(void) {
    void *ovrp = dlopen("libOVRPlugin.so", RTLD_NOW | RTLD_NOLOAD);
    if (!ovrp) ovrp = dlopen("libOVRPlugin.so", RTLD_NOW);
    if (ovrp) {
        real_update2 = (PFN_Update2)dlsym(ovrp, "ovrp_Update2");
        real_wait = (PFN_WaitToBeginFrame)dlsym(ovrp, "ovrp_WaitToBeginFrame");
        real_get_eye_texture_stage_count =
            (PFN_GetEyeTextureStageCount)dlsym(ovrp, "ovrp_GetEyeTextureStageCount");
        real_begin_frame = (PFN_BeginFrame)dlsym(ovrp, "ovrp_BeginFrame");
        real_end_frame = (PFN_EndFrame)dlsym(ovrp, "ovrp_EndFrame");
    }
    LOG("ovrp trace shim: Update2 %s, WaitToBeginFrame %s, GetEyeTextureStageCount %s, BeginFrame %s, EndFrame %s",
        real_update2 ? "OK" : "MISSING", real_wait ? "OK" : "MISSING",
        real_get_eye_texture_stage_count ? "OK" : "MISSING", real_begin_frame ? "OK" : "MISSING",
        real_end_frame ? "OK" : "MISSING");
}

#define STEP_RENDER (-1)  // ovrpStep_Render

static ApiTrace trace_enter(int api_id, const char *api, const char *arguments) {
    ApiTrace trace;
    trace.call = __atomic_fetch_add(&trace_calls, 1, __ATOMIC_RELAXED) + 1;
    trace.api_call = __atomic_fetch_add(&traced_api_calls[api_id], 1, __ATOMIC_RELAXED) + 1;
    trace.enabled = trace.api_call <= 16;
    if (trace.enabled)
        LOG("ovrp api trace: enter call=%u api_call=%u api=%s %s result=pending", trace.call, trace.api_call,
            api, arguments);
    return trace;
}

static void trace_exit(ApiTrace trace, const char *api, int result) {
    if (trace.enabled)
        LOG("ovrp api trace: exit call=%u api_call=%u api=%s result=%d", trace.call, trace.api_call, api, result);
}

static void mouse_click_frame(void);  // below: controller presses as Unity mouse clicks

static FrameportPoseWaitReset pose_wait_reset;
static FrameportPoseWaitTime pose_wait_time;
static FrameportPosePush pose_push;
static FrameportPosePop pose_pop;
static pthread_once_t pose_once = PTHREAD_ONCE_INIT;
static void pose_init(void) {
    void *adapter = dlopen("libopenxr_loader_generic.so", RTLD_NOW | RTLD_NOLOAD);
    if (adapter) {
        pose_wait_reset = (FrameportPoseWaitReset)dlsym(adapter, "framebridge_pose_wait_reset_v1");
        pose_wait_time = (FrameportPoseWaitTime)dlsym(adapter, "framebridge_pose_wait_time_v1");
        pose_push = (FrameportPosePush)dlsym(adapter, "framebridge_pose_push_v1");
        pose_pop = (FrameportPosePop)dlsym(adapter, "framebridge_pose_pop_v1");
    }
    if (!pose_wait_reset || !pose_wait_time || !pose_push || !pose_pop) {
        pose_wait_reset = NULL; pose_wait_time = NULL; pose_push = NULL; pose_pop = NULL;
        LOG("legacy_pose_time: optional adapter ABI unavailable; scoped pose correction disabled");
    }
}

static int update2(int step, int frame_index, double prediction_seconds, int early_wait) {
    pthread_once(&once, init);
    // Reserve an early wait across callers, but never hold the mutex in a runtime call: Wait(N+1) may
    // depend on another thread's Update(render,N) -> Begin(N). Older render updates must remain runnable.
    static pthread_mutex_t early_lock = PTHREAD_MUTEX_INITIALIZER;
    static pthread_cond_t early_done = PTHREAD_COND_INITIALIZER;
    static int early_waiting, early_pending = -1, early_last_waited = -1;
    static int64_t early_last_time;
    int64_t owned_time = 0;
    if (early_wait) pthread_once(&pose_once, pose_init);
    static int legacy_last_waited = -1, logged_by_mode[2];
    int render_step = step == STEP_RENDER;
    int wait_phase = render_step || (early_wait && step == 0);
    int last_waited, pending_newer = 0;
    if (early_wait) {
        pthread_mutex_lock(&early_lock);
        while (wait_phase && early_waiting && frame_index >= early_pending)
            pthread_cond_wait(&early_done, &early_lock);
        last_waited = early_last_waited;
        if (frame_index == early_last_waited) owned_time = early_last_time;
        pending_newer = early_waiting && frame_index < early_pending;
    } else last_waited = legacy_last_waited;
    int last_waited_before = last_waited;
    int dedup = wait_phase && (early_wait ? frame_index <= last_waited || pending_newer : frame_index == last_waited);
    int wait_available = real_wait != NULL;
    int wait_called = wait_phase && wait_available && !dedup;
    if (early_wait) {
        if (wait_called) { early_pending = frame_index; early_waiting = 1; }
        pthread_mutex_unlock(&early_lock);
    }
    char arguments[192];
    snprintf(arguments, sizeof(arguments), "step=%d frame=%d prediction=%.6f render=%d wait_phase=%d last_waited=%d "
             "dedup=%d wait_available=%d wait_called=%d", step, frame_index, prediction_seconds, render_step,
             wait_phase, last_waited_before, dedup, wait_available, wait_called);
    ApiTrace trace = trace_enter(TRACE_UPDATE2, "Update2", arguments);
    int wait_result = 0;
    if (wait_called) {
        if (!early_wait) legacy_last_waited = frame_index;  // preserve the original wrapper's reentry behavior
        if (early_wait && pose_wait_reset) pose_wait_reset();
        wait_result = real_wait(frame_index);
        if (early_wait && pose_wait_time && wait_result >= 0) owned_time = pose_wait_time();
        if (!early_wait || wait_result >= 0) last_waited = frame_index;
        if (early_wait) {
            pthread_mutex_lock(&early_lock);
            early_last_waited = last_waited;
            if (wait_result >= 0) early_last_time = owned_time;
            early_waiting = 0;
            pthread_cond_broadcast(&early_done);
            pthread_mutex_unlock(&early_lock);
        }
        int logged = __atomic_fetch_add(&logged_by_mode[early_wait], 1, __ATOMIC_RELAXED);
        if (logged < 3) LOG("ovrp frame loop shim: waited for frame %d: %d", frame_index, wait_result);
        if (early_wait && logged < 3)
            LOG("ovrp early wait: step=%d frame=%d result=%d", step, frame_index, wait_result);
        mouse_click_frame();
    }
    FrameportPoseScope saved_scope;
    int scoped = early_wait && step == 0 && owned_time > 0 && pose_push;
    if (scoped) pose_push(owned_time, &saved_scope);
    int update_result = real_update2 ? real_update2(step, frame_index, prediction_seconds) : -1000;  // ovrpFailure
    if (scoped) pose_pop(&saved_scope);
    if (trace.enabled) {
        if (wait_called)
            LOG("ovrp api trace: exit call=%u api_call=%u api=Update2 last_waited=%d wait_called=1 wait_result=%d updateresult=%d",
                trace.call, trace.api_call, last_waited, wait_result, update_result);
        else
            LOG("ovrp api trace: exit call=%u api_call=%u api=Update2 last_waited=%d wait_called=0 wait_result=not-called updateresult=%d",
                trace.call, trace.api_call, last_waited_before, update_result);
    }
    return update_result;
}

EXPORT int fpov_Update2(int step, int frame_index, double prediction_seconds) {
    return update2(step, frame_index, prediction_seconds, 0);
}

// Manual frame.unity_early_frame_wait: Unity's simulation Update2(0,N) implicitly updates render poses too in
// replacement OVRPlugin. Waiting only at Update2(-1,N) leaves both earlier updates outside frame N's bounds.
EXPORT int fpow_PoseTimeVersion(void) { return 1; }

EXPORT int fpow_Update2(int step, int frame_index, double prediction_seconds) {
    return update2(step, frame_index, prediction_seconds, 1);
}

EXPORT int fpov_GetEyeTextureStageCount(void) {
    ApiTrace trace = trace_enter(TRACE_STAGE_COUNT, "GetEyeTextureStageCount", "args=none");
    pthread_once(&once, init);
    int result = real_get_eye_texture_stage_count ? real_get_eye_texture_stage_count() : -1000;
    trace_exit(trace, "GetEyeTextureStageCount", result);
    return result;
}

EXPORT int fpov_BeginFrame(int frame_index) {
    char arguments[48];
    snprintf(arguments, sizeof(arguments), "frame=%d", frame_index);
    ApiTrace trace = trace_enter(TRACE_BEGIN_FRAME, "BeginFrame", arguments);
    pthread_once(&once, init);
    int result = real_begin_frame ? real_begin_frame(frame_index) : -1000;
    if (real_begin_frame) {
        unity_begin_frame = frame_index;
        unity_begin_result = result;
        unity_begin_pending = 1;
    }
    trace_exit(trace, "BeginFrame", result);
    return result;
}

EXPORT int fpov_EndFrame(int frame_index) {
    char arguments[48];
    snprintf(arguments, sizeof(arguments), "frame=%d", frame_index);
    ApiTrace trace = trace_enter(TRACE_END_FRAME, "EndFrame", arguments);
    pthread_once(&once, init);
    int result = real_end_frame ? real_end_frame(frame_index) : -1000;
    trace_exit(trace, "EndFrame", result);
    return result;
}

// Opt-in compatibility entry point. Unity's direct BeginFrame lookup is redirected to fpov_BeginFrame so this
// wrapper can observe the real call. If that matching call is absent on this render thread, begin a real frame just
// before the original EndFrame. Both real return values remain unchanged; no waits or layers are synthesized.
EXPORT int fpob_EndFrame(int frame_index) {
    int unity_began = unity_begin_pending && unity_begin_frame == frame_index;
    unity_begin_pending = 0;  // do not let an unmatched earlier frame satisfy a later EndFrame
    unsigned long long thread = (unsigned long long)(uintptr_t)pthread_self();
    char arguments[96];
    snprintf(arguments, sizeof(arguments), "frame=%d begin_source=%s thread=%llu", frame_index,
             unity_began ? "unity" : "shim", thread);
    ApiTrace trace = trace_enter(TRACE_CONDITIONAL_END_FRAME, "ConditionalEndFrame", arguments);
    pthread_once(&once, init);

    int begin_called = !unity_began && real_begin_frame != NULL;
    int begin_result = unity_began ? unity_begin_result : -1000;
    if (begin_called) begin_result = real_begin_frame(frame_index);
    int end_result = real_end_frame ? real_end_frame(frame_index) : -1000;
    if (trace.enabled)
        LOG("ovrp conditional end-begin: call=%u frame=%d thread=%llu begin_source=%s begin_called=%d "
            "begin_result=%d end_result=%d", trace.api_call, frame_index, thread,
            unity_began ? "unity" : "shim", begin_called, begin_result, end_result);
    trace_exit(trace, "ConditionalEndFrame", end_result);
    return end_result;
}

// ---------------------------------------------------------------- input diagnostics (pass-through)
// The game's C# input (OVRInput) asks OVRPlugin which controllers are connected and for their button state. These
// wrappers return exactly what OVRPlugin returns and log the connected mask and every change of the button bits, to
// see whether presses reach the game (Accounting+ stuck at "press any button" while the runtime reports presses).
typedef struct { unsigned int words[3]; } State;  // the common start: ConnectedControllers, Buttons, Touches
// ovrpControllerState2 is returned by value (through x8): the type must have its exact size, 64 bytes (4 uint32,
// IndexTrigger[2], HandTrigger[2], Thumbstick[2] and Touchpad[2] as float pairs)
typedef struct { unsigned int words[16]; } State2;
_Static_assert(sizeof(State2) == 64, "ovrpControllerState2 is 64 bytes");
typedef unsigned int (*PFN_GetConnected)(void);
typedef int (*PFN_GetState4)(unsigned int mask, State *state);  // (written by OVRPlugin; only the start is read)
typedef State2 (*PFN_GetState2)(unsigned int mask);
static PFN_GetConnected real_connected;
static PFN_GetState4 real_state4;
static PFN_GetState2 real_state2;
static pthread_once_t input_once = PTHREAD_ONCE_INIT;

static void input_init(void) {
    void *ovrp = dlopen("libOVRPlugin.so", RTLD_NOW | RTLD_NOLOAD);
    if (!ovrp) return;
    real_connected = (PFN_GetConnected)dlsym(ovrp, "ovrp_GetConnectedControllers");
    real_state4 = (PFN_GetState4)dlsym(ovrp, "ovrp_GetControllerState4");
    real_state2 = (PFN_GetState2)dlsym(ovrp, "ovrp_GetControllerState2");
}

// per requested controller mask (the game may ask for Touch, Remote, Gamepad... separately)
static struct { unsigned int mask, connected, buttons; int seen; } masks[8];
static int changes;

// Face buttons on the Frame's controllers can read as clicked while only touched (Accounting+: buttons == touches ==
// A|B|X|Y held for seconds). A button held for more than STUCK_NS is reported released until it really lets go, so
// real presses (short) still register as a change.
#include <time.h>
#define STUCK_NS 2000000000ll
static long long held_since[32];
static unsigned int suppressed;

static long long now_ns(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec * 1000000000ll + t.tv_nsec;
}

static unsigned int unstick(unsigned int buttons) {
    long long now = now_ns();
    for (int b = 0; b < 32; b++) {
        unsigned int bit = 1u << b;
        if (!(buttons & bit)) {
            held_since[b] = 0;
            suppressed &= ~bit;
            continue;
        }
        if (!held_since[b]) held_since[b] = now;
        if (now - held_since[b] > STUCK_NS && !(suppressed & bit)) {
            suppressed |= bit;
            static int logged;
            if (logged++ < 20) LOG("ovrp input: button 0x%x held for > 2 s: reported released until let go", bit);
        }
    }
    return buttons & ~suppressed;
}

static void note_state(const char *which, unsigned int mask, const State *s) {
    int i = 0;
    while (i < 8 && masks[i].seen && masks[i].mask != mask) i++;
    if (i == 8) return;
    if (!masks[i].seen || masks[i].buttons != s->words[1] || masks[i].connected != s->words[0]) {
        if (changes++ < 400)
            LOG("ovrp input: %s(mask 0x%x): connected 0x%x buttons 0x%x touches 0x%x%s", which, mask, s->words[0],
                s->words[1], s->words[2], masks[i].seen ? "" : " (first call with this mask)");
        masks[i].seen = 1;
        masks[i].mask = mask;
        masks[i].connected = s->words[0];
        masks[i].buttons = s->words[1];
    }
}

EXPORT unsigned int fpov_GetConnectedControllers(void) {
    pthread_once(&input_once, input_init);
    unsigned int c = real_connected ? real_connected() : 0;
    static unsigned int last = 0xffffffffu;
    static int logged;
    if (c != last && logged++ < 20) LOG("ovrp input: connected controllers 0x%x", c);
    last = c;
    return c;
}

EXPORT int fpov_GetControllerState4(unsigned int mask, State *state) {
    pthread_once(&input_once, input_init);
    int r = real_state4 ? real_state4(mask, state) : -1000;
    if (state) {
        // ovrpControllerState4 continues with 4 floats after the 4 masks: L/R IndexTrigger, L/R HandTrigger. OVRInput
        // turns them into the trigger/grip buttons at 0.5; log each time one crosses it (per requested mask)
        const float *axis = (const float *)((const char *)state + 16);
        static struct { unsigned int mask, bits; } seen[8];
        static int axis_logs;
        unsigned int bits = 0;
        for (int k = 0; k < 4; ++k) bits |= (axis[k] >= 0.5f) << k;
        int i = 0;
        while (i < 8 && seen[i].mask && seen[i].mask != mask) i++;
        if (i < 8 && (!seen[i].mask || seen[i].bits != bits)) {
            if (seen[i].mask && axis_logs++ < 200)
                LOG("ovrp input: State4(mask 0x%x) triggers L %.2f R %.2f grips L %.2f R %.2f", mask, axis[0], axis[1],
                    axis[2], axis[3]);
            seen[i].mask = mask;
            seen[i].bits = bits;
        }
        note_state("State4", mask, state);
        state->words[1] = unstick(state->words[1]);
    }
    return r;
}

EXPORT State2 fpov_GetControllerState2(unsigned int mask) {
    pthread_once(&input_once, input_init);
    State2 s = {{0}};
    if (real_state2) s = real_state2(mask);
    note_state("State2", mask, (const State *)&s);
    s.words[1] = unstick(s.words[1]);
    return s;
}

// Unity's own input (libunity.so: Input.GetKey/anyKey on the Oculus device) reads ovrp_GetControllerState (v1,
// returned by value, 48 bytes: 4 uint32, IndexTrigger[2], HandTrigger[2], Thumbstick[2]) and ovrp_GetControllerState2
typedef struct { unsigned int words[12]; } State1;
_Static_assert(sizeof(State1) == 48, "ovrpControllerState is 48 bytes");
typedef State1 (*PFN_GetState1)(unsigned int mask);
static PFN_GetState1 real_state1;

EXPORT State1 fpov_GetControllerState(unsigned int mask) {
    pthread_once(&input_once, input_init);
    if (!real_state1) {
        void *ovrp = dlopen("libOVRPlugin.so", RTLD_NOW | RTLD_NOLOAD);
        if (ovrp) real_state1 = (PFN_GetState1)dlsym(ovrp, "ovrp_GetControllerState");
    }
    State1 s = {{0}};
    if (real_state1) s = real_state1(mask);
    note_state("State1(unity)", mask, (const State *)&s);
    return s;
}

// Oculus Utilities (1.3x) ignores every button unless OVRPlugin reports input focus; with Unity 2017's legacy frame
// loop on OVRPort's OVRPlugin that may never be true. Report it as true (the session's own focus still pauses the game)
// and log what OVRPlugin said.
typedef int (*PFN_GetInputFocus)(int *has_focus);
static PFN_GetInputFocus real_input_focus;

EXPORT int fpov_GetAppHasInputFocus(int *has_focus) {
    pthread_once(&input_once, input_init);
    if (!real_input_focus) {
        void *ovrp = dlopen("libOVRPlugin.so", RTLD_NOW | RTLD_NOLOAD);
        if (ovrp) real_input_focus = (PFN_GetInputFocus)dlsym(ovrp, "ovrp_GetAppHasInputFocus");
    }
    int value = 0, r = real_input_focus ? real_input_focus(&value) : -1000;
    static int last = -2, logged;
    if ((r < 0 ? -1 : value) != last && logged++ < 20) LOG("ovrp input: OVRPlugin input focus %d (result %d)", value, r);
    last = r < 0 ? -1 : value;
    if (has_focus) *has_focus = 1;
    return 0;  // ovrpSuccess
}

// ---------------------------------------------------------------- controller presses as mouse clicks
// Unity 2017-2019 games made for Go/Gear VR-era input wait for Input.GetMouseButtonDown(0/1), which a Quest delivers
// for the controller's primary press (Accounting+'s "press any button" motion warning checks only that). Lepton runs
// VR apps without a focused Android window, so no touch/mouse event ever arrives. IL2CPP looks engine functions up by
// name (il2cpp_resolve_icall) on first use: this registers a replacement for UnityEngine.Input::GetMouseButtonDown
// that returns Unity's answer, or true in the frame a Touch trigger (>= 0.5) or A/B/X/Y is newly pressed.
#define MOUSE_ICALL "UnityEngine.Input::GetMouseButtonDown(System.Int32)"
typedef void (*PFN_AddICall)(const char *name, const void *fn);
typedef const void *(*PFN_ResolveICall)(const char *name);
typedef int (*PFN_MouseDown)(int button);
static PFN_MouseDown real_mouse_down;
static volatile int click_now;  // set for the frame in which a press began

static int fp_mouse_down(int button) {
    int real = real_mouse_down ? real_mouse_down(button) : 0;
    return real || ((button == 0 || button == 1) && click_now);
}

static void mouse_install(void) {
    static int tries;
    if (real_mouse_down || tries > 600) return;  // ~10 s of frames
    tries++;
    PFN_AddICall add = (PFN_AddICall)dlsym(RTLD_DEFAULT, "il2cpp_add_internal_call");
    PFN_ResolveICall resolve = (PFN_ResolveICall)dlsym(RTLD_DEFAULT, "il2cpp_resolve_icall");
    if (!add || !resolve) {
        void *il2cpp = dlopen("libil2cpp.so", RTLD_NOW | RTLD_NOLOAD);
        if (il2cpp) {
            add = (PFN_AddICall)dlsym(il2cpp, "il2cpp_add_internal_call");
            resolve = (PFN_ResolveICall)dlsym(il2cpp, "il2cpp_resolve_icall");
        }
    }
    if (!add || !resolve) return;
    const void *current = resolve(MOUSE_ICALL);
    if (!current) return;  // Unity hasn't registered its own yet: try again next frame
    real_mouse_down = (PFN_MouseDown)current;
    add(MOUSE_ICALL, (const void *)fp_mouse_down);
    LOG("controller presses count as mouse clicks (%s %s)", MOUSE_ICALL,
        resolve(MOUSE_ICALL) == (const void *)fp_mouse_down ? "replaced" : "NOT replaced");
}

static void mouse_click_frame(void) {
    mouse_install();
    pthread_once(&input_once, input_init);
    static int was_pressed;
    unsigned char raw[512] __attribute__((aligned(16))) = {0};  // ovrpControllerState4 (+ room for newer layouts)
    int pressed = 0;
    if (real_state4 && real_state4(0x3, (State *)raw) >= 0) {
        const unsigned int *w = (const unsigned int *)raw;
        const float *axis = (const float *)(raw + 16);  // L/R IndexTrigger, L/R HandTrigger
        pressed = (w[1] & 0x303u) != 0 || axis[0] >= 0.5f || axis[1] >= 0.5f;  // A, B, X, Y or a trigger
    }
    click_now = pressed && !was_pressed;
    was_pressed = pressed;
}

// ---------------------------------------------------------------- hand nodes
// Unity's built-in Oculus XR input creates its left/right controller devices (InputDevices.GetDeviceAtXRNode, the
// Touch buttons and triggers games read through CommonUsages) for the hand nodes OVRPlugin reports as present
// (ovrp_GetNodePresent). Logged per node; a hand node whose Touch controller is connected counts as present.
typedef int (*PFN_NodePresent)(int node);
#define NODE_HAND_LEFT 3
#define NODE_HAND_RIGHT 4

EXPORT int fpov_GetNodePresent(int node) {
    static PFN_NodePresent real;
    static int logged[16];
    pthread_once(&input_once, input_init);
    if (!real) {
        void *ovrp = dlopen("libOVRPlugin.so", RTLD_NOW | RTLD_NOLOAD);
        if (ovrp) real = (PFN_NodePresent)dlsym(ovrp, "ovrp_GetNodePresent");
    }
    int present = real ? real(node) : 0, answer = present;
    if (!present && (node == NODE_HAND_LEFT || node == NODE_HAND_RIGHT) && real_connected)
        answer = (real_connected() & (node == NODE_HAND_LEFT ? 0x1u : 0x2u)) != 0;  // LTouch / RTouch
    if (node >= 0 && node < 16 && logged[node] != (answer ? 2 : 1)) {
        logged[node] = answer ? 2 : 1;
        LOG("ovrp node %d present: OVRPlugin %d -> Unity %d", node, present, answer);
    }
    return answer;
}

// ---------------------------------------------------------------- user presence (patch frame.unity_user_presence)
// Unity's Oculus XR Plugin gives its HMD input device a UserPresence feature from ovrp_GetUserPresent2. On the Frame
// OVRPort's OVRPlugin answers "not worn" a few seconds after start while the headset is worn, and games that drive
// their rig only while the user is present stay frozen (BONELAB's Marrow rig: XRHMD.IsUserPresent). The plugin's
// lookup is renamed to this function, which reports the user as present and logs OVRPlugin's own answer on change.
typedef int (*PFN_UserPresent2)(int *present);

EXPORT int fpov_GetUserPresent2(int *present) {
    static PFN_UserPresent2 real;
    if (!real) {
        void *ovrp = dlopen("libOVRPlugin.so", RTLD_NOW | RTLD_NOLOAD);
        if (ovrp) real = (PFN_UserPresent2)dlsym(ovrp, "ovrp_GetUserPresent2");
    }
    int value = 0, r = real ? real(&value) : -1000;
    static int last = -2, logged;
    if ((r < 0 ? -1 : value) != last && logged++ < 20) LOG("user presence: OVRPlugin %d (result %d) -> 1", value, r);
    last = r < 0 ? -1 : value;
    if (present) *present = 1;
    return 0;  // ovrpSuccess
}
