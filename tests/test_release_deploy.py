"""TDD tests for atomic multi-image release deployment on the SSH host."""

import os
import signal
import stat
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "release_deploy.sh"


def write_exec(path: Path, body: str):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def mock_docker(path: Path, log: Path, *, compose_rc=0, fail_pull=False, fail_tag=False):
    write_exec(
        path,
        f'''#!/bin/bash
echo "$@" >> "{log}"
if [ "$1" = compose ] && [[ " $* " == *" config --images "* ]]; then
  printf 'frontend:%s\\nbackend:%s\\n' "$D3_RELEASE_TAG" "$D3_RELEASE_TAG"
  exit 0
fi
if [ "$1" = image ] && [ "$2" = inspect ]; then exit {1 if fail_pull else 0}; fi
if [ "$1" = pull ] && {'true' if fail_pull else 'false'}; then exit 1; fi
if [ "$1" = tag ] && {'true' if fail_tag else 'false'}; then exit 1; fi
if [ "$1" = compose ]; then exit {compose_rc}; fi
exit 0
''',
    )


def mock_curl(path: Path, status: str):
    write_exec(path, f"#!/bin/bash\nprintf '%s' '{status}'\n")


def manifest(path: Path, sha="abc123456789"):
    path.write_text(
        "D3_RELEASE_MANIFEST=1\n"
        "image\tfrontend\tfrontend\n"
        "image\tbackend\tbackend\n"
        "probe\thttp://localhost/frontend\t200\n"
        "probe\thttp://localhost/api/health\t200\n"
    )


def base(tmp_path, *, status="200", compose_rc=0, fail_pull=False, fail_tag=False):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "docker.log"
    mock_docker(bindir / "docker", log, compose_rc=compose_rc, fail_pull=fail_pull, fail_tag=fail_tag)
    mock_curl(bindir / "curl", status)
    deploy = tmp_path / "deploy"
    deploy.mkdir(exist_ok=True)
    (deploy / "compose.yml").write_text("services: {}\n")
    mf = tmp_path / "release.manifest"
    manifest(mf)
    env = dict(os.environ)
    env.update(
        RELEASE_MANIFEST=str(mf),
        D3_RELEASE_TAG="abc123456789",
        ACR_REGISTRY="registry",
        ACR_NAMESPACE="ns",
        DEPLOY_DIR=str(deploy),
        STATE_DIR=str(tmp_path / "state"),
        HOST_LOCK=str(tmp_path / "host.lock"),
        DOCKER_BIN=str(bindir / "docker"),
        CURL_BIN=str(bindir / "curl"),
        DOCKER_LOG=str(log),
        HEALTHCHECK_WARMUP="0",
        HEALTHCHECK_INTERVAL="0",
        HEALTHCHECK_RETRIES="1",
        HEALTHCHECK_TIMEOUT="1",
        PULL_RETRIES="1",
        PULL_RETRY_DELAY="0",
    )
    return env, log


def run(env):
    return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)


def test_group_deploy_retags_sha_and_compose_once(tmp_path):
    env, log = base(tmp_path)
    result = run(env)
    assert result.returncode == 0, result.stdout + result.stderr
    lines = log.read_text().splitlines()
    assert lines.count("compose --env-file " + str(Path(env["DEPLOY_DIR"]) / ".d3-release.env") + " up -d") == 1
    assert any("tag registry/ns/frontend:abc123456789 frontend:abc123456789" in line for line in lines)
    assert not any(":latest" in line for line in lines)
    assert (Path(env["STATE_DIR"]) / "last_good_manifest").exists()


def test_compose_preserves_existing_dotenv_and_overlays_release_tag(tmp_path):
    env, log = base(tmp_path)
    deploy = Path(env["DEPLOY_DIR"])
    (deploy / ".env").write_text("COMPOSE_PROJECT_NAME=existing\n")
    result = run(env)
    assert result.returncode == 0, result.stdout + result.stderr
    expected = (
        f"compose --env-file {deploy / '.env'} --env-file {deploy / '.d3-release.env'} up -d"
    )
    assert expected in log.read_text()
    assert (deploy / ".env").read_text() == "COMPOSE_PROJECT_NAME=existing\n"


