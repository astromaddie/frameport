# Saints & Sinners Chapter 1 startup investigation

Verified unattended on 2026-10-12. The installation succeeded. FramePort's
separate automatic headless launch test failed; the user had not tried the game
in the headset. Two ordinary Steam launches subsequently reached stereo frame
submission without an APK, launcher, or settings change. Worn menus, controls,
gameplay, graphics, and SteamVR dashboard exit remain unverified.

## Installed identity

- Package: `com.SDI.TWD`, Chapter 1 / `STAGECH1`, build 400355, version code
  40035500 (2024-01-19 shipping build), ARM64 Unreal.
- Installed APK: 85,139,454 bytes, SHA-256
  `fccf00af8586105ed0ffb38032448f5f51b2b36f090c3fde6a6612c04d8c3d37`.
- Expansion files: `main.40035500.com.SDI.TWD.obb` (4,294,452,788 bytes) and
  `patch.40035500.com.SDI.TWD.obb` (3,559,453,345 bytes).
- OVRPort runtime `3.4.3-aa54c3f`, installed FramePort agent 80, Lepton 3.0.5.
- Deployment app ID 2152352472; current Steam devkit ID 3384173317. Both installed
  launchers already preserve Steam's inherited `SteamAppId`.
- Investigation source baseline: FramePort `0f3f4b3`. Installed agent source was
  used for launch/stop; the desktop checkout was not deployed over it.

## Why FramePort showed a warning

The original install job completed at 15:05 JST. Static checks, signature,
alignment, dependency resolution, transfer, and Steam registration passed. The
library records `installs.frame.result.ok=true` and two transferred expansion
files. Android's initial streamed install returned `Success`.

The following automatic, non-Steam headless test recorded `EXITED after 42s`,
furthest milestone `Lepton container started`, with no classified finding or
suggested repair. Its 742-byte retained launch log does not contain an Android
backtrace. This is a failed startup check following a successful installation;
it does not demonstrate an incomplete transfer. The cause of that original exit
was not established and was not assumed to be a game crash or data corruption.

## Repeatable normal Steam startup

With explicit headset-off permission and an exclusive canonical Frame booking:

1. Back up this title's `lepton-data/internal` and `external`, recording file
   hashes and modes, plus the launcher/settings/deployment and old launch log.
2. Import the installed agent and call
   `cmd_launch({'package': 'com.SDI.TWD', 'fallback': False, 'wait': 1})`.
   This uses the existing devkit entry and normal Steam launch route.
3. Capture Android main/crash/system logs from the selected
   `lepton-steamlaunch-3384173317` container as soon as it starts.
4. Observe for 75 seconds, then stop only this title with the installed agent's
   `cmd_stop`. Repeat the ordinary Steam launch after verifying no scene app is
   active.

The two launches began at approximately 20:02:15 and 20:04:58 JST. Both stayed
running until the investigation stopped them (76.31 and 75.72 seconds including
teardown). Both logged successful OpenXR/OVRPlugin initialization, a 1728 x 1728
two-slice main-eye swapchain, and separate left/right projection views. The
second run's late pacing windows repeatedly reported 72.2 fps. Neither capture
contained a fatal signal, fatal Java exception, or `xrEndFrame failed` line.
These are startup/frame-submission results, not display/GPU timing or wearer
acceptance. Optional profile/performance-extension warnings and early
outside-frame Update warnings did not prevent continued submissions.

The compact [evidence record](evidence/saints-sinners-startup-20261012.json)
retains the two run results, native-library hashes, decisive log lines, original
capture hashes, unchanged-file hashes, and restoration/cleanup result.

## Comparison with IEYTD

IEYTD's conditional-Begin companion targets demonstrated legacy Unity End
without matching Begin. This installed Saints & Sinners build uses Unreal and
already progresses through frame submission; there is no evidence requiring
that Unity repair. IEYTD's Steam identity correction is already present here.

The separate Saints & Sinners Chapter 2 microphone workaround also was not
needed: the install job explicitly detected the upstream fixed microphone
runtime and omitted `frame.ovr_microphone`. No compatibility patch was added.

## Retained state and cleanup

The APK, launcher, settings, deployment, and expansion payloads were not replaced.
After both launches ended, restore the original internal/external data and verify
the exact file set plus all 13 original file hashes and modes. This passed.
Remove the investigation backup and raw captures only after extracting the
compact evidence: 37,797,888 allocated bytes (about 36 MiB) of diagnostics and
backup were removed. Temporary host capture scripts and the isolated source
checkout are removed after the report is committed and pushed. Existing work in
the shared FramePort checkout is preserved.

Next acceptance step: launch **The Walking Dead: Saints & Sinners** from the
Frame's Steam library and check menus, input, and a short gameplay segment in the
headset. Keep the old failed headless record as historical evidence; do not mark
the title wearer-verified from these unattended runs.
