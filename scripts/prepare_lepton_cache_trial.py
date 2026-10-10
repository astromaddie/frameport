#!/usr/bin/env python3
"""Build and operate an opt-in Tetris Effect Lepton cache-retention trial.

The repository contains only the builder and patch logic. The generated Lepton
entrypoint is made from the installed runtime on the Frame after its source
hashes and title paths pass the gate; no Lepton source or binaries are bundled.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]
CAPTURE_ROOT = REPO_ROOT / "work/frameport-audit-20261010/runtime-source"
LAUNCH_CAPTURE = (
    REPO_ROOT
    / "work/frameport-audit-20261010/evidence/com.enhanceexperience.tetriseffect/launch.sh"
)
DEFAULT_OUTPUT = REPO_ROOT / "work/lepton-cache-trial/candidate"

RUNTIME_DIR = Path("/home/steamos/.local/share/Steam/steamapps/common/Lepton")
APP_DIR = Path("/home/steamos/Applications/quest-frame/com.enhanceexperience.tetriseffect")
STEAM_APP_ID = "3150751987"
PACKAGE_ID = "com.enhanceexperience.tetriseffect"
RUNTIME_VERSION = "compat tool: v3.0.5, rootfs: v3.0.3"
MARKER_NAME = ".frameport-lepton-cache-trial.json"
MARKER_OWNER = "frameport-lepton-cache-trial-v1"

SOURCE_HASHES = {
    "lepton": "3963c0000d530e3729ba5209d7e27fda7fb499c00b45f8f0797ac0fb045cc07e",
    "liblepton/liblepton.sh": "5f7fa5db485e64f9171b1d90b89db8a6394035d6fe95d9ee55d0b334b0a819ab",
    "liblepton/baking.sh": "359d9a151748f11514920479bb3b57fc848160e87a4d56a8f50f114065bd30fc",
    "liblepton/app_metadata.sh": "6cb92d7e2abed8dbc60392211f44d1226f6e7106920104fb0abbaeb339ab6e96",
    "liblepton/mounting.sh": "88d2c9f27d3671ebfb96db54d6754994069e9c586fc47cc7e0c8fb4a61b2f8a4",
    "liblepton/utils.sh": "d695603561c2893e1da2007ffa9c242cdc664e629026646adfdcde6076ba370c",
}
LAUNCH_SHA256 = "468513d425b63435cd612f4a2bac8ee7d011fb7ce668938908f765e0721089aa"


class TrialError(RuntimeError):
    """A source gate or safe-path check failed."""


class UnsafeCacheState(TrialError):
    """The cache cannot be touched without risking another mounted context."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def trial_identity() -> str:
    payload = {
        "owner": MARKER_OWNER,
        "runtime_version": RUNTIME_VERSION,
        "runtime_dir": str(RUNTIME_DIR),
        "app_dir": str(APP_DIR),
        "steam_app_id": STEAM_APP_ID,
        "package_id": PACKAGE_ID,
        "source_hashes": SOURCE_HASHES,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _manifest_hashes(manifest_path: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        parts = [part.strip() for part in line.split("|")]
        if len(parts) == 4 and re.fullmatch(r"[0-9a-f]{64}", parts[3]):
            found[parts[1]] = parts[3]
    return found


def validate_capture(capture_root: Path = CAPTURE_ROOT, launch_path: Path = LAUNCH_CAPTURE) -> None:
    """Verify the local read-only capture still matches the audited runtime."""
    manifest = capture_root / "manifest.txt"
    version = capture_root / "version.txt"
    if not manifest.is_file() or not version.is_file():
        raise TrialError(f"missing captured Lepton manifest/version under {capture_root}")
    if version.read_text(encoding="utf-8").strip() != RUNTIME_VERSION:
        raise TrialError("captured Lepton version differs from the approved v3.0.5/rootfs v3.0.3 gate")

    listed = _manifest_hashes(manifest)
    for relative_path, expected in SOURCE_HASHES.items():
        if listed.get(relative_path) != expected:
            raise TrialError(f"manifest hash mismatch for {relative_path}")
        source = capture_root / relative_path
        if not source.is_file() or sha256_file(source) != expected:
            raise TrialError(f"captured source hash mismatch for {relative_path}")
    if not launch_path.is_file() or sha256_file(launch_path) != LAUNCH_SHA256:
        raise TrialError("captured Tetris Effect launcher hash mismatch")


def _teardown_function_source() -> str:
    """Return the small Bash adapter inserted into the generated entrypoint."""
    helper = APP_DIR / "lepton-cache-trial/prepare_lepton_cache_trial.py"
    return f'''function frameport_lepton_cache_trial_finish_prefix()
{{
    local APP_BAKED=false
    if is_app_baked; then APP_BAKED=true; fi

    local TRIAL_RC=0
    if python3 "${{LEPTON_CACHE_TRIAL_HELPER:-{helper}}}" finish-prefix \\
        --runtime-dir "${{LEPTON_CACHE_TRIAL_RUNTIME_DIR:-{RUNTIME_DIR}}}" \\
        --app-dir "{APP_DIR}" \\
        --install-path "${{STEAM_COMPAT_INSTALL_PATH:-}}" \\
        --data-path "${{STEAM_COMPAT_DATA_PATH:-}}" \\
        --steam-app-id "${{SteamAppId:-}}" \\
        --context "${{LEPTON_CONTEXT:-}}" \\
        --package-id "$(get_app_id)" \\
        --normal-teardown "${{LEPTON_CACHE_TRIAL_NORMAL_TEARDOWN:-false}}" \\
        --early-shutdown "${{EARLY_SHUTDOWN:-1}}" \\
        --dump-logcat "${{LEPTON_DUMP_LOGCAT:-}}" \\
        --app-or-depot-changed "${{APP_OR_DEPOT_CHANGED:-true}}" \\
        --app-baked "$APP_BAKED" \\
        --keep-context "${{LEPTON_KEEP_CONTEXT:-}}" \\
        --no-cleanup "${{LEPTON_NO_CLEANUP:-false}}" \\
        --prefix-path "${{LEPTON_PREFIX:-}}"; then
        return 0
    else
        TRIAL_RC=$?
    fi

    if [[ "$TRIAL_RC" == "10" ]]; then
        # The helper already stopped/removed this exact context and proved the
        # upper/work dirs are unmounted. Preserve Lepton's original clear path.
        remove_prefix
    else
        println "Lepton cache trial left baked paths untouched: safe unmount/cleanup could not be proved (status $TRIAL_RC)."
    fi
}}
'''


def patch_runtime_text(source: str) -> str:
    """Patch only the captured teardown handoff and runtime library binding."""
    script_dir_line = 'SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )'
    if source.count(script_dir_line) != 1:
        raise TrialError("Lepton SCRIPT_DIR anchor changed; refusing to generate an entrypoint")
    source = source.replace(
        script_dir_line,
        f'SCRIPT_DIR="${{LEPTON_CACHE_TRIAL_RUNTIME_DIR:-{RUNTIME_DIR}}}"',
        1,
    )

    source_include = 'source "${SCRIPT_DIR}/liblepton/liblepton.sh"\n\ncase "${COMMAND}" in'
    if source.count(source_include) != 1:
        raise TrialError("Lepton library-source anchor changed; refusing to generate an entrypoint")
    source = source.replace(
        source_include,
        'source "${SCRIPT_DIR}/liblepton/liblepton.sh"\n\n'
        + _teardown_function_source()
        + '\ncase "${COMMAND}" in',
        1,
    )

    top_guard_anchor = "set -euo pipefail\n"
    if source.count(top_guard_anchor) != 1:
        raise TrialError("Lepton startup guard anchor changed; refusing to generate an entrypoint")
    guard = f'''set -euo pipefail

# Trial is restricted to this Steam title and validated before Lepton mutates
# the container or its prefix. Any mismatch falls through to installed Lepton.
if [[ "${{1:-}}" == "start" || "${{1:-}}" == "waitforexitandrun" ]]; then
    TRIAL_RUNTIME="{RUNTIME_DIR}"
    TRIAL_APP="{APP_DIR}"
    TRIAL_HELPER="$TRIAL_APP/lepton-cache-trial/prepare_lepton_cache_trial.py"
    if [[ "${{SteamAppId:-}}" == "{STEAM_APP_ID}" ]]; then
        if python3 "$TRIAL_HELPER" preflight \\
            --runtime-dir "$TRIAL_RUNTIME" --app-dir "$TRIAL_APP" \\
            --install-path "${{STEAM_COMPAT_INSTALL_PATH:-}}" \\
            --data-path "${{STEAM_COMPAT_DATA_PATH:-}}" \\
            --steam-app-id "${{SteamAppId:-}}" \\
            --keep-context "${{LEPTON_KEEP_CONTEXT:-}}" \\
            --no-cleanup "${{LEPTON_NO_CLEANUP:-false}}"; then
            :
        else
            TRIAL_RC=$?
            if [[ "$TRIAL_RC" == "10" ]]; then exec "$TRIAL_RUNTIME/lepton" "$@"; fi
            exit "$TRIAL_RC"
        fi
    else
        exec "$TRIAL_RUNTIME/lepton" "$@"
    fi
fi
'''
    source = source.replace(top_guard_anchor, guard, 1)

    normal_teardown_anchor = "        wait_for_container\n        teardown\n        ;;"
    if source.count(normal_teardown_anchor) != 1:
        raise TrialError("Lepton normal teardown anchor changed; refusing to generate an entrypoint")
    source = source.replace(
        normal_teardown_anchor,
        "        wait_for_container\n        LEPTON_CACHE_TRIAL_NORMAL_TEARDOWN=true\n        teardown\n        ;;",
        1,
    )

    remove_prefix_anchor = "            remove_prefix\n\n            if ! is_steamlaunch; then"
    if source.count(remove_prefix_anchor) != 1:
        raise TrialError("Lepton teardown cleanup anchor changed; refusing to generate an entrypoint")
    source = source.replace(
        remove_prefix_anchor,
        '''            if [[ "${SteamAppId:-}" == "3150751987" ]] &&
                [[ "${STEAM_COMPAT_DATA_PATH:-}" == "/home/steamos/Applications/quest-frame/com.enhanceexperience.tetriseffect/lepton-data" ]] &&
                [[ "${LEPTON_CONTEXT:-}" == "steamlaunch-3150751987" ]]; then
                frameport_lepton_cache_trial_finish_prefix
            else
                remove_prefix
            fi

            if ! is_steamlaunch; then''',
        1,
    )
    return source


def _launcher_preflight_block() -> str:
    return f'''trial_dir="$app_dir/lepton-cache-trial"
trial_stock="$trial_dir/launch-stock.sh"
{_stock_launcher_gate_block()}
trial_helper="$trial_dir/prepare_lepton_cache_trial.py"
runtime_dir="{RUNTIME_DIR}"
if [[ ! -f "$trial_helper" ]]; then
    echo "Lepton cache trial helper missing at $trial_helper; refusing launch." >&2
    exit 1
fi
if python3 "$trial_helper" preflight \\
    --runtime-dir "$runtime_dir" --app-dir "$app_dir" \\
    --install-path "$app_dir/lepton-app" --data-path "$app_dir/lepton-data" \\
    --steam-app-id "{STEAM_APP_ID}" \\
    --keep-context "${{LEPTON_KEEP_CONTEXT:-}}" \\
    --no-cleanup "${{LEPTON_NO_CLEANUP:-false}}"; then
    :
else
    trial_rc=$?
    if [[ "$trial_rc" == "10" ]]; then
        flock -u 9 || true
        exec "$trial_stock" "$@"
    fi
    exit "$trial_rc"
fi
'''


def _stock_launcher_gate_block(expected_hash: str | None = None) -> str:
    """Hash-gate the on-device stock copy before preflight or fallback."""
    if expected_hash is None:
        expected_hash = LAUNCH_SHA256
    return f'''if [[ ! -f "$trial_stock" || -L "$trial_stock" || ! -x "$trial_stock" ]]; then
    echo "Lepton cache trial stock launcher missing, unsafe, or not executable at $trial_stock; refusing launch." >&2
    exit 1
fi
if ! stock_hash_line="$(sha256sum -- "$trial_stock" 2>/dev/null)"; then
    echo "Lepton cache trial could not hash stock launcher $trial_stock; refusing launch." >&2
    exit 1
fi
stock_hash="${{stock_hash_line%% *}}"
if [[ "$stock_hash" != "{expected_hash}" ]]; then
    echo "Lepton cache trial stock launcher hash mismatch at $trial_stock; refusing launch." >&2
    exit 1
fi
'''


def build_trial_dispatch() -> str:
    """Dispatch the existing per-title Steam launcher target to the wrapper."""
    return f'''#!/usr/bin/env bash
set -euo pipefail
# The wrapper owns the lock, watchdog and plays.log start/end records.
app_dir="{APP_DIR}"
trial_dir="$app_dir/lepton-cache-trial"
exec "$trial_dir/launch-cache-trial.sh" "$@"
'''


def build_trial_launcher(source: str) -> str:
    """Create a separate one-title launcher; the stock launcher stays intact."""
    lock_line = 'exec 9>"$app_dir/.launch.lock"; flock -n 9 || { echo "$(date +%s) already starting or running: second launch ignored" >>"$app_dir/launch-dup.log"; exit 0; }\n'
    if source.count(lock_line) != 1:
        raise TrialError("Tetris launcher lock anchor changed")
    source = source.replace(lock_line, lock_line + _launcher_preflight_block(), 1)

    launch_call = "setsid /home/steamos/.local/share/Steam/steamapps/common/Lepton/lepton start"
    if source.count(launch_call) != 1:
        raise TrialError("Tetris launcher Lepton invocation changed")
    source = source.replace(
        launch_call,
        'trial_lepton="$runtime_dir/lepton"\n'
        'if python3 "$trial_helper" write-runtime \\\n'
        '    --runtime-dir "$runtime_dir" --app-dir "$app_dir" \\\n'
        '    --install-path "$app_dir/lepton-app" --data-path "$app_dir/lepton-data" \\\n'
        f'    --steam-app-id "{STEAM_APP_ID}" --output "$trial_dir/lepton"; then\n'
        '    trial_lepton="$trial_dir/lepton"\n'
        'else\n'
        '    trial_rc=$?\n'
        '    if [[ "$trial_rc" != "10" ]]; then exit "$trial_rc"; fi\n'
        'fi\n'
        'export LEPTON_CACHE_TRIAL_RUNTIME_DIR="$runtime_dir"\n'
        'export LEPTON_CACHE_TRIAL_HELPER="$trial_helper"\n'
        'setsid "$trial_lepton" start',
        1,
    )
    return source


def build_bundle(capture_root: Path = CAPTURE_ROOT, launch_path: Path = LAUNCH_CAPTURE) -> dict[str, str]:
    validate_capture(capture_root, launch_path)
    runtime_text = (capture_root / "lepton").read_text(encoding="utf-8")
    launcher_text = launch_path.read_text(encoding="utf-8")
    patched_runtime = patch_runtime_text(runtime_text)
    candidate_launcher = build_trial_launcher(launcher_text)
    candidate_dispatch = build_trial_dispatch()
    return {
        "patched_runtime_sha256": hashlib.sha256(patched_runtime.encode()).hexdigest(),
        "launcher_sha256": hashlib.sha256(candidate_launcher.encode()).hexdigest(),
        "dispatch_sha256": hashlib.sha256(candidate_dispatch.encode()).hexdigest(),
        "trial_identity": trial_identity(),
    }


def write_candidate(output_dir: Path, capture_root: Path = CAPTURE_ROOT, launch_path: Path = LAUNCH_CAPTURE) -> dict[str, str]:
    metadata = build_bundle(capture_root, launch_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    candidate_launcher = build_trial_launcher(launch_path.read_text(encoding="utf-8"))
    helper_destination = output_dir / "prepare_lepton_cache_trial.py"
    shutil.copyfile(Path(__file__).resolve(), helper_destination)
    helper_destination.chmod(0o755)
    launcher_destination = output_dir / "launch-cache-trial.sh"
    launcher_destination.write_text(candidate_launcher, encoding="utf-8")
    launcher_destination.chmod(0o755)
    dispatch_destination = output_dir / "launch-trial-dispatch.sh"
    dispatch_destination.write_text(build_trial_dispatch(), encoding="utf-8")
    dispatch_destination.chmod(0o755)

    manifest = {
        "status": "prototype; not deployed or speed-verified",
        "target": {"steam_app_id": STEAM_APP_ID, "package_id": PACKAGE_ID, "app_dir": str(APP_DIR)},
        "runtime": {"dir": str(RUNTIME_DIR), "version": RUNTIME_VERSION, "source_hashes": SOURCE_HASHES},
        "captured_launcher_sha256": LAUNCH_SHA256,
        **metadata,
    }
    (output_dir / "candidate.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output_dir / "deployment.md").write_text(_deployment_notes(output_dir), encoding="utf-8")
    return manifest


def _deployment_notes(output_dir: Path) -> str:
    return f"""# One-title Lepton cache trial candidate

This is an opt-in prototype for Tetris Effect (`{PACKAGE_ID}`, Steam app `{STEAM_APP_ID}`). It has no speedup claim and is not activated by FramePort. The bundle contains a title-specific dispatch script, Tetris launcher wrapper, and helper; it does not contain the original stock launcher, Lepton runtime source, or binaries.

Before installation, copy the candidate files onto the Frame but do not change the launch route yet. Back up the existing `launch.sh` file type, any symlink text, resolved target path, target bytes, target mode, and hash. Confirm the resolved target is owned only by this Tetris installation; if it is shared or its ownership is unclear, stop. The current launcher must match the audited SHA-256 `{LAUNCH_SHA256}`. The audited launcher is a regular file. If it is a symlink on another matching installation, keep that link unchanged and replace only its verified title-owned target contents with the generated dispatch script.

```sh
app_dir="{APP_DIR}"
trial_dir="$app_dir/lepton-cache-trial"
candidate_dir="/tmp/lepton-cache-trial-candidate"
test -d "$app_dir" && test ! -L "$app_dir"
test -f "$app_dir/launch.sh"
test ! -L "$trial_dir" || {{ echo "trial directory is a symlink; abort" >&2; exit 1; }}
mkdir -p "$trial_dir"
test ! -e "$trial_dir/launch-stock.sh" && test ! -L "$trial_dir/launch-stock.sh" || {{ echo "stock backup already exists; inspect before proceeding" >&2; exit 1; }}
stock_kind=regular
stock_link=""
if test -L "$app_dir/launch.sh"; then
    stock_kind=symlink
    stock_link="$(readlink "$app_dir/launch.sh")"
fi
stock_target="$(readlink -f "$app_dir/launch.sh")"
case "$stock_target" in "$app_dir"/*) ;; *) echo "launch target is not title-local; abort" >&2; exit 1 ;; esac
test "$(stat -c '%h' "$stock_target")" = 1 || {{ echo "launch target has shared hardlinks; abort" >&2; exit 1; }}
stock_mode="$(stat -c '%a' "$stock_target")"
test -x "$stock_target" || {{ echo "stock launcher is not executable; abort" >&2; exit 1; }}
stock_hash="$(sha256sum -- "$stock_target" | cut -d' ' -f1)"
test "$stock_hash" = "{LAUNCH_SHA256}" || {{ echo "stock launcher hash mismatch; abort" >&2; exit 1; }}
cp -p -- "$stock_target" "$trial_dir/launch-stock.sh"
test "$(sha256sum -- "$trial_dir/launch-stock.sh" | cut -d' ' -f1)" = "{LAUNCH_SHA256}"
printf '%s\\n' "$stock_link" >"$trial_dir/launch-link.txt"
printf '%s\\n' "$stock_kind" >"$trial_dir/launch-kind.txt"
printf '%s\\n' "$stock_target" >"$trial_dir/launch-target.txt"
printf '%s\\n' "$stock_mode" >"$trial_dir/launch-mode.txt"
printf '%s\\n' "$stock_hash" >"$trial_dir/launch-stock.sha256"
install -m 755 "$candidate_dir/launch-cache-trial.sh" "$trial_dir/launch-cache-trial.sh"
install -m 755 "$candidate_dir/prepare_lepton_cache_trial.py" "$trial_dir/prepare_lepton_cache_trial.py"
install -m "$stock_mode" "$candidate_dir/launch-trial-dispatch.sh" "$stock_target"
if test "$stock_kind" = symlink; then
    test "$(readlink "$app_dir/launch.sh")" = "$stock_link"
else
    test ! -L "$app_dir/launch.sh"
fi
test "$(readlink -f "$app_dir/launch.sh")" = "$stock_target"
```

The device copy at `launch-stock.sh` is required and hash-checked by the wrapper before any trial preflight or fallback. Do not copy it into the repository or the generated candidate bundle.

After copying `launch-trial-dispatch.sh`, `launch-cache-trial.sh`, and `prepare_lepton_cache_trial.py` into `/tmp/lepton-cache-trial-candidate/` on the Frame, install them using the commands above. Before launching, generate and syntax-check the isolated entrypoint with the target Bash:

```sh
trial_dir="{APP_DIR}/lepton-cache-trial"
python3 "$trial_dir/prepare_lepton_cache_trial.py" write-runtime \\
  --runtime-dir "{RUNTIME_DIR}" --app-dir "{APP_DIR}" \\
  --install-path "{APP_DIR}/lepton-app" --data-path "{APP_DIR}/lepton-data" \\
  --steam-app-id "{STEAM_APP_ID}" --output "$trial_dir/lepton"
/bin/bash -n "$trial_dir/lepton"
```

The supported priming condition for the device trial is to let the cold launch run for more than 30 seconds, then use the controlled Android app-exit path `am force-stop {PACKAGE_ID}`. This is expected to produce an app-exit observer event without sending SIGTERM to Lepton; device confirmation is still required. The audit's package-stop helper signals Lepton and cannot prime the cache. Retention after ordinary Steam Exit is unverified and must not be assumed. The generated entrypoint binds `SCRIPT_DIR` back to `{RUNTIME_DIR}` so it uses the installed libraries.

On an eligible app-exit teardown after 30 seconds, the trial removes the exact stopped container, proves its upper/work paths are no longer mounted, removes only the two overlay workdirs and temporary Lepton prefix, then writes a title/runtime identity marker beside the baked overlays. That retained cache serves one subsequent launch; its teardown clears the trial-owned baked overlays instead of reusing them indefinitely when Lepton does not expose reliable late-crash status. Early shutdown, signals, logcat-dump requests, failed or changed app/depot state, and non-Steam launches use Lepton's original `remove_prefix` path. `LEPTON_KEEP_CONTEXT` and non-default `LEPTON_NO_CLEANUP` settings select the stock launcher.

If runtime hashes change, a marker-owned cache is invalidated only after the exact container is stopped/removed and mounts are absent. Only `baked/data_overlay`, `baked/app_overlay`, their two workdirs, the recorded trial prefix, and the marker can be removed. Internal, external, and shader data are outside those paths. If ownership or unmount state is ambiguous, the helper stops and prints a recovery message instead of starting stock Lepton against possibly retained overlays.

Rollback only after Tetris is closed. Restore the exact bytes from `launch-stock.sh` to the recorded resolved target, restore the numeric mode from `launch-mode.txt`, verify its SHA-256 is `{LAUNCH_SHA256}`, and verify `launch.sh` has its original file type from `launch-kind.txt`, any original link text from `launch-link.txt`, and resolves to `launch-target.txt`. Then run the helper's `invalidate` mode for the exact app/runtime paths to clear only a marker-owned cache. Keep `lepton-data/internal`, `lepton-data/external`, and `lepton-shaders` untouched. No shared Lepton files are edited.

Candidate files were generated under `{output_dir}` from the captured, hash-matched runtime source. Device launch and cold/warm timing remain unverified.

The `build` command requires the private, read-only audit inputs at `{CAPTURE_ROOT}` and `{LAUNCH_CAPTURE}`. Those captures are intentionally not committed; the pytest suite uses minimal synthetic anchor fixtures and can run from a clean checkout.
"""


def _canonical_existing_directory(path_text: str, expected: Path) -> bool:
    try:
        path = Path(path_text)
        if str(path) != str(expected) or path.is_symlink() or not path.is_dir():
            return False
        return path.resolve(strict=True) == expected
    except OSError:
        return False


def _source_mismatch(runtime_dir: Path) -> str | None:
    for relative_path, expected in SOURCE_HASHES.items():
        path = runtime_dir / relative_path
        try:
            actual = sha256_file(path)
        except OSError:
            return f"missing/unreadable runtime source: {path}"
        if actual != expected:
            return f"runtime source hash changed: {relative_path}"
    return None


def _runtime_version_mismatch(runtime_dir: Path) -> str | None:
    match = re.fullmatch(r"compat tool:\s*(.+),\s*rootfs:\s*(.+)", RUNTIME_VERSION)
    if match is None:
        return "pinned Lepton runtime version format is invalid"
    version_paths = (
        (runtime_dir / "version.txt", match.group(1)),
        (runtime_dir / "images/version.txt", match.group(2)),
    )
    mismatches: list[str] = []
    for version_path, expected in version_paths:
        try:
            installed = version_path.read_text(encoding="utf-8").strip()
        except OSError:
            return f"missing/unreadable runtime version: {version_path}"
        if installed != expected:
            mismatches.append(f"{version_path.name}={installed or 'empty'}")
    if mismatches:
        return f"runtime version changed: {', '.join(mismatches)}"
    return None


def _expected_apk_id(runtime_dir: Path, install_path: Path) -> str:
    apks = sorted(install_path.glob("*.apk"))
    if not apks:
        return ""
    # app_metadata.sh binds APK_INFO_EXTRACTOR_PATH relative to LEPTON_DIR,
    # which is the directory containing liblepton.sh, not the entrypoint root.
    extractor = runtime_dir / "liblepton/apk_extractor/bin/apk-info-extractor"
    try:
        result = subprocess.run(
            [str(extractor), "--print-app-id", str(apks[0])],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _paths(app_dir: Path, data_path: Path) -> dict[str, Path]:
    baked = data_path / "baked"
    return {
        "data_path": data_path,
        "baked": baked,
        "data_overlay": baked / "data_overlay",
        "app_overlay": baked / "app_overlay",
        "data_workdir": baked / "data_workdir",
        "app_workdir": baked / "app_workdir",
        "marker": baked / MARKER_NAME,
        "external": data_path / "external",
        "internal": data_path / "internal",
        "shader": app_dir / "lepton-shaders",
    }


def _validate_trial_paths(app_dir_text: str, data_path_text: str, install_path_text: str) -> tuple[Path, Path, Path, dict[str, Path]]:
    if app_dir_text != str(APP_DIR):
        raise UnsafeCacheState(f"expected exact Tetris app path {APP_DIR}; got {app_dir_text!r}")
    if data_path_text != str(APP_DIR / "lepton-data"):
        raise UnsafeCacheState("expected exact Tetris lepton-data path; refusing cache mutation")
    if install_path_text != str(APP_DIR / "lepton-app"):
        raise UnsafeCacheState("expected exact Tetris lepton-app path; refusing cache mutation")
    app_dir = APP_DIR
    data_path = APP_DIR / "lepton-data"
    install_path = APP_DIR / "lepton-app"
    if not _canonical_existing_directory(str(app_dir), APP_DIR):
        raise UnsafeCacheState("Tetris app directory is missing, symlinked, or resolves outside its expected path")
    for directory in (data_path, install_path):
        if directory.exists() and (directory.is_symlink() or not directory.is_dir() or directory.resolve() != directory):
            raise UnsafeCacheState(f"unexpected symlink or non-directory at {directory}")
    paths = _paths(app_dir, data_path)
    if paths["baked"].exists() and (paths["baked"].is_symlink() or not paths["baked"].is_dir()):
        raise UnsafeCacheState(f"unexpected baked path at {paths['baked']}")
    return app_dir, data_path, install_path, paths


def _path_exists(path: Path) -> bool:
    return os.path.lexists(path)


def _validate_overlay_root(path: Path, baked: Path) -> None:
    if not _path_exists(path):
        return
    if path.is_symlink() or not path.is_dir() or path.parent != baked:
        raise UnsafeCacheState(f"refusing to recurse through unexpected cache path {path}")


def _prefix_path_is_safe(prefix_text: str, prefix_root: Path) -> bool:
    if not prefix_text:
        return True
    prefix = Path(prefix_text)
    try:
        root = prefix_root.resolve(strict=True)
        if prefix.parent.resolve(strict=True) != root or not prefix.name.startswith("lepton-context."):
            return False
        if prefix.is_symlink():
            return False
        if not _path_exists(prefix):
            return True
        info = prefix.lstat()
        return stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
    except OSError:
        return False


def _marker_payload(prefix_path: str, state: str) -> dict[str, Any]:
    return {
        "owner": MARKER_OWNER,
        "identity": trial_identity(),
        "state": state,
        "runtime_version": RUNTIME_VERSION,
        "runtime_dir": str(RUNTIME_DIR),
        "runtime_source_hashes": SOURCE_HASHES,
        "steam_app_id": STEAM_APP_ID,
        "package_id": PACKAGE_ID,
        "app_dir": str(APP_DIR),
        "install_path": str(APP_DIR / "lepton-app"),
        "data_path": str(APP_DIR / "lepton-data"),
        "prefix_path": prefix_path,
    }


def _read_owned_marker(marker: Path, prefix_root: Path) -> dict[str, Any] | None:
    if not _path_exists(marker):
        return None
    if marker.is_symlink() or not marker.is_file():
        raise UnsafeCacheState(f"trial ownership marker is not a regular file: {marker}")
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise UnsafeCacheState("trial ownership marker is unreadable or malformed") from error
    required = _marker_payload(str(payload.get("prefix_path", "")), str(payload.get("state", "")))
    if payload != required or payload["state"] not in {"pending", "retained"}:
        raise UnsafeCacheState("trial marker does not match this exact title/runtime identity")
    if not _prefix_path_is_safe(payload["prefix_path"], prefix_root):
        raise UnsafeCacheState("recorded trial prefix is outside its exact temporary path")
    return payload


def _write_marker(marker: Path, payload: dict[str, Any]) -> None:
    if marker.parent.is_symlink() or not marker.parent.is_dir():
        raise UnsafeCacheState(f"baked marker directory is missing or symlinked: {marker.parent}")
    if _path_exists(marker) and (marker.is_symlink() or not marker.is_file()):
        raise UnsafeCacheState(f"refusing to replace unexpected marker path {marker}")
    fd, temporary_name = tempfile.mkstemp(prefix=f"{MARKER_NAME}.", dir=marker.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, marker)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _container_exists(context_name: str) -> bool:
    try:
        result = subprocess.run(
            ["podman", "container", "exists", context_name],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise UnsafeCacheState("cannot query the exact Lepton container with Podman") from error
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise UnsafeCacheState(f"Podman could not confirm container state for {context_name}")


def _release_exact_container(context_name: str) -> None:
    exact = f"lepton-steamlaunch-{STEAM_APP_ID}"
    if context_name != f"steamlaunch-{STEAM_APP_ID}":
        raise UnsafeCacheState(f"refusing to touch non-target Lepton context {context_name!r}")
    if _container_exists(exact):
        try:
            inspect = subprocess.run(
                ["podman", "inspect", "--format", "{{.State.Running}}", exact],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise UnsafeCacheState("cannot inspect the exact Lepton container") from error
        if inspect.returncode != 0:
            raise UnsafeCacheState("cannot determine whether the exact Lepton container is stopped")
        if inspect.stdout.strip().lower() == "true":
            raise UnsafeCacheState(f"exact Lepton container {exact} is still running")
        try:
            removed = subprocess.run(
                ["podman", "rm", "-f", exact],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise UnsafeCacheState("cannot remove the stopped exact Lepton container") from error
        if removed.returncode != 0:
            raise UnsafeCacheState("Podman failed to remove the stopped exact Lepton container")
    if _container_exists(exact):
        raise UnsafeCacheState("the exact Lepton container still exists after removal")


def _mounts_clear(paths: dict[str, Path], mountinfo_path: Path) -> None:
    try:
        mountinfo = mountinfo_path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise UnsafeCacheState("cannot read mountinfo to prove Lepton overlays are unmounted") from error
    checked = ("data_overlay", "app_overlay", "data_workdir", "app_workdir")
    for key in checked:
        path = str(paths[key])
        escaped = path.replace("\\", "\\134").replace(" ", "\\040").replace("\t", "\\011")
        if path in mountinfo or escaped in mountinfo:
            raise UnsafeCacheState(f"Lepton overlay path remains mounted: {path}")


def _remove_tree_without_following_symlinks(path: Path, baked: Path | None = None) -> None:
    if not _path_exists(path):
        return
    if path.is_symlink() or not path.is_dir():
        raise UnsafeCacheState(f"refusing to recursively remove unexpected path {path}")
    if baked is not None and path.parent != baked:
        raise UnsafeCacheState(f"cache path is outside exact baked directory: {path}")

    for current, dirs, files in os.walk(path, topdown=False, followlinks=False):
        for name in files + dirs:
            child = Path(current) / name
            if child.is_symlink():
                continue
            try:
                child.chmod(0o700, follow_symlinks=False)
            except (NotImplementedError, OSError) as error:
                raise UnsafeCacheState(f"cannot safely prepare Lepton cache path {child}") from error
        try:
            Path(current).chmod(0o700)
        except OSError as error:
            raise UnsafeCacheState(f"cannot safely prepare Lepton cache path {current}") from error
    try:
        shutil.rmtree(path)
    except OSError as error:
        raise UnsafeCacheState(f"cannot remove Lepton cache path {path}") from error


def _remove_prefix(prefix_text: str, prefix_root: Path) -> None:
    if not prefix_text:
        return
    if not _prefix_path_is_safe(prefix_text, prefix_root):
        raise UnsafeCacheState("refusing to remove a prefix outside the exact Lepton temporary path")
    prefix = Path(prefix_text)
    if _path_exists(prefix):
        _remove_tree_without_following_symlinks(prefix)


def _invalidate_owned_cache(
    app_dir_text: str,
    data_path_text: str,
    install_path_text: str,
    context_name: str,
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
    prefix_root: Path = Path("/tmp"),
) -> bool:
    app_dir, data_path, _, paths = _validate_trial_paths(app_dir_text, data_path_text, install_path_text)
    marker = _read_owned_marker(paths["marker"], prefix_root)
    if marker is None:
        return False
    _release_exact_container(context_name)
    _mounts_clear(paths, mountinfo_path)
    for key in ("data_overlay", "app_overlay", "data_workdir", "app_workdir"):
        _validate_overlay_root(paths[key], paths["baked"])
    # Remove only Lepton's baked upper/work dirs. rmtree never follows save
    # symlinks; internal/external/shader paths are not passed to it.
    for key in ("data_overlay", "app_overlay", "data_workdir", "app_workdir"):
        _remove_tree_without_following_symlinks(paths[key], paths["baked"])
    _remove_prefix(marker["prefix_path"], prefix_root)
    paths["marker"].unlink(missing_ok=True)
    print("Lepton cache trial: invalidated its marker-owned Tetris overlays before stock fallback.", file=sys.stderr)
    return True


def _recover_owned_cache(
    app_dir_text: str,
    data_path_text: str,
    install_path_text: str,
    context_name: str,
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
    prefix_root: Path = Path("/tmp"),
) -> bool:
    _, _, _, paths = _validate_trial_paths(app_dir_text, data_path_text, install_path_text)
    marker = _read_owned_marker(paths["marker"], prefix_root)
    if marker is None:
        return False
    package_metadata = paths["data_overlay"] / "system/packages.xml"
    try:
        overlay_is_baked = (
            paths["data_overlay"].is_dir()
            and paths["app_overlay"].is_dir()
            and package_metadata.is_file()
            and PACKAGE_ID in package_metadata.read_text(encoding="utf-8", errors="replace")
        )
    except OSError:
        overlay_is_baked = False
    if not overlay_is_baked:
        _invalidate_owned_cache(
            app_dir_text,
            data_path_text,
            install_path_text,
            context_name,
            mountinfo_path,
            prefix_root,
        )
        print("Lepton cache trial: discarded an incomplete retained overlay before a fresh bake.", file=sys.stderr)
        return False
    _release_exact_container(context_name)
    _mounts_clear(paths, mountinfo_path)
    for key in ("data_workdir", "app_workdir"):
        _validate_overlay_root(paths[key], paths["baked"])
        _remove_tree_without_following_symlinks(paths[key], paths["baked"])
    _remove_prefix(marker["prefix_path"], prefix_root)
    marker["state"] = "retained"
    _write_marker(paths["marker"], marker)
    return True


def _preflight(
    runtime_dir_text: str,
    app_dir_text: str,
    install_path_text: str,
    data_path_text: str,
    steam_app_id: str,
    keep_context: str,
    no_cleanup: str,
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
    prefix_root: Path = Path("/tmp"),
) -> bool:
    mismatch: str | None = None
    if runtime_dir_text != str(RUNTIME_DIR):
        mismatch = "runtime directory does not match the pinned Lepton installation"
    elif not _canonical_existing_directory(runtime_dir_text, RUNTIME_DIR):
        mismatch = "pinned Lepton runtime directory is missing, symlinked, or resolves elsewhere"
    elif steam_app_id != STEAM_APP_ID:
        mismatch = "Steam app id is outside the one-title trial"
    elif keep_context or no_cleanup.lower() not in {"", "false"}:
        mismatch = "Lepton cleanup override is active"
    else:
        try:
            _validate_trial_paths(app_dir_text, data_path_text, install_path_text)
        except UnsafeCacheState as error:
            mismatch = str(error)
    if mismatch is None:
        mismatch = _runtime_version_mismatch(RUNTIME_DIR)
    if mismatch is None:
        mismatch = _source_mismatch(RUNTIME_DIR)
    if mismatch is None:
        actual_package = _expected_apk_id(RUNTIME_DIR, APP_DIR / "lepton-app")
        if actual_package != PACKAGE_ID:
            mismatch = f"expected APK package {PACKAGE_ID}; found {actual_package or 'unreadable APK metadata'}"

    if mismatch is not None:
        # If a previous trial retained overlays, stock Lepton's timestamp gate
        # alone could reuse them. Invalidate only an exact trial-owned marker.
        try:
            invalidated = _invalidate_owned_cache(
                str(APP_DIR),
                str(APP_DIR / "lepton-data"),
                str(APP_DIR / "lepton-app"),
                f"steamlaunch-{STEAM_APP_ID}",
                mountinfo_path,
                prefix_root,
            )
        except UnsafeCacheState as error:
            raise UnsafeCacheState(
                "refusing stock fallback because marker-owned Lepton cache could not be safely invalidated: "
                f"{error}. Close Tetris and ensure lepton-steamlaunch-3150751987 is stopped/unmounted, then retry."
            ) from error
        if (
            "runtime source hash changed" in mismatch
            or "missing/unreadable runtime source" in mismatch
            or "missing/unreadable runtime version" in mismatch
        ):
            baked = _paths(APP_DIR, APP_DIR / "lepton-data")["baked"]
            if not invalidated and any(
                _path_exists(baked / name) for name in ("data_overlay", "app_overlay")
            ):
                raise UnsafeCacheState(
                    "Lepton source hashes changed but no valid trial ownership marker proves the baked overlays "
                    "safe for stock fallback. Preserve saves; inspect Tetris baked overlays and restore the pinned "
                    "runtime or clear only verified trial-owned cache paths before retrying."
                )
        print(f"Lepton cache trial: {mismatch}; using installed Lepton.", file=sys.stderr)
        return False

    _recover_owned_cache(
        str(APP_DIR),
        str(APP_DIR / "lepton-data"),
        str(APP_DIR / "lepton-app"),
        f"steamlaunch-{STEAM_APP_ID}",
        mountinfo_path,
        prefix_root,
    )
    return True


def _finish_prefix(
    *,
    runtime_dir_text: str,
    app_dir_text: str,
    install_path_text: str,
    data_path_text: str,
    steam_app_id: str,
    context_name: str,
    package_id: str,
    normal_teardown: bool,
    early_shutdown: bool,
    dump_logcat: str,
    app_or_depot_changed: str,
    app_baked: bool,
    keep_context: str,
    no_cleanup: str,
    prefix_path: str,
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
    prefix_root: Path = Path("/tmp"),
) -> bool:
    app_dir, data_path, _, paths = _validate_trial_paths(app_dir_text, data_path_text, install_path_text)
    if (
        runtime_dir_text != str(RUNTIME_DIR)
        or not _canonical_existing_directory(runtime_dir_text, RUNTIME_DIR)
        or steam_app_id != STEAM_APP_ID
    ):
        raise UnsafeCacheState("normal teardown no longer matches the exact trial runtime/title")
    if context_name != f"steamlaunch-{STEAM_APP_ID}":
        raise UnsafeCacheState("normal teardown context does not match the exact title container")
    for key in ("data_overlay", "app_overlay", "data_workdir", "app_workdir"):
        _validate_overlay_root(paths[key], paths["baked"])

    _release_exact_container(f"steamlaunch-{STEAM_APP_ID}")
    _mounts_clear(paths, mountinfo_path)

    marker = _read_owned_marker(paths["marker"], prefix_root)
    if marker is not None:
        # The retained overlay is a one-launch cache. Once it has served a warm
        # trial launch, return to Lepton's normal teardown behavior instead of
        # reusing it indefinitely across late-crash/unknown-exit cases.
        _invalidate_owned_cache(
            app_dir_text,
            data_path_text,
            install_path_text,
            context_name,
            mountinfo_path,
            prefix_root,
        )
        print("Lepton cache trial: consumed its one warm launch; cleared its owned baked overlays.", file=sys.stderr)
        return False

    source_ok = _runtime_version_mismatch(RUNTIME_DIR) is None and _source_mismatch(RUNTIME_DIR) is None
    eligible = (
        source_ok
        and package_id == PACKAGE_ID
        and normal_teardown
        and not early_shutdown
        and not dump_logcat
        and app_or_depot_changed.lower() == "false"
        and app_baked
        and not keep_context
        and no_cleanup.lower() in {"", "false"}
        and _prefix_path_is_safe(prefix_path, prefix_root)
    )
    if not eligible:
        return False

    if not paths["baked"].is_dir() or paths["baked"].is_symlink():
        raise UnsafeCacheState("baked directory disappeared or became a symlink before cache retention")
    payload = _marker_payload(prefix_path, "pending")
    _write_marker(paths["marker"], payload)
    try:
        for key in ("data_workdir", "app_workdir"):
            _remove_tree_without_following_symlinks(paths[key], paths["baked"])
        _remove_prefix(prefix_path, prefix_root)
        payload["state"] = "retained"
        _write_marker(paths["marker"], payload)
    except BaseException:
        # Leave the pending marker for exact recovery/invalidation on next run.
        raise
    print("Lepton cache trial: retained Tetris baked overlays after a normal 30-second Steam session.", file=sys.stderr)
    return True


def _device_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--app-dir", required=True)
    parser.add_argument("--install-path", required=True)
    parser.add_argument("--data-path", required=True)
    parser.add_argument("--prefix-root", default="/tmp")
    parser.add_argument("--mountinfo", default="/proc/self/mountinfo")


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser(
        "build",
        help="validate private audit captures and write the candidate bundle",
    )
    build_parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    build_parser.add_argument("--capture-root", type=Path, default=CAPTURE_ROOT)
    build_parser.add_argument("--launch", type=Path, default=LAUNCH_CAPTURE)

    preflight_parser = subparsers.add_parser("preflight", help="strict device source/title gate before launch")
    _device_common(preflight_parser)
    preflight_parser.add_argument("--steam-app-id", required=True)
    preflight_parser.add_argument("--keep-context", default="")
    preflight_parser.add_argument("--no-cleanup", default="false")

    write_parser = subparsers.add_parser("write-runtime", help="generate isolated Lepton entrypoint on the Frame")
    _device_common(write_parser)
    write_parser.add_argument("--steam-app-id", required=True)
    write_parser.add_argument("--output", type=Path, required=True)

    finish_parser = subparsers.add_parser("finish-prefix", help="retain or clear exact title cache on teardown")
    _device_common(finish_parser)
    finish_parser.add_argument("--steam-app-id", required=True)
    finish_parser.add_argument("--context", required=True)
    finish_parser.add_argument("--package-id", required=True)
    finish_parser.add_argument("--normal-teardown", required=True)
    finish_parser.add_argument("--early-shutdown", required=True)
    finish_parser.add_argument("--dump-logcat", default="")
    finish_parser.add_argument("--app-or-depot-changed", required=True)
    finish_parser.add_argument("--app-baked", required=True)
    finish_parser.add_argument("--keep-context", default="")
    finish_parser.add_argument("--no-cleanup", default="false")
    finish_parser.add_argument("--prefix-path", default="")

    invalidate_parser = subparsers.add_parser("invalidate", help="invalidate only a marker-owned cache after exit")
    _device_common(invalidate_parser)
    invalidate_parser.add_argument("--context", default=f"steamlaunch-{STEAM_APP_ID}")

    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            manifest = write_candidate(args.output_dir, args.capture_root, args.launch)
            print(json.dumps(manifest, indent=2, sort_keys=True))
            return 0

        mountinfo = Path(args.mountinfo)
        prefix_root = Path(args.prefix_root)
        if args.command == "preflight":
            ok = _preflight(
                args.runtime_dir,
                args.app_dir,
                args.install_path,
                args.data_path,
                args.steam_app_id,
                args.keep_context,
                args.no_cleanup,
                mountinfo,
                prefix_root,
            )
            return 0 if ok else 10
        if args.command == "write-runtime":
            if not _preflight(
                args.runtime_dir,
                args.app_dir,
                args.install_path,
                args.data_path,
                args.steam_app_id,
                "",
                "false",
                mountinfo,
                prefix_root,
            ):
                return 10
            source_path = Path(args.runtime_dir) / "lepton"
            patched = patch_runtime_text(source_path.read_text(encoding="utf-8"))
            output = args.output
            expected_output = APP_DIR / "lepton-cache-trial/lepton"
            if output != expected_output:
                raise UnsafeCacheState(f"generated entrypoint must use the exact title-local path {expected_output}")
            output_parent = output.parent
            if output_parent.is_symlink() or not output_parent.is_dir() or output_parent.resolve() != output_parent:
                raise UnsafeCacheState(f"title-local generated entrypoint directory is missing or symlinked: {output_parent}")
            if _path_exists(output) and (output.is_symlink() or not output.is_file()):
                raise UnsafeCacheState(f"refusing to replace unexpected generated entrypoint path {output}")
            if output.exists() and output.is_file() and output.read_text(encoding="utf-8") == patched:
                return 0
            temporary = output.with_name(f".{output.name}.tmp-{os.getpid()}")
            temporary.write_text(patched, encoding="utf-8")
            temporary.chmod(0o755)
            os.replace(temporary, output)
            return 0
        if args.command == "finish-prefix":
            retained = _finish_prefix(
                runtime_dir_text=args.runtime_dir,
                app_dir_text=args.app_dir,
                install_path_text=args.install_path,
                data_path_text=args.data_path,
                steam_app_id=args.steam_app_id,
                context_name=args.context,
                package_id=args.package_id,
                normal_teardown=args.normal_teardown.lower() == "true",
                early_shutdown=args.early_shutdown.lower() != "0",
                dump_logcat=args.dump_logcat,
                app_or_depot_changed=args.app_or_depot_changed,
                app_baked=args.app_baked.lower() == "true",
                keep_context=args.keep_context,
                no_cleanup=args.no_cleanup,
                prefix_path=args.prefix_path,
                mountinfo_path=mountinfo,
                prefix_root=prefix_root,
            )
            return 0 if retained else 10
        if args.command == "invalidate":
            _invalidate_owned_cache(
                args.app_dir,
                args.data_path,
                args.install_path,
                args.context,
                mountinfo,
                prefix_root,
            )
            return 0
    except (TrialError, OSError, subprocess.SubprocessError) as error:
        print(f"Lepton cache trial: {error}", file=sys.stderr)
        return 20
    return 2


def main() -> int:
    return _cli()


if __name__ == "__main__":
    raise SystemExit(main())
