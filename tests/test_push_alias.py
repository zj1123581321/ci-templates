"""Bounded, no-rebuild retry contract for alias image pushes."""
import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "push_alias_to_acr.sh"


def write_exec(path: Path, body: str):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def test_alias_push_retries_three_times_without_build(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    count = tmp_path / "count"
    log = tmp_path / "log"
    docker = bindir / "docker"
    timeout = bindir / "timeout"
    write_exec(
        docker,
        f'''#!/bin/bash
echo "$@" >> "{log}"
n=$(cat "{count}" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "{count}"
[ "$n" -lt 3 ] && exit 1
exit 0
''',
    )
    write_exec(timeout, "#!/bin/bash\nshift 2\nexec \"$@\"\n")
    env = dict(os.environ)
    env.update(
        SOURCE_IMAGE="registry/ns/base:abc123456789",
        TARGET_IMAGE="registry/ns/worker:abc123456789",
        DOCKER_BIN=str(docker),
        TIMEOUT_BIN=str(timeout),
        PUSH_RETRY_DELAY_SECONDS="0",
    )
    result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert log.read_text().splitlines() == [
        "push registry/ns/worker:abc123456789",
        "push registry/ns/worker:abc123456789",
        "push registry/ns/worker:abc123456789",
    ]
    assert "build" not in log.read_text()


def test_alias_push_final_failure_is_bounded(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    timeout = bindir / "timeout"
    write_exec(docker, "#!/bin/bash\nexit 1\n")
    write_exec(timeout, "#!/bin/bash\nshift 2\nexec \"$@\"\n")
    env = dict(os.environ)
    env.update(
        SOURCE_IMAGE="registry/ns/base:abc123456789",
        TARGET_IMAGE="registry/ns/worker:abc123456789",
        DOCKER_BIN=str(docker),
        TIMEOUT_BIN=str(timeout),
        PUSH_RETRY_DELAY_SECONDS="0",
    )
    result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert "failed after 3 attempts" in result.stderr
