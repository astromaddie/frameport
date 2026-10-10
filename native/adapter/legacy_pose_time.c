// SPDX-License-Identifier: GPL-3.0-only
// Only the opt-in fpow physics call can open this scope. No global timestamp heuristic.
#include "../legacy_pose_time.h"
static __thread int64_t legacy_wait_time;
static __thread FrameportPoseScope legacy_scope;
static int64_t monotonic_ns(void);
static pthread_mutex_t legacy_pose_lock = PTHREAD_MUTEX_INITIALIZER;
static XrAction legacy_actions[64];
static XrSpace legacy_spaces[64];
static int legacy_action_count, legacy_space_count;
static void legacy_note_binding(XrAction action, const char *path) {
    size_t n = strlen(path);
    if (strncmp(path, "/user/hand/", 11) ||
        !((n >= 9 && !strcmp(path + n - 9, "/aim/pose")) ||
          (n >= 10 && !strcmp(path + n - 10, "/grip/pose")))) return;
    pthread_mutex_lock(&legacy_pose_lock);
    int found = 0;
    for (int i = 0; i < legacy_action_count; ++i) found |= legacy_actions[i] == action;
    if (!found && legacy_action_count < 64) legacy_actions[legacy_action_count++] = action;
    pthread_mutex_unlock(&legacy_pose_lock);
}
static void legacy_note_space(XrAction action, XrSpace space) {
    pthread_mutex_lock(&legacy_pose_lock);
    int known = 0, found = 0;
    for (int i = 0; i < legacy_action_count; ++i) known |= legacy_actions[i] == action;
    for (int i = 0; i < legacy_space_count; ++i) found |= legacy_spaces[i] == space;
    if (known && !found && legacy_space_count < 64) legacy_spaces[legacy_space_count++] = space;
    pthread_mutex_unlock(&legacy_pose_lock);
}
static int is_controller_space(XrSpace space) {
    pthread_mutex_lock(&legacy_pose_lock);
    int found = 0;
    for (int i = 0; i < legacy_space_count; ++i) found |= legacy_spaces[i] == space;
    pthread_mutex_unlock(&legacy_pose_lock);
    return found;
}
static void forget_pose_space(XrSpace space) {
    pthread_mutex_lock(&legacy_pose_lock);
    for (int i = 0; i < legacy_space_count; ++i)
        if (legacy_spaces[i] == space) { legacy_spaces[i] = legacy_spaces[--legacy_space_count]; break; }
    pthread_mutex_unlock(&legacy_pose_lock);
}
#define POSE_EXPORT __attribute__((visibility("default")))
POSE_EXPORT void framebridge_pose_wait_reset_v1(void) { legacy_wait_time = 0; }
POSE_EXPORT int64_t framebridge_pose_wait_time_v1(void) { return legacy_pose_time ? legacy_wait_time : 0; }
POSE_EXPORT void framebridge_pose_push_v1(int64_t predicted, FrameportPoseScope *saved) {
    *saved = legacy_scope;
    legacy_scope = (FrameportPoseScope){legacy_pose_time ? predicted : 0, monotonic_ns()};
}
POSE_EXPORT void framebridge_pose_pop_v1(const FrameportPoseScope *saved) { legacy_scope = *saved; }

static XrTime legacy_query_time(XrTime requested, int controller) {
    if (!legacy_pose_time || legacy_scope.predicted <= 0 || requested == legacy_scope.predicted) return requested;
    int64_t now = monotonic_ns();
    int correction = controller ? requested >= legacy_scope.mono_start && requested <= now : requested == 100000000;
    if (!correction) return requested;
    static unsigned int logged[2];
    if (__atomic_fetch_add(&logged[controller], 1, __ATOMIC_RELAXED) < 8)
        LOG("legacy_pose_time: source=%s requested=%lld replacement=%lld mono_start=%lld mono_end=%lld",
            controller ? "controller-dispatcher-mono" : "views-physics-sentinel", (long long)requested,
            (long long)legacy_scope.predicted, (long long)legacy_scope.mono_start, (long long)now);
    return legacy_scope.predicted;
}

static XrTime legacy_space_time(XrSpace space, XrTime requested) {
    return legacy_scope.predicted && is_controller_space(space) ? legacy_query_time(requested, 1) : requested;
}
