"""Contract tests for the ACR image publisher's bounded retries."""

import os
import subprocess
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "push_to_acr.sh"


def _env(tmp_path: Path, docker_bin: Path, **overrides: str) -> dict[str, str]:
    env = os.environ | {
        "ACR_REGISTRY": "registry.example",
        "ACR_NAMESPACE": "namespace",
        "IMAGE_NAME": "service",
        "GIT_SHA": "abc123",
        "DOCKER_BIN": str(docker_bin),
        "BUILD_CONTEXT": str(tmp_path),
        "DOCKERFILE": "Dockerfile",
        "PUSH_RETRY_DELAY_SECONDS": "0",
    }
    env.update(overrides)
    return env


def _write_fake_docker(tmp_path: Path, body: str) -> Path:
    docker = tmp_path / "docker"
    docker.write_text("#!/bin/bash\nset -euo pipefail\n" + body)
    docker.chmod(0o755)
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    return docker


def test_push_retries_a_transient_failure_without_rebuilding(tmp_path):
    calls = tmp_path / "calls"
    docker = _write_fake_docker(
        tmp_path,
        f'''if [ "$1" = build ]; then echo build >> "{calls}"; exit 0; fi
if [ "$1" = push ]; then
  echo "push:$2" >> "{calls}"
  [ "$(grep -c '^push:' "{calls}")" -eq 1 ] && exit 1
  exit 0
fi
''',
    )

    result = subprocess.run(
        ["bash", str(SCRIPT)], env=_env(tmp_path, docker, PUSH_MAX_ATTEMPTS="3"),
        text=True, capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines() == [
        "build",
        "push:registry.example/namespace/service:abc123",
        "push:registry.example/namespace/service:abc123",
    ]
    assert "attempt 2/3" in result.stdout


def test_push_times_out_and_stops_after_configured_attempts(tmp_path):
    docker = _write_fake_docker(
        tmp_path,
        '''if [ "$1" = build ]; then exit 0; fi
if [ "$1" = push ]; then sleep 2; fi
''',
    )

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=_env(tmp_path, docker, PUSH_TIMEOUT_SECONDS="1", PUSH_MAX_ATTEMPTS="2"),
        text=True, capture_output=True,
    )

    assert result.returncode != 0
    assert "timed out after 1s" in result.stdout
    assert "failed after 2 attempts" in result.stdout


def test_push_timeout_kills_a_client_that_ignores_sigterm(tmp_path):
    docker = _write_fake_docker(
        tmp_path,
        '''if [ "$1" = build ]; then exit 0; fi
if [ "$1" = push ]; then trap '' TERM; sleep 10; fi
''',
    )

    started = time.monotonic()
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=_env(
            tmp_path, docker, PUSH_TIMEOUT_SECONDS="1", PUSH_TIMEOUT_KILL_AFTER_SECONDS="1",
            PUSH_MAX_ATTEMPTS="1",
        ),
        text=True, capture_output=True,
    )

    assert result.returncode != 0
    assert time.monotonic() - started < 4
    assert "timed out after 1s" in result.stdout


def test_push_bounds_cannot_be_relaxed(tmp_path):
    docker = _write_fake_docker(tmp_path, 'exit 0\n')

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=_env(tmp_path, docker, PUSH_TIMEOUT_SECONDS="301", PUSH_MAX_ATTEMPTS="4"),
        text=True, capture_output=True,
    )

    assert result.returncode == 2
    assert "PUSH_TIMEOUT_SECONDS must not exceed 300" in result.stderr