def test_compose_identity_gate_rejects_latest_and_preserves_last_good(tmp_path):
    env, log = base(tmp_path)
    assert run(env).returncode == 0
    before = (Path(env["STATE_DIR"]) / "last_good_release").read_text()
    log.write_text("")
    write_exec(
        Path(env["DOCKER_BIN"]),
        f'''#!/bin/bash
echo "$@" >> "{log}"
if [ "$1" = compose ] && [[ " $* " == *" config --images "* ]]; then
  printf 'frontend:latest\\nbackend:latest\\n'
  exit 0
fi
exit 0
''',
    )
    env["D3_RELEASE_TAG"] = "def567890123"
    result = run(env)
    assert result.returncode != 0
    assert "compose up" not in log.read_text()
    assert (Path(env["STATE_DIR"]) / "last_good_release").read_text() == before


def test_compose_identity_gate_allows_extra_public_images(tmp_path):
    env, log = base(tmp_path)
    write_exec(
        Path(env["DOCKER_BIN"]),
        f'''#!/bin/bash
echo "$@" >> "{log}"
if [ "$1" = compose ] && [[ " $* " == *" config --images "* ]]; then
  printf 'frontend:%s\\nbackend:%s\\nnginx:1.27\\n' "$D3_RELEASE_TAG" "$D3_RELEASE_TAG"
fi
exit 0
''',
    )
    result = run(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "compose --env-file" in log.read_text()


def test_compose_config_and_up_run_from_deploy_dir(tmp_path):
    env, log = base(tmp_path)
    cwd_log = tmp_path / "cwd.log"
    env["CWD_LOG"] = str(cwd_log)
    write_exec(
        Path(env["DOCKER_BIN"]),
        f'''#!/bin/bash
echo "$@" >> "{log}"
if [ "$1" = compose ]; then printf '%s\\n' "$PWD" >> "{cwd_log}"; fi
if [ "$1" = compose ] && [[ " $* " == *" config --images "* ]]; then
  printf 'frontend:%s\\nbackend:%s\\n' "$D3_RELEASE_TAG" "$D3_RELEASE_TAG"
fi
exit 0
''',
    )
    result = run(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert cwd_log.read_text().splitlines() == [env["DEPLOY_DIR"], env["DEPLOY_DIR"]]


def test_remote_cleanup_deletes_only_exact_three_segment_paths(tmp_path):
    env, _ = base(tmp_path)
    nonce = f"{os.getpid()}-1-7"
    remote_script = Path("/tmp") / f"d3-release-{nonce}.sh"
    remote_manifest = Path("/tmp") / f"d3-release-{nonce}.manifest"
    nonmatching = Path("/tmp") / f"d3-release-{os.getpid()}-1.sh"
    remote_script.write_text("temporary")
    remote_manifest.write_text(Path(env["RELEASE_MANIFEST"]).read_text())
    nonmatching.write_text("keep")
    env.update(
        RELEASE_MANIFEST=str(remote_manifest),
        RELEASE_TEMP_SCRIPT=str(remote_script),
        BUSY_LOCK_FILE=str(tmp_path / "busy.lock"),
        BUSY_LOCK_TIMEOUT="invalid",
    )
    result = run(env)
    assert result.returncode == 1
    assert not remote_script.exists()
    assert not remote_manifest.exists()
    assert nonmatching.exists()
    nonmatching.unlink()


def test_probe_failure_rolls_back_entire_group_and_preserves_good(tmp_path):
    env, log = base(tmp_path)
    assert run(env).returncode == 0
    env["D3_RELEASE_TAG"] = "def567890123"
    env["RELEASE_MANIFEST"] = str(Path(env["RELEASE_MANIFEST"]))
    mock_curl(Path(env["CURL_BIN"]), "500")
    log.write_text("")
    result = run(env)
    assert result.returncode != 0
    lines = log.read_text()
    assert "compose" in lines
    assert "abc123456789" in lines
    assert "def567890123" in lines
    assert (Path(env["STATE_DIR"]) / "last_good_sha").read_text().strip() == "abc123456789"


def test_first_release_probe_failure_has_explicit_no_rollback(tmp_path):
    env, _ = base(tmp_path, status="500")
    result = run(env)
    assert result.returncode != 0
    assert "no previous" in (result.stdout + result.stderr).lower()
    assert not (Path(env["STATE_DIR"]) / "last_good_sha").exists()


def test_pull_or_compose_failure_never_runs_compose_for_partial_group(tmp_path):
    env, log = base(tmp_path, fail_pull=True)
    result = run(env)
    assert result.returncode != 0
    assert not log.exists() or "compose" not in log.read_text()

    env, log = base(tmp_path, compose_rc=1)
    result = run(env)
    assert result.returncode != 0
    assert "compose" in log.read_text()
    assert not (Path(env["STATE_DIR"]) / "last_good_sha").exists()


def test_retag_failure_never_runs_compose(tmp_path):
    env, log = base(tmp_path, fail_tag=True)
    result = run(env)
    assert result.returncode != 0
    assert "compose" not in log.read_text()


def test_remote_manifest_rejects_zero_probes_defense_in_depth(tmp_path):
    env, log = base(tmp_path)
    Path(env["RELEASE_MANIFEST"]).write_text(
        "D3_RELEASE_MANIFEST=1\nimage\tfrontend\tfrontend\n"
    )
    result = run(env)
    assert result.returncode != 0
    assert "probe" in (result.stdout + result.stderr).lower()
    assert not log.exists() or "compose" not in log.read_text()


def test_remote_manifest_rejects_duplicate_or_full_registry_ref(tmp_path):
    env, log = base(tmp_path)
    Path(env["RELEASE_MANIFEST"]).write_text(
        "D3_RELEASE_MANIFEST=1\n"
        "image\tfrontend\tfrontend\n"
        "image\tfrontend\tfrontend\n"
        "probe\thttp://localhost/health\t200\n"
    )
    result = run(env)
    assert result.returncode != 0
    assert "compose" not in log.read_text() if log.exists() else True

    Path(env["RELEASE_MANIFEST"]).write_text(
        "D3_RELEASE_MANIFEST=1\n"
        "image\tfrontend\tregistry/ns/frontend\n"
        "probe\thttp://localhost/health\t200\n"
    )
    result = run(env)
    assert result.returncode != 0


def test_pull_exhausted_exact_sha_local_fallback(tmp_path):
    env, log = base(tmp_path)
    write_exec(
        Path(env["DOCKER_BIN"]),
        f'''#!/bin/bash
echo "$@" >> "{log}"
if [ "$1" = pull ]; then exit 1; fi
if [ "$1" = image ] && [ "$2" = inspect ]; then exit 0; fi
if [ "$1" = compose ] && [[ " $* " == *" config --images "* ]]; then
  printf 'frontend:%s\\nbackend:%s\\n' "$D3_RELEASE_TAG" "$D3_RELEASE_TAG"
fi
exit 0
''',
    )
    env["PULL_RETRIES"] = "1"
    env["PULL_RETRY_DELAY"] = "0"
    result = run(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "already local" in result.stdout


def _lock_holder(path: Path, mode: str = "-x", seconds: str = "1"):
    return subprocess.Popen(
        ["bash", "-c", f'exec 8>"$1"; flock {mode} 8; sleep "$2"', "holder", str(path), seconds],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_busy_lock_timeout_defers_without_compose_or_last_good(tmp_path):
    env, log = base(tmp_path)
    busy = tmp_path / "busy.lock"
    holder = _lock_holder(busy, seconds="2")
    time.sleep(0.05)
    env.update(BUSY_LOCK_FILE=str(busy), BUSY_LOCK_TIMEOUT="1")
    result = run(env)
    holder.wait(timeout=3)
    assert result.returncode == 3
    assert "deferred" in (result.stdout + result.stderr).lower()
    assert not (Path(env["STATE_DIR"]) / "last_good_release").exists()
    assert not log.exists() or "compose" not in log.read_text()


def test_busy_lock_release_within_budget_allows_deploy(tmp_path):
    env, log = base(tmp_path)
    busy = tmp_path / "busy.lock"
    holder = _lock_holder(busy, seconds="0.2")
    env.update(BUSY_LOCK_FILE=str(busy), BUSY_LOCK_TIMEOUT="2")
    result = run(env)
    holder.wait(timeout=3)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "compose" in log.read_text()


def test_busy_lock_missing_file_warns_and_creates(tmp_path):
    env, _ = base(tmp_path)
    busy = tmp_path / "missing" / "busy.lock"
    env.update(BUSY_LOCK_FILE=str(busy), BUSY_LOCK_TIMEOUT="2")
    result = run(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert busy.exists()
    assert "WARN" in result.stderr


def test_busy_lock_invalid_timeout_is_configuration_failure(tmp_path):
    env, _ = base(tmp_path)
    env.update(BUSY_LOCK_FILE=str(tmp_path / "busy.lock"), BUSY_LOCK_TIMEOUT="nope")
    result = run(env)
    assert result.returncode == 1


def test_busy_gate_host_contention_releases_admission_fd(tmp_path):
    env, _ = base(tmp_path)
    busy = tmp_path / "busy.lock"
    host_holder = _lock_holder(Path(env["HOST_LOCK"]), seconds="2")
    time.sleep(0.05)
    env.update(BUSY_LOCK_FILE=str(busy), BUSY_LOCK_TIMEOUT="1")
    result = run(env)
    host_holder.wait(timeout=3)
    assert result.returncode == 3
    probe = subprocess.run(["flock", "-n", str(busy), "-c", "true"])
    assert probe.returncode == 0, "busy admission lock leaked while host was contended"


def test_busy_shared_service_lock_defers_before_compose(tmp_path):
    env, log = base(tmp_path)
    busy = tmp_path / "busy.lock"
    holder = _lock_holder(busy, mode="-s", seconds="2")
    time.sleep(0.05)
    env.update(BUSY_LOCK_FILE=str(busy), BUSY_LOCK_TIMEOUT="1")
    result = run(env)
    holder.wait(timeout=3)
    assert result.returncode == 3
    assert log.read_text().count("pull ") == 2
    assert "compose" not in log.read_text()
    assert not (Path(env["DEPLOY_DIR"]) / ".d3-release.env").exists()
    assert not (Path(env["STATE_DIR"]) / "last_good_release").exists()


def test_busy_lock_release_does_not_repeat_staged_pulls(tmp_path):
    env, log = base(tmp_path)
    busy = tmp_path / "busy.lock"
    holder = _lock_holder(busy, seconds="0.2")
    time.sleep(0.05)
    env.update(BUSY_LOCK_FILE=str(busy), BUSY_LOCK_TIMEOUT="2")
    result = run(env)
    holder.wait(timeout=3)
    assert result.returncode == 0, result.stdout + result.stderr
    assert log.read_text().count("pull ") == 2


def test_term_while_busy_lock_waiting_does_not_switch_release(tmp_path):
    env, log = base(tmp_path)
    busy = tmp_path / "busy.lock"
    holder = _lock_holder(busy, seconds="2")
    time.sleep(0.05)
    env.update(BUSY_LOCK_FILE=str(busy), BUSY_LOCK_TIMEOUT="2")
    started = time.time()
    proc = subprocess.Popen(["bash", str(SCRIPT)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(0.2)
    proc.send_signal(signal.SIGTERM)
    stdout, stderr = proc.communicate(timeout=4)
    holder.wait(timeout=3)
    assert proc.returncode != 0, stdout + stderr
    assert time.time() - started < 4
    assert "compose" not in log.read_text()
    assert not (Path(env["STATE_DIR"]) / "last_good_release").exists()


def test_rollback_pull_failure_preserves_atomic_previous_release(tmp_path):
    env, log = base(tmp_path)
    assert run(env).returncode == 0
    state = Path(env["STATE_DIR"])
    before_release = (state / "last_good_release").read_text()

    # New release pulls two images, then rollback's first pull fails.  The
    # previous manifest/SHA must remain byte-for-byte untouched.
    count_file = Path(env["DOCKER_LOG"] + ".count")
    write_exec(
        Path(env["DOCKER_BIN"]),
        f'''#!/bin/bash
echo "$@" >> "{log}"
if [ "$1" = pull ]; then
  n=$(cat "{count_file}" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "{count_file}"
  [ "$n" -ge 3 ] && exit 1
fi
if [ "$1" = compose ]; then exit 0; fi
exit 0
''',
    )
    env["D3_RELEASE_TAG"] = "def567890123"
    mock_curl(Path(env["CURL_BIN"]), "500")
    result = run(env)
    assert result.returncode != 0
    assert (state / "last_good_release").read_text() == before_release
    assert (state / "last_good_sha").read_text().strip() == "abc123456789"


def test_term_during_new_compose_rolls_back_and_releases_lock(tmp_path):
    env, log = base(tmp_path)
    assert run(env).returncode == 0
    state = Path(env["STATE_DIR"])
    before = (state / "last_good_release").read_text()
    log.write_text("")
    marker = tmp_path / "compose.done"
    write_exec(
        Path(env["DOCKER_BIN"]),
        f'''#!/bin/bash
echo "$@" >> "{log}"
if [ "$1" = compose ] && [[ " $* " == *" config --images "* ]]; then
  printf 'frontend:%s\\nbackend:%s\\n' "$D3_RELEASE_TAG" "$D3_RELEASE_TAG"
  exit 0
fi
if [ "$1" = compose ]; then sleep 0.25; touch "{marker}"; fi
exit 0
''',
    )
    env["D3_RELEASE_TAG"] = "def567890123"
    proc = subprocess.Popen(["bash", str(SCRIPT)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 3
    while time.time() < deadline and "compose" not in log.read_text():
        time.sleep(0.01)
    proc.send_signal(signal.SIGTERM)
    stdout, stderr = proc.communicate(timeout=5)
    assert proc.returncode != 0, stdout + stderr
    lines = log.read_text().splitlines()
    assert sum(line.startswith("compose ") for line in lines) >= 2
    assert marker.exists(), "rollback compose must finish despite TERM"
    assert (state / "last_good_release").read_text() == before
    assert not list(state.glob(".release-*.release"))
    # The lock is not leaked by the signal handler.
    assert run(env).returncode == 0


def test_term_during_pull_does_not_start_new_compose(tmp_path):
    env, log = base(tmp_path)
    assert run(env).returncode == 0
    state = Path(env["STATE_DIR"])
    before = (state / "last_good_release").read_text()
    log.write_text("")
    marker = tmp_path / "pull.started"
    write_exec(
        Path(env["DOCKER_BIN"]),
        f'''#!/bin/bash
echo "$@" >> "{log}"
if [ "$1" = compose ] && [[ " $* " == *" config --images "* ]]; then
  printf 'frontend:%s\\nbackend:%s\\n' "$D3_RELEASE_TAG" "$D3_RELEASE_TAG"
  exit 0
fi
if [ "$1" = pull ]; then touch "{marker}"; sleep 0.3; fi
exit 0
''',
    )
    env["D3_RELEASE_TAG"] = "def567890123"
    proc = subprocess.Popen(["bash", str(SCRIPT)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 3
    while time.time() < deadline and not marker.exists():
        time.sleep(0.01)
    proc.send_signal(signal.SIGTERM)
    stdout, stderr = proc.communicate(timeout=5)
    assert proc.returncode != 0, stdout + stderr
    lines = log.read_text().splitlines()
    assert sum(line.startswith("compose ") for line in lines) <= 1
    assert (state / "last_good_release").read_text() == before
    assert run(env).returncode == 0


def test_term_during_rollback_is_ignored_until_group_finishes(tmp_path):
    env, log = base(tmp_path)
    assert run(env).returncode == 0
    marker = tmp_path / "rollback.done"
    log.write_text("")
    write_exec(
        Path(env["DOCKER_BIN"]),
        f'''#!/bin/bash
echo "$@" >> "{log}"
if [ "$1" = compose ]; then
  n=$(grep -c '^compose ' "{log}")
  if [ "$n" -ge 2 ]; then sleep 0.25; touch "{marker}"; fi
fi
exit 0
''',
    )
    env["D3_RELEASE_TAG"] = "def567890123"
    mock_curl(Path(env["CURL_BIN"]), "500")
    proc = subprocess.Popen(["bash", str(SCRIPT)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 3
    while time.time() < deadline and log.read_text().count("compose ") < 2:
        time.sleep(0.01)
    proc.send_signal(signal.SIGTERM)
    stdout, stderr = proc.communicate(timeout=5)
    assert proc.returncode != 0, stdout + stderr
    assert marker.exists(), "TERM must not interrupt rollback compose"
    assert (Path(env["STATE_DIR"]) / "last_good_sha").read_text().strip() == "abc123456789"
