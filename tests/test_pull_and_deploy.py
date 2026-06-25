"""TDD for pull_and_deploy.sh — the SSH-side deploy internals.

Contract (eng-review A3 / T4):
  - per-host flock so concurrent deploys to the same host serialize
  - deploys an IMMUTABLE git-SHA image tag, records the last good tag
  - post-deploy health probe (warmup / retries / expected status)
  - probe failure -> automatic rollback to the previous good tag
  - rollback must NOT promote the failed tag to "last good"

docker / curl are mocked so no real daemon or network is touched. The script
honours DOCKER_BIN / CURL_BIN overrides for exactly this reason.
"""
import os
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "pull_and_deploy.sh"


def _write_exec(path: Path, body: str):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _mock_docker(log_path: Path, compose_sleep: float = 0.0) -> str:
    return f"""#!/bin/bash
echo "$@" >> "{log_path}"
if [ "$1" = "compose" ]; then
  sleep {compose_sleep}
fi
exit 0
"""


def _mock_curl(status: str) -> str:
    # mimics: curl -s -o /dev/null -w '%{{http_code}}' ... -> prints an HTTP code
    return f"""#!/bin/bash
printf '%s' "{status}"
exit 0
"""


def _base_env(tmp_path: Path, *, mock_dir: Path, status: str = "200",
              compose_sleep: float = 0.0) -> dict:
    docker_log = tmp_path / "docker.log"
    docker = mock_dir / "docker"
    curl = mock_dir / "curl"
    _write_exec(docker, _mock_docker(docker_log, compose_sleep))
    _write_exec(curl, _mock_curl(status))

    deploy_dir = tmp_path / "app"
    deploy_dir.mkdir(exist_ok=True)
    (deploy_dir / "docker-compose.yml").write_text("services: {}\n")

    env = dict(os.environ)
    env.update(
        IMAGE_NAME="demo",
        ACR_IMAGE="registry.example.com/ns/demo",
        GIT_SHA="abc1234",
        DEPLOY_DIR=str(deploy_dir),
        STATE_DIR=str(tmp_path / "state"),
        HOST_LOCK=str(tmp_path / "host.lock"),
        HEALTHCHECK_URL="http://localhost/health",
        HEALTHCHECK_EXPECT_STATUS="200",
        HEALTHCHECK_RETRIES="2",
        HEALTHCHECK_INTERVAL="0",
        HEALTHCHECK_WARMUP="0",
        HEALTHCHECK_TIMEOUT="1",
        DOCKER_BIN=str(docker),
        CURL_BIN=str(curl),
        DOCKER_LOG=str(docker_log),
    )
    return env


def _run(env, extra=None):
    e = dict(env)
    if extra:
        e.update(extra)
    return subprocess.run(
        ["bash", str(SCRIPT)], env=e, capture_output=True, text=True
    )


def test_script_exists_and_is_bash():
    assert SCRIPT.exists(), "pull_and_deploy.sh must exist"
    assert SCRIPT.read_text().startswith("#!"), "must have a shebang"


def test_healthy_deploy_succeeds_and_records_good_tag(tmp_path):
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    res = _run(env)
    assert res.returncode == 0, res.stdout + res.stderr

    good = Path(env["STATE_DIR"]) / "last_good_tag"
    assert good.exists(), "must record the last good tag on success"
    assert good.read_text().strip() == "abc1234"


def test_deploys_immutable_git_sha_tag(tmp_path):
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    res = _run(env)
    assert res.returncode == 0, res.stdout + res.stderr

    docker_log = Path(env["DOCKER_LOG"]).read_text()
    # the SHA-tagged image must be pulled, never ":latest" from ACR
    assert "pull registry.example.com/ns/demo:abc1234" in docker_log
    assert "pull registry.example.com/ns/demo:latest" not in docker_log


def test_probe_failure_triggers_rollback(tmp_path):
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()

    # 1st deploy is healthy -> records good tag "abc1234"
    env_ok = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    assert _run(env_ok).returncode == 0

    # 2nd deploy of a new SHA is unhealthy -> must roll back to abc1234
    env_bad = _base_env(tmp_path, mock_dir=mock_dir, status="500")
    env_bad["GIT_SHA"] = "def5678"
    Path(env_bad["DOCKER_LOG"]).write_text("")  # reset log for assertions
    res = _run(env_bad)

    assert res.returncode != 0, "an unhealthy deploy must report failure"

    docker_log = Path(env_bad["DOCKER_LOG"]).read_text()
    # rollback retags the previous good image and brings it back up
    assert "abc1234" in docker_log, "rollback must redeploy the previous good tag"

    # the failed tag must NOT be promoted to last good
    good = (Path(env_bad["STATE_DIR"]) / "last_good_tag").read_text().strip()
    assert good == "abc1234", f"last good tag must stay abc1234, got {good}"


def test_probe_failure_without_previous_good_just_fails(tmp_path):
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="500")
    res = _run(env)
    assert res.returncode != 0
    good = Path(env["STATE_DIR"]) / "last_good_tag"
    assert not good.exists(), "must not record a bad deploy as good"


def test_concurrent_same_host_deploys_serialize(tmp_path):
    """Two deploys sharing HOST_LOCK must not run their critical sections at once."""
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    event_log = tmp_path / "events.log"

    def launch(deploy_id, sub):
        sub.mkdir()
        env = _base_env(sub, mock_dir=sub, status="200", compose_sleep=0.4)
        env["HOST_LOCK"] = str(tmp_path / "shared-host.lock")  # same host lock
        env["DEPLOY_EVENT_LOG"] = str(event_log)
        env["DEPLOY_ID"] = deploy_id
        return _run(env)

    results = {}

    def worker(name):
        results[name] = launch(name, tmp_path / name)

    t1 = threading.Thread(target=worker, args=("A",))
    t2 = threading.Thread(target=worker, args=("B",))
    t1.start()
    time.sleep(0.05)  # ensure A grabs the lock first
    t2.start()
    t1.join()
    t2.join()

    assert results["A"].returncode == 0, results["A"].stderr
    assert results["B"].returncode == 0, results["B"].stderr

    events = [ln.strip() for ln in event_log.read_text().splitlines() if ln.strip()]
    # serialized critical sections => enter/exit are never interleaved
    assert events == ["enter:A", "exit:A", "enter:B", "exit:B"], events
