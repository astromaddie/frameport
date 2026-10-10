// SPDX-License-Identifier: GPL-3.0-only
#ifndef FRAMEPORT_LEGACY_POSE_TIME_H
#define FRAMEPORT_LEGACY_POSE_TIME_H
#include <stdint.h>
// Versioned private ABI between the opt-in Unity shim and FrameBridge. Times are nanoseconds.
typedef struct { int64_t predicted, mono_start; } FrameportPoseScope;
typedef void (*FrameportPoseWaitReset)(void);
typedef int64_t (*FrameportPoseWaitTime)(void);
typedef void (*FrameportPosePush)(int64_t, FrameportPoseScope *);
typedef void (*FrameportPosePop)(const FrameportPoseScope *);
#endif
