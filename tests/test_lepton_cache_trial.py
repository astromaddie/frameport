from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts/prepare_lepton_cache_trial.py"
SPEC = importlib.util.spec_from_file_location("prepare_lepton_cache_trial", SCRIPT_PATH)
assert SPEC and SPEC.loader
trial = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = trial
SPEC.loader.exec_module(trial)


def _runtime_source_fixture() -> str:
    return '''#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
source "${SCRIPT_DIR}/liblepton/liblepton.sh"

case "${COMMAND}" in
    start)
        wait_for_container
        teardown
        ;;
esac

function teardown()
{
    if [[ "${EARLY_SHUTDOWN}" == "1" ]]; then
        return
    fi
    if [[ "${APP_OR_DEPOT_CHANGED}" == "true" ]]; then
        return
    fi
            remove_prefix

            if ! is_steamlaunch; then
                return
            fi
}
'''


def _launcher_source_fixture() -> str:
    return '''#!/usr/bin/env bash
app_dir=/home/steamos/Applications/quest-frame/com.enhanceexperience.tetriseffect
exec 9>"$app_dir/.launch.lock"; flock -n 9 || { echo "$(date +%s) already starting or running: second launch ignored" >>"$app_dir/launch-dup.log"; exit 0; }
setsid /home/steamos/.local/share/Steam/steamapps/common/Lepton/lepton start
'''


