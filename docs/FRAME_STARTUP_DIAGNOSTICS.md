# Frame startup diagnostics

Use a per-game `framebridge.conf` for a bounded startup comparison. These settings
are off by default; restore the game's settings after collecting evidence.

`layer_debug=1` logs the first 16 entries and returns of each OpenXR frame API:
`xrWaitFrame`, `xrBeginFrame`, and `xrEndFrame`. The wait return includes
`shouldRender`, predicted time, and period. An entry without a matching return
identifies a blocking call; process survival alone does not establish rendering.

For GLES games, `snapshot=1` captures a submitted projection swapchain once per
second to `fb_snap_0.ppm` through `fb_snap_7.ppm`. Add `snapshot_quad=1` to capture
the first submitted quad instead, at full resolution, to `fb_quad_N.ppm`. This
helps inspect loading layers omitted by the projection capture. Neither capture
is the compositor's final headset view. A black image, especially during an
off-head session with `shouldRender=false`, does not establish a game stall.

The legacy Unity OVRPlugin shim logs at most 16 calls per traced API, with shared
sequence numbers and unchanged forwarded returns. Additional `fpov_` frame
wrappers are available for private same-length lookup diagnostics; normal builds
do not redirect those extra lookups. Do not synthesize frames or change return
values merely to make a startup trace appear active.

Keep three acceptance levels separate: APK/static checks, unattended device
startup and scene progression, and worn gameplay/controller confirmation.
