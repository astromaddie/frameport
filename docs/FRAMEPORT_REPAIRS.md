# Four-game repair record

This document records three repair families developed for Quest games on the Steam Frame: legacy Unity startup and
frame timing, Civilization VII expansion loading, and C-Smash VRS platform callbacks. The Unity family covers Until
You Fall and TO THE TOP. Each family contains the component patches needed for its observed failure; these are not
three interchangeable patches for every game.

The evidence cutoff is 2026-10-11. Device results below come from the repair audit and wearer feedback, not a new
device test performed while writing this document. Source references describe commit
[`8e8404e`](https://github.com/astromaddie/frameport/commit/8e8404ecf56af371e8d862f8972284d37d6dd401).

## Results and remaining work

| Game and Android package | Failure addressed | Recorded acceptance | Still unverified or unresolved |
|---|---|---|---|
| Until You Fall — `com.schellgames.untilyoufall` | Startup cubemap underlay crash; later legacy pose-time investigation | Wearer confirmed menu and gameplay on the earlier startup-repaired build. The later timing build survived an unattended run and corrected observed invalid view timestamps. | New timing build needs wearer QA. Low pointer angle and sluggish controllers were reported on the earlier playable build. |
| TO THE TOP — `com.electrichatgames.tothetop_al` | Legacy `EndFrame` submission before a matching `BeginFrame`; later pose-time investigation | Wearer confirmed gameplay on an earlier trial, with severe tracking jitter. Later conditional-Begin and timestamp builds passed unattended frame-order checks. | Latest build needs wearer QA. Smooth tracking and pointer angle are not accepted. |
| Civilization VII — `com.mrf.firaxis.invernessdemo` | OBB mount failures and a case-sensitive `Base.obb` lookup mismatch | Wearer confirmed menu and gameplay after directory loading and the filename alias were both present. | Low pointer angle, jitter, and the reported failure of Steam's End Game / Close App remain open. |
| C-Smash VRS — `com.wolfandwood.csmashvs` | Menu initialization waited forever for an unsupported version request | Normal repaired build reached the Terms of Use panel and 3D scene in a direct capture. The wearer subsequently reported that it works, with low frame rate. | Sustained 15.7–17.2 fps in the latest worn run. Detailed controller, full-game, and online acceptance remain unverified. |

Source/package validation, unattended launch, and wearer acceptance are separate gates. A balanced OpenXR frame loop
or an off-headset capture does not establish playable controller behavior, visual quality while worn, or frame rate.

## 1. Legacy Unity startup and frame timing

### Until You Fall: bypass the failing startup underlay

Until You Fall creates a custom 1×1 cubemap underlay through
`SG.XR.Oculus.XRPlatformOculus.SetUnderlayColor`. That startup path reached a compositor-layer failure. The existing
`frame.unity_no_overlay_copy` patch already skipped selected `OVROverlay.CreateLayer` and `PopulateLayer` methods;
the repair extended its method list to return early from this title's underlay method.

Implementation: [unity_runtime.py](../src/frameport/patches/frame/unity_runtime.py), commit
[`64a3d67`](https://github.com/astromaddie/frameport/commit/64a3d67db252311faa0980ad259eca8e1f225eeb).
This targets Unity games with ARM64, `libil2cpp.so`, and GLES graphics. Selection is manual or recipe-driven, not
automatic detection of an underlay crash.

The tradeoff is visible: skipped overlay paths can remove fades, solid-color underlays, and overlay splash screens
or menus. Only use the patch when that loss is acceptable for the affected title. It is a startup workaround, not
an MSAA repair.

An unattended loading diagnostic observed the game's 512×512 loading-logo quad without the original native crash.
The wearer subsequently confirmed menu and gameplay on the startup-repaired build identified below.

### TO THE TOP: begin only frames that have not already begun

The legacy Unity path could submit `EndFrame` without first forwarding the matching `BeginFrame`. An initial trial
injected Begin unconditionally; it could also begin a frame twice when Unity had already begun it. The retained
`frame.unity_legacy_begin_frame` implementation records the actual forwarded Begin on the render thread and calls
the real Begin immediately before End only when the same thread/frame lacks it.

The patch redirects the existing `libunity.so` lookup strings to wrappers in `libfp_ovrp.so`:

| Original lookup | Wrapper lookup |
|---|---|
| `ovrp_BeginFrame` | `fpov_BeginFrame` |
| `ovrp_EndFrame` | `fpob_EndFrame` |

The strings keep their original lengths. The transformer requires each original lookup exactly once and rejects
unexpected or already-present wrapper state. The wrapper preserves the real End result and introduces no waits or
layers. Implementation: [unity_legacy_begin_frame.py](../src/frameport/patches/frame/unity_legacy_begin_frame.py)
and [ovrpshim.c](../native/ovrpshim/ovrpshim.c).

This patch is experimental, manual, and never automatically recommended. Apply it only after a trace demonstrates
the missing matching Begin. It is distinct from moving the frame wait or changing pose timestamps.

### Both games: scope legacy pose timestamps to the waited frame

The subsequent controller investigation found legacy Unity view requests using the literal invalid `XrTime`
sentinel `100000000`. The manual `frame.unity_early_frame_wait` mode waits once at the first simulation/render
`Update2`, captures that frame's predicted display time, and exposes it only within the relevant physics-cache
scope. It requires `frame.unity_oculus_check` and the legacy ARM64 Unity loop with `libOVRPlugin.so`.

The adapter setting `legacy_pose_time=1` enables narrowly guarded corrections: replace that view sentinel, or a
recognized controller-dispatcher clock timestamp within the measured scope, with the owning frame's waited time.
The implementation releases the wait reservation before the original update so older rendering can progress. Its
optional, versioned adapter bridge does no correction when absent or disabled. Default legacy `fpov` behavior is
preserved; the early-wait path uses its separate `fpow` mode.

Implementation: [unity_early_frame_wait.py](../src/frameport/patches/frame/unity_early_frame_wait.py),
[ovrpshim.c](../native/ovrpshim/ovrpshim.c), and
[legacy_pose_time.c](../native/adapter/legacy_pose_time.c), all in commit
[`0c77d79`](https://github.com/astromaddie/frameport/commit/0c77d79631ae519a634c64619b875c7d487ea96).
These changes add no smoothing or pointer-angle offset and retain real runtime errors and diagnostic warnings.

Recorded unattended runs of the latest timing builds:

| Observation | Until You Fall | TO THE TOP |
|---|---:|---:|
| Run duration | 100.8 s | 95.468 s |
| Logged Wait/Begin/End triplets with matching frame times | 16 | 16 |
| View sentinel corrections matching their own waited time | 8 | 8 |
| Balanced five-second summaries | 18 | 24 |
| Discarded or automatically balanced frames | 0 | 0 |
| Call-order errors / fatal errors | 0 / 0 | 0 / 0 |
| Remaining OVR outside-frame warnings | 6,533 | 11,192 |

No controller-dispatcher pose calls were observed while the headset was off-head. These runs verify the exercised
view-time correction and frame ordering, not controller feel. The remaining warnings and wearer reports must not
be described as resolved. Civilization uses a different path: its diagnostic requests already matched predicted
time, so this legacy Unity repair was not applied to it.

### Selecting the Unity patches

From a source checkout, resolve the installed game's name or identifier with `uv run frameport list` and
`uv run frameport show "<game>"`. The commands below add individual patches to an existing recipe; they are not a
complete recipe and should not all be enabled for every Unity game.

```sh
# Until You Fall startup underlay workaround.
uv run frameport recipe "<game>" --enable frame.unity_no_overlay_copy

# A legacy game with a demonstrated missing Begin before End.
uv run frameport recipe "<game>" --enable frame.unity_legacy_begin_frame

# Separate, experimental early-wait comparison; prerequisite must be selected.
uv run frameport recipe "<game>" --enable frame.unity_oculus_check
uv run frameport recipe "<game>" --enable frame.unity_early_frame_wait
```

Build and install commands are documented in [INSTALL.md](INSTALL.md#command-line). Preserve the known working
recipe and APK before an experimental comparison. The audit's final per-title settings enabled only
`legacy_pose_time=1` beyond the respective baseline; temporary diagnostic settings were removed. See
[FRAME_STARTUP_DIAGNOSTICS.md](FRAME_STARTUP_DIAGNOSTICS.md) for bounded frame-call and loading-layer diagnostics.

## 2. Civilization VII: validated directories for OBB expansions

The Android OBB mounting path failed on this title. The repair supplies already-extracted expansion directories
through a title-specific NativeActivity subclass. The helper accepts only unencrypted, keyless `.obb` requests
directly under the configured root, with a sibling extraction and ready marker matching source size and mtime.
Requests outside those conditions fall back to Android's original service.

The APK packager sets the existing `android:hasCode` attribute to `true`, substitutes the reviewed activity, and
adds one helper DEX. In the recorded build that entry is `classes2.dex`. Existing DEX files and native libraries
retain their original bytes. The packager guards the exact package and sole native MAIN activity, rejects an
existing helper, and verifies the original non-signature payloads plus the matching signing certificate.

Directory mounting alone was insufficient. An earlier run still crashed after preparing eleven directories:
the native game probed literal lowercase `base.obb`, while its source expansion was `Base.obb`. The validated
relative alias `base.obb -> Base.obb` allowed the missing lookup. The original image was neither renamed nor copied.

Source: [directory OBB utility](../scripts/civilization_directory_obb/README.md),
[DirectoryMount.java](../scripts/civilization_directory_obb/java/frameport/civ/DirectoryMount.java), and
[extract_obb.py](../scripts/civilization_directory_obb/extract_obb.py). Commits:
[`d0f5d1e`](https://github.com/astromaddie/frameport/commit/d0f5d1e2e5d06900761f2d3db873b545b2c90e20) for directory
loading and [`0f128cc`](https://github.com/astromaddie/frameport/commit/0f128cce725b81dda7f3324d001fd541b4242cb1)
for the alias. This is a developer utility, not an automatic FramePort patch ID.

### Applying the complete repair

Both the signed APK helper and the validated external directory tree are required. The repaired APK alone does
not contain the expansions. Follow the utility's dependency and signing instructions; use the existing matching
FramePort key. Package an APK already imported or built by FramePort:

```sh
PYTHONPATH=src python scripts/civilization_directory_obb/package_directory_loader.py \
  --input /path/to/frameport-import-or-build.apk \
  --output /path/to/civilization-directory-loader.apk \
  --javac /path/to/jdk-17/bin/javac \
  --android-jar /path/to/android-sdk/platforms/android-31/android.jar \
  --d8 /path/to/android-sdk/build-tools/35.0.0/d8
```

Extract every expansion into the game's OBB root with the footer owner `com.firaxis.invernessdemo`, which differs
from the APK package. For `Base.obb`, request the alias explicitly:

```sh
python scripts/civilization_directory_obb/extract_obb.py \
  --root /path/to/Android/obb/com.mrf.firaxis.invernessdemo \
  --owner com.firaxis.invernessdemo \
  --alias base.obb \
  /path/to/Android/obb/com.mrf.firaxis.invernessdemo/Base.obb
```

For the other expansions, use their own source paths and omit `--alias`. Publish the complete validated output
and alias into the game's actual OBB root before testing. The extractor refuses stale output, unsafe paths,
owner mismatches, or an occupied/unsafe alias; it does not overwrite those conditions to force installation.

After the alias was present, the audit recorded eleven mount requests and eleven successful state-1 callbacks,
54 balanced XR summaries, and no call-order error, native segmentation fault, or fatal error in that run. The
wearer then confirmed menu and gameplay. A uniform standby compositor image in the unattended run was not used
as visual proof. Steam close succeeded once unattended, but lacked a persistent request/host receipt; the reported
worn close failure remains open.

## 3. C-Smash VRS: complete an unsupported version request

C-Smash's menu initializer called `Application.GetVersion().OnComplete` and stayed in `CHECKING_UPDATE` while
waiting for the callback. The installed platform loader's `ovr_Application_GetVersion` was an eight-byte ARM64
stub returning request ID zero. No completion message reached the application, so a healthy frame loop still
produced no initialized menu.

`frame.platform_version_callback` handles this exact unsupported stub. A real nonzero request is forwarded
unchanged. When the stub returns zero, the shim allocates a nonzero request ID and queues an error completion:
message type `0x68670A0E`, error code `501`, HTTP code `0`, with an unsupported-version explanation. The game's
existing error callback clears the checking state and allows initialization to proceed. The patch supplies no
version number, login, entitlement, or online-service success.

The important integration detail is the message queue. Interposing GetVersion alone cannot deliver the shim's
message through the loader's queue. The patch therefore routes all eleven version/message/error function exports
through `libfp_langpack.so`: GetVersion, PopMessage, FreeMessage, the four Message getters, and the four Error
getters. Foreign messages and handles forward to the original loader APIs. Language-pack and asset APIs remain
loader-bound; upgrading an already-linked loader preserves the existing metadata and asset flags.

Implementation: [platform_version_callback.py](../src/frameport/patches/frame/platform_version_callback.py) and
[langpack.c](../native/langpack/langpack.c), commit
[`8e8404e`](https://github.com/astromaddie/frameport/commit/8e8404ecf56af371e8d862f8972284d37d6dd401).
The patch is experimental and opt-in; detection does not auto-select it. Mutation requires an ARM64 little-endian
ELF with unique defined function exports and the exact eight stub bytes `e0031faac0035fd6` (`mov x0, xzr; ret`). A
different loader must fail the guard rather than receive this transformation based only on its filename.

Select it in the affected game's existing recipe, then build and install through the normal signed pipeline:

```sh
uv run frameport recipe "<game>" --enable frame.platform_version_callback
uv run frameport build "<game>"
uv run frameport install "<game>"
```

In six baseline diagnostic samples, the version state remained `CHECKING_UPDATE` and menu/network/avatar
initialization remained incomplete. In six repaired samples, the checking state cleared and those initialization
flags completed. The normal production APK then produced a direct projection capture showing the Terms of Use
panel and 3D geometry. Only `libovrplatformloader.so` and the added `libfp_langpack.so` changed in that APK. The
adapter, GL library, game code/assets, and original 4× MSAA remained byte-identical.

Temporary snapshot settings were restored after the unattended capture. Terms were not accepted and gameplay/
controllers were not exercised during that diagnostic. The wearer subsequently reported that C-Smash works, but
feels slow. Three nonfatal achievement-manager null-reference exceptions occurred in both baseline and repaired
runs and remain outside this repair. A SteamVR standby capture alone would have missed the positive game-frame
evidence.

### Subsequent worn frame-rate result

The 2026-10-11 worn run used the same production APK, verified by its SHA-256 below. Its saved launch log records
60.2 fps before a sustained drop to 15.7–17.2 fps across seven successive five-second reporting windows, ending at
09:36:56 JST. This confirms the wearer's low-frame-rate report; startup success does not imply acceptable speed.

The [adapter's pacing counter](../native/adapter/frame_adapter.c) counts `xrEndFrame` calls over monotonic elapsed
time. The separate `displayTime vs predicted` columns measure submitted-versus-predicted display-time error;
they are not CPU/GPU timings or hitch durations. Those columns reached roughly 58–64 ms average and 250.49 ms
maximum during the slow interval. Software H.264 decoding (`OMX.google.h264.decoder` and `SoftwareRenderer`) starts
near the drop, but this correlation does not establish the bottleneck. No settings or APK bytes were changed for
this read-only check. The private receipt is
`frameport-audit-20261011/csmash-worn-performance/result.json` with the original launch log and installed APK hash.

### Opt-in tutorial video performance trial

The privately inspected 1.2.0.7 assets contain one 3840×2160, 59.94 fps tutorial video, `CSVRS - Ball Coaxing`.
The other eleven clips are 1280×720. All six scene VideoPlayers use the existing fixed 1280×720 tutorial
RenderTexture and loop playback; the selected clip lasts 8.98 seconds. This makes repeated video playback a
plausible contributor to the sustained slow interval, but the worn log does not identify the active clip.

[prepare_csmash_video_trial.py](../scripts/prepare_csmash_video_trial.py) creates a private, experimental bundle
with only that clip resized to the existing target dimensions. It requires the exact original bundle SHA-256
`cfcd39939ec752b5151fc82af2d1a92dabeea72d5e85c36f3eac8a0882e42a7c`, UnityPy 1.25.4, and FFmpeg with libx264.
It preserves cadence, frame count, duration, stream offsets, all other serialized objects, and the eleven other
clip payloads, checking parity after reopening the output. It refuses unknown input or an existing output directory.
It does not install anything or change the APK, callback repair, adapter, controller support, eye resolution, or MSAA.

```sh
python scripts/prepare_csmash_video_trial.py /private/original/videos_assets_all.bundle /private/new-trial
```

Explicit matching BT.2020 limited-range input/output matrices preserve the original video's effective Android
color interpretation during resizing. A plain untagged 720p conversion would change Android's resolution-based
color default and was rejected. The final output matches the original decoder's actual color standard/transfer/
range (6/3/2), output dataspace (281411584), and SoftwareRenderer window dataspace (`0x8c60000`). Against the
original resized with the same matrix and bilinear filter, all 538 frames measure SSIM 0.997790 and PSNR 51.229 dB.
Four corresponding frame samples were visually consistent; these metrics do not substitute for in-game video QA.

On the same Frame, an isolated decoder-plus-SoftwareRenderer surface test consumed all 538 frames in 7.520 seconds
for the original and 1.095 seconds for the final candidate (71.54 versus 491.33 frames/second). Process CPU time
fell from 7.267 to 1.119 seconds. This measures video processing headroom, **not Unity rendering or XR frame rate**.
Raw decoding alone already exceeded 128 fps, so the decoder alone is not established as the game bottleneck.

The final bundle SHA-256 is `55b2d6774847e931eef4bf8a32627693ca1505e255b0e7f0bf7aeb897b87d508`.
Normal startup renders the C-Smash logo, scene, and hands; the mounted bundle hash matches, and temporary snapshot
settings were removed before wearer handoff. Real Ball Coaxing playback smoothness, color, and worn frame rate
remain unverified. The trial is not automatically selected by any recipe. Original bundle and device-local data
backup remain available. Private conversion, parity, quality, surface-test, and capture evidence lives under
`csmash/performance-20261011/`, including `repro-source-run/receipt.json`, `surface-color-comparison.txt`, and
`color-preserved-v2/`. No game assets or diagnostic APK are included in this repository change.

## Build identities

These SHA-256 values identify recorded APKs. They do not establish which file is currently installed after future
updates. Hash the installed APK and compare it before attributing a new test result to a build.

| Recorded APK | SHA-256 |
|---|---|
| Until You Fall — wearer-confirmed startup repair | `05df62ac67ce5bf4427a25d7688af675e8defb257eb3da007e3edc7d4921d937` |
| Until You Fall — latest scoped-time experiment | `a53dc646428a7a0c6dbe17a429e7325f7a253ecbc42d764e4fa8717fdebc8f7c` |
| TO THE TOP — earlier wearer-confirmed trial | `154bd8be651bddd119ea4c9a3769fd4da71c698555333349066a82c137754d01` |
| TO THE TOP — conditional-Begin rollback baseline, not wearer-accepted | `0c3ffb8497869eb750b9148ef0fa3ac955076dd5adb630f4157a87552f64a549` |
| TO THE TOP — latest conditional-Begin/scoped-time experiment | `7c7bb2bea4d99fd42d9a2f181c43fdaadbbe9bd6e47f1208db84e36d1fa86098` |
| Civilization VII — directory loader; external expansions/alias also required | `25936171486a76f15368498a81b052a51d126750b9ecd9af1941c9b3df85adec` |
| C-Smash VRS — normal production callback repair | `6d19d91623e080373b687aabfcb2551f9747235343943515701546c969eda1a2` |

## Verification and audit evidence

Relevant source tests are [test_unity_runtime.py](../tests/test_unity_runtime.py),
[test_unity_legacy_begin_frame.py](../tests/test_unity_legacy_begin_frame.py),
[test_unity_early_frame_wait.py](../tests/test_unity_early_frame_wait.py),
[test_ovrpshim_native.py](../tests/test_ovrpshim_native.py),
[test_civilization_directory_obb.py](../tests/test_civilization_directory_obb.py),
[test_platform_version_callback.py](../tests/test_platform_version_callback.py), and
[test_langpack.py](../tests/test_langpack.py).

The Unity timing handoff recorded 29 focused tests passing, scoped Ruff checks, ARM64/ARM32 NDK builds, coherent
artifact hashes, and expected production-parity results with matching APK certificates. The existing host lacked
Typer, so production patch/parity APIs were invoked directly instead of claiming the full CLI/catalog had run.
The callback handoff recorded six focused tests passing and 23 Linux-only C integration tests skipped on macOS;
the ARM64 NDK build and real Frame callback path supplied additional evidence, without replacing those skipped
tests. Civilization packaging verified the matching certificate and unchanged original application payloads.

Raw APKs, expansions, screenshots, logs, signing keys, and game code remain outside this repository. The private
audit directory `frameport-repairs-20261010/` retains the following receipts for reproduction and review:

| Evidence | Private receipt, relative to the audit directory |
|---|---|
| Consolidated status and wearer feedback | `repair-status.json` |
| Unity source/package checks | `controller-timing/scoped-time-source-receipt.json` |
| Unity native artifact coherence | `controller-timing/native-artifact-coherence-20261011.json` |
| Until You Fall latest unattended timing run | `controller-timing/until-scoped-time-20261011/result.json` |
| TO THE TOP latest unattended timing run | `controller-timing/ttt-scoped-time-20261011/result.json` |
| Civilization successful directory-plus-alias run | `civilization/case-alias-trial/device-result.json` |
| C-Smash production package checks | `csmash/version-callback-repair-package-receipt.json` |
| C-Smash direct projection and restoration | `csmash/msaa-force-render-20261011/diagnostic-followup-result.json` |

For another title, first reproduce its specific failure with bounded diagnostics, then check the relevant patch
guards. Preserve a rollback APK/recipe, verify signed payloads and certificate continuity, launch through Steam,
and record both startup evidence and wearer acceptance against the exact build hash. Copying all patches into a
recipe without that diagnosis is not supported by this record.

The open acceptance work is controller feel/pointing in the three earlier played games, Civilization's worn Steam
close behavior, and C-Smash's performance and detailed gameplay/controller tests. RE4 was also wearer-confirmed
working, but Steam's End Game failed; that separate exit investigation remains open. Its selected
`patch_remove_unreal_force_quit` alternate also disables in-game Quit, so that button is not a substitute for
repairing Steam shutdown. Swarm 2's MSAA seams are a separate unresolved investigation;
disabling MSAA hides some artifacts but introduces jagged edges and is not a completed graphics repair. No 90 Hz
or 120 Hz performance result is established by these four-game repairs.