def _capture_fixture(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    capture = tmp_path / "captured-runtime"
    hashes: dict[str, str] = {}
    for relative in trial.SOURCE_HASHES:
        source = _runtime_source_fixture() if relative == "lepton" else f"fixture:{relative}\n"
        path = capture / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        hashes[relative] = hashlib.sha256(source.encode()).hexdigest()
    (capture / "version.txt").write_text(trial.RUNTIME_VERSION + "\n", encoding="utf-8")
    manifest = "".join(
        f"/runtime/{relative} | {relative} | {len((capture / relative).read_bytes())} | {digest}\n"
        for relative, digest in hashes.items()
    )
    (capture / "manifest.txt").write_text(manifest, encoding="utf-8")
    launch = tmp_path / "launch.sh"
    launch.write_text(_launcher_source_fixture(), encoding="utf-8")
    monkeypatch.setattr(trial, "SOURCE_HASHES", hashes)
    monkeypatch.setattr(trial, "LAUNCH_SHA256", hashlib.sha256(launch.read_bytes()).hexdigest())
    return capture, launch


def _write_runtime_tree(root: Path, *, bad_file: str | None = None) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for relative in trial.SOURCE_HASHES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        value = f"captured:{relative}"
        if relative == bad_file:
            value += ":changed"
        path.write_text(value, encoding="utf-8")
        hashes[relative] = hashlib.sha256(value.encode()).hexdigest()
    (root / "liblepton/apk_extractor/bin").mkdir(parents=True, exist_ok=True)
    return hashes


def _trial_tree(tmp_path: Path, monkeypatch, *, mismatch: bool = False):
    app_dir = tmp_path / "Applications/quest-frame/com.enhanceexperience.tetriseffect"
    runtime_dir = tmp_path / "Lepton"
    data_path = app_dir / "lepton-data"
    install_path = app_dir / "lepton-app"
    app_dir.mkdir(parents=True)
    data_path.mkdir()
    install_path.mkdir()
    (install_path / "game.apk").write_bytes(b"APK")
    hashes = _write_runtime_tree(runtime_dir, bad_file="lepton" if mismatch else None)
    (runtime_dir / "version.txt").write_text("v3.0.5\n", encoding="utf-8")
    (runtime_dir / "images/version.txt").parent.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "images/version.txt").write_text("v3.0.3\n", encoding="utf-8")
    monkeypatch.setattr(trial, "APP_DIR", app_dir)
    monkeypatch.setattr(trial, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(trial, "SOURCE_HASHES", hashes if not mismatch else {**hashes, "lepton": "0" * 64})

    baked = data_path / "baked"
    paths = trial._paths(app_dir, data_path)
    for name in ("data_overlay", "app_overlay", "data_workdir", "app_workdir"):
        paths[name].mkdir(parents=True)
        (paths[name] / "owned.tmp").write_text(name, encoding="utf-8")
    (paths["data_overlay"] / "system").mkdir()
    (paths["data_overlay"] / "system/packages.xml").write_text("com.enhanceexperience.tetriseffect", encoding="utf-8")
    (data_path / "external").mkdir()
    (data_path / "external/save.dat").write_text("external-save", encoding="utf-8")
    (data_path / "internal").mkdir()
    (data_path / "internal/save.dat").write_text("internal-save", encoding="utf-8")
    (app_dir / "lepton-shaders").mkdir()
    (app_dir / "lepton-shaders/cache.bin").write_bytes(b"shader-cache")
    return app_dir, runtime_dir, data_path, install_path, baked, paths


def _finish_args(app_dir, runtime_dir, data_path, install_path, prefix, **overrides):
    args = {
        "runtime_dir_text": str(runtime_dir),
        "app_dir_text": str(app_dir),
        "install_path_text": str(install_path),
        "data_path_text": str(data_path),
        "steam_app_id": trial.STEAM_APP_ID,
        "context_name": f"steamlaunch-{trial.STEAM_APP_ID}",
        "package_id": trial.PACKAGE_ID,
        "normal_teardown": True,
        "early_shutdown": False,
        "dump_logcat": "",
        "app_or_depot_changed": "false",
        "app_baked": True,
        "keep_context": "",
        "no_cleanup": "false",
        "prefix_path": str(prefix),
        "mountinfo_path": Path("/nonexistent-test-mountinfo"),
        "prefix_root": prefix.parent,
    }
    args.update(overrides)
    return args


def test_package_gate_uses_installed_liblepton_extractor_path(tmp_path):
    runtime = tmp_path / "Lepton"
    install = tmp_path / "lepton-app"
    install.mkdir()
    apk = install / "game.apk"
    apk.write_bytes(b"metadata fixture")
    extractor = runtime / "liblepton/apk_extractor/bin/apk-info-extractor"
    extractor.parent.mkdir(parents=True)
    extractor.write_text(
        '#!/bin/sh\n'
        '[ "$1" = --print-app-id ] || exit 8\n'
        '[ -f "$2" ] || exit 9\n'
        'printf "%s\\n" com.enhanceexperience.tetriseffect\n',
        encoding="utf-8",
    )
    extractor.chmod(0o755)
    assert trial._expected_apk_id(runtime, install) == trial.PACKAGE_ID
    extractor.unlink()
    assert trial._expected_apk_id(runtime, install) == ""


def test_capture_hash_gates_refuse_runtime_or_launcher_drift(tmp_path, monkeypatch):
    capture, launch = _capture_fixture(tmp_path, monkeypatch)
    trial.validate_capture(capture, launch)
    original_runtime = (capture / "liblepton/liblepton.sh").read_text(encoding="utf-8")
    with (capture / "liblepton/liblepton.sh").open("a", encoding="utf-8") as changed:
        changed.write("# changed\n")
    with pytest.raises(trial.TrialError, match="captured source hash mismatch"):
        trial.validate_capture(capture, launch)
    (capture / "liblepton/liblepton.sh").write_text(original_runtime, encoding="utf-8")
    launch.write_text(launch.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
    with pytest.raises(trial.TrialError, match="captured Tetris Effect launcher hash mismatch"):
        trial.validate_capture(capture, launch)


@pytest.mark.skipif(
    not trial.CAPTURE_ROOT.is_dir() or not trial.LAUNCH_CAPTURE.is_file(),
    reason="private read-only audit capture is unavailable in this checkout",
)
def test_private_audit_capture_matches_pinned_runtime():
    trial.validate_capture()


def test_dispatch_preserves_wrapper_logging_through_agent_maintenance(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    agent_path = SCRIPT_PATH.parents[1] / "agent/frameport_agent.py"
    spec = importlib.util.spec_from_file_location("frameport_cache_trial_agent", agent_path)
    assert spec and spec.loader
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    anchor = Path(agent.ANCHORS) / trial.PACKAGE_ID
    anchor.mkdir(parents=True)
    launcher = anchor / "launch.sh"
    dispatch = trial.build_trial_dispatch()
    ownership_line = "# The wrapper owns the lock, watchdog and plays.log start/end records.\n"

    # Reproduce the observed prelaunch rewrite through the actual maintenance path.
    launcher.write_text(dispatch.replace(ownership_line, ""), encoding="utf-8")
    launcher.chmod(0o755)
    assert agent.upgrade_launchers() == [trial.PACKAGE_ID]
    start, _ = agent.plays_lines(str(anchor))
    assert launcher.read_text(encoding="utf-8").count(start) == 1

    # Logging belongs to the wrapper; maintenance must not add a second start.
    launcher.write_text(dispatch, encoding="utf-8")
    assert agent.upgrade_launchers() == []
    assert agent.upgrade_launchers() == []
    assert launcher.read_text(encoding="utf-8") == dispatch
    assert launcher.stat().st_mode & 0o777 == 0o755
    assert start not in dispatch


def test_bundle_is_separate_one_title_candidate_without_runtime_copy(tmp_path, monkeypatch):
    capture, launch = _capture_fixture(tmp_path, monkeypatch)
    output = tmp_path / "candidate"
    manifest = trial.write_candidate(output, capture, launch)
    candidate_launcher = output / "launch-cache-trial.sh"
    dispatch = output / "launch-trial-dispatch.sh"
    helper = output / "prepare_lepton_cache_trial.py"

    assert candidate_launcher.is_file() and os.access(candidate_launcher, os.X_OK)
    assert dispatch.is_file() and os.access(dispatch, os.X_OK)
    assert helper.is_file() and os.access(helper, os.X_OK)
    assert not (output / "lepton").exists()
    assert not (output / "launch-stock.sh").exists()
    assert manifest["status"].startswith("prototype")
    launcher_text = candidate_launcher.read_text(encoding="utf-8")
    assert f'app_dir={trial.APP_DIR}' in launcher_text
    assert f'--steam-app-id "{trial.STEAM_APP_ID}"' in launcher_text
    assert 'exec "$trial_stock" "$@"' in launcher_text
    assert 'exec "$app_dir/launch.sh" "$@"' not in launcher_text
    assert 'setsid "$trial_lepton" start' in launcher_text
    assert "write-runtime" in launcher_text
    dispatch_text = dispatch.read_text(encoding="utf-8")
    assert 'exec "$trial_dir/launch-cache-trial.sh" "$@"' in dispatch_text
    notes = (output / "deployment.md").read_text(encoding="utf-8")
    assert "Lepton runtime source" in notes and "binaries" in notes
    assert "exact bytes from `launch-stock.sh`" in notes
    assert "ordinary Steam Exit is unverified" in notes

    subprocess.run(["bash", "-n", str(candidate_launcher)], check=True)
    subprocess.run(["bash", "-n", str(dispatch)], check=True)
    captured = (capture / "lepton").read_text(encoding="utf-8")
    patched = trial.patch_runtime_text(captured)
    assert 'SCRIPT_DIR="${LEPTON_CACHE_TRIAL_RUNTIME_DIR:-' in patched
    assert 'SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )"' not in patched
    assert f'[[ "${{STEAM_COMPAT_DATA_PATH:-}}" == "{trial.APP_DIR}/lepton-data" ]]' in patched
    assert "APP_OR_DEPOT_CHANGED" in patched and '[[ "${EARLY_SHUTDOWN}" == "1" ]]' in captured


def test_patch_requires_exact_lifecycle_anchors(tmp_path, monkeypatch):
    capture, _ = _capture_fixture(tmp_path, monkeypatch)
    source = (capture / "lepton").read_text(encoding="utf-8")
    with pytest.raises(trial.TrialError, match="normal teardown anchor changed"):
        trial.patch_runtime_text(source.replace("        wait_for_container\n", "        wait_container\n", 1))


def test_stock_copy_gate_fails_before_trial_and_fallback_uses_copy(tmp_path, monkeypatch):
    capture, launch = _capture_fixture(tmp_path, monkeypatch)
    output = tmp_path / "candidate"
    trial.write_candidate(output, capture, launch)
    launcher = (output / "launch-cache-trial.sh").read_text(encoding="utf-8")
    assert 'exec "$trial_stock" "$@"' in launcher
    assert 'exec "$app_dir/launch.sh"' not in launcher
    assert launcher.index('if [[ ! -f "$trial_stock"') < launcher.index('if python3 "$trial_helper" preflight')

    app_dir = tmp_path / "title"
    trial_dir = app_dir / "lepton-cache-trial"
    trial_dir.mkdir(parents=True)
    stock = trial_dir / "launch-stock.sh"
    harness = tmp_path / "stock-gate.sh"
    gate = trial._stock_launcher_gate_block(trial.LAUNCH_SHA256)

    def run_gate() -> subprocess.CompletedProcess:
        harness.write_text(
            "#!/bin/bash\nset -euo pipefail\n"
            f'app_dir="{app_dir}"\n'
            'trial_dir="$app_dir/lepton-cache-trial"\n'
            'trial_stock="$trial_dir/launch-stock.sh"\n'
            + gate
            + 'printf "trial-started\\n" >"$trial_dir/trial-ran"\n',
            encoding="utf-8",
        )
        return subprocess.run(["bash", str(harness)], capture_output=True, text=True, check=False)

    missing = run_gate()
    assert missing.returncode != 0
    assert not (trial_dir / "trial-ran").exists()
    assert "stock launcher missing, unsafe, or not executable" in missing.stderr

    stock.write_bytes(launch.read_bytes())
    stock.chmod(0o755)
    valid = run_gate()
    assert valid.returncode == 0
    assert (trial_dir / "trial-ran").is_file()

    # Run the generated production wrapper with its normal preflight fallback
    # result, intercepting exec so the captured stock launcher cannot run.
    source = _launcher_source_fixture().replace(f"app_dir={trial.APP_DIR}", f"app_dir={app_dir}")
    wrapper = trial_dir / "launch-cache-trial.sh"
    wrapper.write_text(trial.build_trial_launcher(source), encoding="utf-8")
    (trial_dir / "prepare_lepton_cache_trial.py").write_text("# test stub\n", encoding="utf-8")
    (app_dir / "launch.sh").write_text("#!/bin/bash\necho dispatch-ran\n", encoding="utf-8")
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    for name, body in {
        "flock": "#!/bin/sh\nexit 0\n",
        "python3": '#!/bin/sh\n[ "$2" = preflight ] && exit 10\nexit 99\n',
    }.items():
        stub = stub_bin / name
        stub.write_text(body, encoding="utf-8")
        stub.chmod(0o755)
    bash_env = tmp_path / "bash-env"
    bash_env.write_text(
        '''exec() {
    case "${1:-}" in
        */lepton-cache-trial/launch-stock.sh)
            printf '%s\\n' "$1" >"$TEST_EXEC_TARGET"
            exit 0
            ;;
        */launch.sh|*/launch-cache-trial.sh)
            printf 'dispatch-recursion\\n' >"$TEST_EXEC_TARGET"
            exit 88
            ;;
        *) builtin exec "$@" ;;
    esac
}
''',
        encoding="utf-8",
    )
    exec_target = tmp_path / "exec-target"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{stub_bin}:{env['PATH']}",
            "BASH_ENV": str(bash_env),
            "TEST_EXEC_TARGET": str(exec_target),
        }
    )
    subprocess.run(["bash", str(wrapper)], env=env, check=True)
    assert exec_target.read_text(encoding="utf-8").strip() == str(stock)

    (trial_dir / "trial-ran").unlink()
    stock.write_bytes(stock.read_bytes() + b"# drift\n")
    drifted = run_gate()
    assert drifted.returncode != 0
    assert not (trial_dir / "trial-ran").exists()
    assert "stock launcher hash mismatch" in drifted.stderr


def test_retention_removes_only_workdirs_after_exact_unmount_and_keeps_saves(tmp_path, monkeypatch):
    app_dir, runtime_dir, data_path, install_path, baked, paths = _trial_tree(tmp_path, monkeypatch)
    prefix_root = tmp_path / "tmp"
    prefix_root.mkdir()
    prefix = prefix_root / "lepton-context.test"
    prefix.mkdir()
    (prefix / "temporary").write_text("remove", encoding="utf-8")
    external = data_path / "external"
    save_link = paths["data_workdir"] / "external-save-link"
    save_link.symlink_to(external / "save.dat")
    (paths["data_overlay"] / "media").mkdir()
    retained_link = paths["data_overlay"] / "media/0"
    retained_link.symlink_to(external)
    order: list[str] = []

    def release(context):
        assert context == f"steamlaunch-{trial.STEAM_APP_ID}"
        order.append("container-removed")

    def mounts_clear(_paths, _mountinfo):
        assert order and order[-1] == "container-removed"
        order.append("mounts-clear")

    monkeypatch.setattr(trial, "_release_exact_container", release)
    monkeypatch.setattr(trial, "_mounts_clear", mounts_clear)
    original_rmtree = trial._remove_tree_without_following_symlinks
    monkeypatch.setattr(
        trial,
        "_remove_tree_without_following_symlinks",
        lambda path, baked_arg=None: (order.append(f"removed:{path.name}"), original_rmtree(path, baked_arg))[1],
    )

    assert trial._finish_prefix(
        **_finish_args(app_dir, runtime_dir, data_path, install_path, prefix),
    )
    assert order[:2] == ["container-removed", "mounts-clear"]
    assert not paths["data_workdir"].exists() and not paths["app_workdir"].exists()
    assert not prefix.exists()
    assert paths["data_overlay"].is_dir() and paths["app_overlay"].is_dir()
    assert (paths["data_overlay"] / "system/packages.xml").is_file()
    assert retained_link.is_symlink()
    assert (external / "save.dat").read_text(encoding="utf-8") == "external-save"
    assert (data_path / "internal/save.dat").read_text(encoding="utf-8") == "internal-save"
    assert (app_dir / "lepton-shaders/cache.bin").read_bytes() == b"shader-cache"
    marker = json.loads(paths["marker"].read_text(encoding="utf-8"))
    assert marker["state"] == "retained"
    assert marker["identity"] == trial.trial_identity()

    for name in ("data_workdir", "app_workdir"):
        paths[name].mkdir()
    next_prefix = prefix_root / "lepton-context.warm"
    next_prefix.mkdir()
    order.clear()
    assert not trial._finish_prefix(
        **_finish_args(app_dir, runtime_dir, data_path, install_path, next_prefix),
    )
    assert not paths["data_overlay"].exists() and not paths["app_overlay"].exists()
    assert not paths["marker"].exists() and next_prefix.is_dir()
    trial._remove_prefix(str(next_prefix), prefix_root)  # adapter then uses Lepton's original remove_prefix
    assert not next_prefix.exists()
    assert (external / "save.dat").read_text(encoding="utf-8") == "external-save"
    assert (data_path / "internal/save.dat").read_text(encoding="utf-8") == "internal-save"
    assert (app_dir / "lepton-shaders/cache.bin").read_bytes() == b"shader-cache"


@pytest.mark.parametrize(
    "overrides",
    [
        {"normal_teardown": False},
        {"early_shutdown": True},
        {"dump_logcat": "1"},
        {"app_or_depot_changed": "true"},
        {"app_baked": False},
        {"package_id": "different.package"},
        {"keep_context": "1"},
        {"no_cleanup": "true"},
    ],
    ids=["signal-or-error", "under-30-seconds", "logcat-dump", "apk-or-depot-change", "not-baked", "wrong-apk", "keep-context", "no-cleanup"],
)
def test_non_eligible_teardown_preserves_original_prefix_clear_decision(tmp_path, monkeypatch, overrides):
    app_dir, runtime_dir, data_path, install_path, _, paths = _trial_tree(tmp_path, monkeypatch)
    prefix_root = tmp_path / "tmp"
    prefix_root.mkdir()
    prefix = prefix_root / "lepton-context.test"
    prefix.mkdir()
    monkeypatch.setattr(trial, "_release_exact_container", lambda context: None)
    monkeypatch.setattr(trial, "_mounts_clear", lambda paths, mountinfo: None)

    retained = trial._finish_prefix(
        **_finish_args(app_dir, runtime_dir, data_path, install_path, prefix, **overrides),
    )
    assert not retained
    assert paths["data_overlay"].is_dir()
    assert paths["app_overlay"].is_dir()
    assert paths["data_workdir"].is_dir()
    assert paths["app_workdir"].is_dir()
    assert prefix.is_dir()
    assert not paths["marker"].exists()


def test_shell_teardown_adapter_runs_original_remove_prefix_on_skip_and_retains_only_on_success(tmp_path):
    harness = tmp_path / "adapter-test.sh"
    function_source = trial._teardown_function_source()
    harness.write_text(
        "#!/bin/bash\nset -euo pipefail\n"
        + function_source
        + '''
is_app_baked() { return 0; }
get_app_id() { echo com.enhanceexperience.tetriseffect; }
println() { :; }
remove_prefix() {
    echo called >>"$TEST_TRACE"
    rm -rf -- "$TEST_DATA_OVERLAY" "$TEST_APP_OVERLAY" "$TEST_DATA_WORKDIR" "$TEST_APP_WORKDIR"
}
python3() { printf '%s\\n' "$@" >"$TEST_ARGS"; return "$TEST_FINISH_RC"; }
frameport_lepton_cache_trial_finish_prefix
''',
        encoding="utf-8",
    )
    subprocess.run(["bash", "-n", str(harness)], check=True)
    fixture = tmp_path / "baked"
    for name in ("data_overlay", "app_overlay", "data_workdir", "app_workdir"):
        (fixture / name).mkdir(parents=True)
    data_overlay, app_overlay, data_workdir, app_workdir = (fixture / name for name in ("data_overlay", "app_overlay", "data_workdir", "app_workdir"))

    env = os.environ.copy()
    env.update(
        {
            "SteamAppId": trial.STEAM_APP_ID,
            "LEPTON_CONTEXT": f"steamlaunch-{trial.STEAM_APP_ID}",
            "STEAM_COMPAT_INSTALL_PATH": str(trial.APP_DIR / "lepton-app"),
            "STEAM_COMPAT_DATA_PATH": str(trial.APP_DIR / "lepton-data"),
            "LEPTON_CACHE_TRIAL_NORMAL_TEARDOWN": "false",
            "EARLY_SHUTDOWN": "1",
            "APP_OR_DEPOT_CHANGED": "true",
            "LEPTON_NO_CLEANUP": "false",
            "LEPTON_DUMP_LOGCAT": "",
            "LEPTON_PREFIX": "",
            "TEST_FINISH_RC": "10",
            "TEST_TRACE": str(tmp_path / "trace"),
            "TEST_ARGS": str(tmp_path / "args"),
            "TEST_DATA_OVERLAY": str(data_overlay),
            "TEST_APP_OVERLAY": str(app_overlay),
            "TEST_DATA_WORKDIR": str(data_workdir),
            "TEST_APP_WORKDIR": str(app_workdir),
        }
    )
    subprocess.run(["bash", str(harness)], env=env, check=True)
    assert not data_overlay.exists() and not app_overlay.exists()
    assert not data_workdir.exists() and not app_workdir.exists()
    args = Path(env["TEST_ARGS"]).read_text(encoding="utf-8")
    assert "--early-shutdown\n1" in args
    assert "--app-or-depot-changed\ntrue" in args


def test_source_mismatch_invalidates_only_marked_cache_or_fails_closed(tmp_path, monkeypatch):
    app_dir, runtime_dir, data_path, install_path, baked, paths = _trial_tree(tmp_path, monkeypatch, mismatch=True)
    prefix_root = tmp_path / "tmp"
    prefix_root.mkdir()
    prefix = prefix_root / "lepton-context.interrupted"
    prefix.mkdir()
    (prefix / "leftover").write_text("temporary", encoding="utf-8")
    external = data_path / "external"
    symlink = paths["data_overlay"] / "external-save-link"
    symlink.symlink_to(external / "save.dat")
    trial._write_marker(paths["marker"], trial._marker_payload(str(prefix), "pending"))
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text("no matching Lepton overlay mounts\n", encoding="utf-8")
    released: list[str] = []
    monkeypatch.setattr(trial, "_release_exact_container", lambda context: released.append(context))

    assert not trial._preflight(
        str(runtime_dir),
        str(app_dir),
        str(install_path),
        str(data_path),
        trial.STEAM_APP_ID,
        "",
        "false",
        mountinfo,
        prefix_root,
    )
    assert released == [f"steamlaunch-{trial.STEAM_APP_ID}"]
    assert not paths["data_overlay"].exists() and not paths["app_overlay"].exists()
    assert not paths["data_workdir"].exists() and not paths["app_workdir"].exists()
    assert not paths["marker"].exists() and not prefix.exists()
    assert (external / "save.dat").read_text(encoding="utf-8") == "external-save"
    assert (data_path / "internal/save.dat").read_text(encoding="utf-8") == "internal-save"
    assert (app_dir / "lepton-shaders/cache.bin").read_bytes() == b"shader-cache"


def test_incomplete_marker_owned_overlay_is_cleared_before_fresh_bake(tmp_path, monkeypatch):
    app_dir, _, data_path, install_path, _, paths = _trial_tree(tmp_path, monkeypatch)
    prefix_root = tmp_path / "tmp"
    prefix_root.mkdir()
    prefix = prefix_root / "lepton-context.interrupted"
    prefix.mkdir()
    trial._write_marker(paths["marker"], trial._marker_payload(str(prefix), "retained"))
    (paths["data_overlay"] / "system/packages.xml").unlink()
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text("", encoding="utf-8")
    monkeypatch.setattr(trial, "_release_exact_container", lambda _context: None)
    monkeypatch.setattr(trial, "_mounts_clear", lambda _paths, _mountinfo: None)

    assert not trial._recover_owned_cache(
        str(app_dir),
        str(data_path),
        str(install_path),
        f"steamlaunch-{trial.STEAM_APP_ID}",
        mountinfo,
        prefix_root,
    )
    assert not paths["data_overlay"].exists() and not paths["app_overlay"].exists()
    assert not paths["data_workdir"].exists() and not paths["app_workdir"].exists()
    assert not paths["marker"].exists() and not prefix.exists()
    assert (data_path / "external/save.dat").read_text(encoding="utf-8") == "external-save"
    assert (data_path / "internal/save.dat").read_text(encoding="utf-8") == "internal-save"
    assert (app_dir / "lepton-shaders/cache.bin").read_bytes() == b"shader-cache"


def test_source_mismatch_with_ambiguous_marker_stops_before_cache_mutation(tmp_path, monkeypatch):
    app_dir, runtime_dir, data_path, install_path, _, paths = _trial_tree(tmp_path, monkeypatch, mismatch=True)
    prefix_root = tmp_path / "tmp"
    prefix_root.mkdir()
    paths["marker"].write_text("not a trial marker", encoding="utf-8")
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text("", encoding="utf-8")
    monkeypatch.setattr(trial, "_release_exact_container", lambda context: pytest.fail("must fail before container mutation"))

    with pytest.raises(trial.UnsafeCacheState, match="could not be safely invalidated"):
        trial._preflight(
            str(runtime_dir),
            str(app_dir),
            str(install_path),
            str(data_path),
            trial.STEAM_APP_ID,
            "",
            "false",
            mountinfo,
            prefix_root,
        )
    assert paths["data_overlay"].is_dir() and paths["app_overlay"].is_dir()


def test_source_mismatch_without_marker_fails_closed_when_baked_overlays_exist(tmp_path, monkeypatch):
    app_dir, runtime_dir, data_path, install_path, _, paths = _trial_tree(tmp_path, monkeypatch, mismatch=True)
    prefix_root = tmp_path / "tmp"
    prefix_root.mkdir()
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text("", encoding="utf-8")
    monkeypatch.setattr(trial, "_release_exact_container", lambda context: pytest.fail("no marker means no cache mutation"))

    with pytest.raises(trial.UnsafeCacheState, match="no valid trial ownership marker"):
        trial._preflight(
            str(runtime_dir),
            str(app_dir),
            str(install_path),
            str(data_path),
            trial.STEAM_APP_ID,
            "",
            "false",
            mountinfo,
            prefix_root,
        )
    assert paths["data_overlay"].is_dir() and paths["app_overlay"].is_dir()


def test_runtime_version_change_falls_back_to_stock_version_invalidation(tmp_path, monkeypatch):
    app_dir, runtime_dir, data_path, install_path, _, paths = _trial_tree(tmp_path, monkeypatch)
    (runtime_dir / "version.txt").write_text("v3.0.6\n", encoding="utf-8")
    (runtime_dir / "images/version.txt").write_text("v3.0.4\n", encoding="utf-8")
    prefix_root = tmp_path / "tmp"
    prefix_root.mkdir()
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text("", encoding="utf-8")

    assert not trial._preflight(
        str(runtime_dir),
        str(app_dir),
        str(install_path),
        str(data_path),
        trial.STEAM_APP_ID,
        "",
        "false",
        mountinfo,
        prefix_root,
    )
    assert paths["data_overlay"].is_dir() and paths["app_overlay"].is_dir()


def test_write_runtime_rejects_output_outside_title_trial_directory(tmp_path, monkeypatch):
    app_dir, runtime_dir, data_path, install_path, _, paths = _trial_tree(tmp_path, monkeypatch)
    trial_dir = app_dir / "lepton-cache-trial"
    trial_dir.mkdir()
    real_hashes = {}
    for relative in trial.SOURCE_HASHES:
        destination = runtime_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        content = _runtime_source_fixture() if relative == "lepton" else f"fixture:{relative}\n"
        destination.write_text(content, encoding="utf-8")
        real_hashes[relative] = hashlib.sha256(content.encode()).hexdigest()
    monkeypatch.setattr(trial, "SOURCE_HASHES", real_hashes)
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text("", encoding="utf-8")
    monkeypatch.setattr(trial, "_expected_apk_id", lambda *_args: trial.PACKAGE_ID)
    outside = tmp_path / "shared-Lepton-entrypoint"

    rc = trial._cli(
        [
            "write-runtime",
            "--runtime-dir",
            str(runtime_dir),
            "--app-dir",
            str(app_dir),
            "--install-path",
            str(install_path),
            "--data-path",
            str(data_path),
            "--steam-app-id",
            trial.STEAM_APP_ID,
            "--output",
            str(outside),
            "--mountinfo",
            str(mountinfo),
            "--prefix-root",
            str(tmp_path / "tmp"),
        ]
    )
    assert rc == 20
    assert not outside.exists()
    assert paths["data_overlay"].is_dir()


def test_container_release_removes_only_the_stopped_exact_title_container(monkeypatch):
    calls: list[list[str]] = []
    replies = [(0, ""), (0, "false"), (0, ""), (1, "")]

    def run(command, **_kwargs):
        calls.append(command)
        code, output = replies.pop(0)
        return subprocess.CompletedProcess(command, code, stdout=output, stderr="")

    monkeypatch.setattr(trial.subprocess, "run", run)
    trial._release_exact_container(f"steamlaunch-{trial.STEAM_APP_ID}")
    assert calls == [
        ["podman", "container", "exists", f"lepton-steamlaunch-{trial.STEAM_APP_ID}"],
        ["podman", "inspect", "--format", "{{.State.Running}}", f"lepton-steamlaunch-{trial.STEAM_APP_ID}"],
        ["podman", "rm", "-f", f"lepton-steamlaunch-{trial.STEAM_APP_ID}"],
        ["podman", "container", "exists", f"lepton-steamlaunch-{trial.STEAM_APP_ID}"],
    ]


def test_running_container_or_mount_reference_blocks_cache_mutation(tmp_path, monkeypatch):
    app_dir, runtime_dir, data_path, install_path, _, paths = _trial_tree(tmp_path, monkeypatch)
    original_release = trial._release_exact_container
    monkeypatch.setattr(trial, "_release_exact_container", lambda _context: None)
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(f"overlay {paths['data_overlay']} mounted\n", encoding="utf-8")
    with pytest.raises(trial.UnsafeCacheState, match="remains mounted"):
        trial._mounts_clear(paths, mountinfo)
    with pytest.raises(trial.UnsafeCacheState, match="non-target Lepton context"):
        original_release("steamlaunch-elsewhere")
