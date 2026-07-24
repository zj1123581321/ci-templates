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


def _mock_docker_flaky_pull(log_path: Path, fail_pulls: int, image_local: bool) -> str:
    """docker mock: first `fail_pulls` pull calls fail; `image inspect` mirrors local presence."""
    return f"""#!/bin/bash
echo "$@" >> "{log_path}"
if [ "$1" = "pull" ]; then
  count_file="{log_path}.pullcount"
  n=$(cat "$count_file" 2>/dev/null || echo 0)
  n=$((n+1)); echo "$n" > "$count_file"
  [ "$n" -le {fail_pulls} ] && exit 1
  exit 0
fi
if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then
  exit {0 if image_local else 1}
fi
exit 0
"""


def _flaky_env(tmp_path, *, fail_pulls: int, image_local: bool) -> dict:
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    _write_exec(mock_dir / "docker",
                _mock_docker_flaky_pull(Path(env["DOCKER_LOG"]), fail_pulls, image_local))
    env["PULL_RETRY_DELAY"] = "0"
    return env


def test_pull_flake_retries_then_succeeds(tmp_path):
    """registry 抖 2 次、第 3 次成功 → 部署照常绿。"""
    env = _flaky_env(tmp_path, fail_pulls=2, image_local=False)
    res = _run(env)
    assert res.returncode == 0, res.stderr
    log = Path(env["DOCKER_LOG"]).read_text()
    assert log.count("pull ") == 3, log
    assert "compose up -d" in log


def test_pull_exhausted_but_local_image_proceeds(tmp_path):
    """registry 全程不可达,但 SHA 镜像已在本地(预热/回滚残留)→ 放行部署。"""
    env = _flaky_env(tmp_path, fail_pulls=99, image_local=True)
    res = _run(env)
    assert res.returncode == 0, res.stderr
    assert "already local" in res.stdout
    assert "compose up -d" in Path(env["DOCKER_LOG"]).read_text()


def test_pull_exhausted_and_no_local_image_fails(tmp_path):
    """registry 不可达且本地也没镜像 → 该失败还是失败,不能拿旧 latest 蒙混。"""
    env = _flaky_env(tmp_path, fail_pulls=99, image_local=False)
    res = _run(env)
    assert res.returncode != 0
    log = Path(env["DOCKER_LOG"]).read_text()
    assert "compose up -d" not in log, "must not compose up without the image"
    good = Path(env["STATE_DIR"]) / "last_good_tag"
    assert not good.exists()


# --- busy-lock deploy gate (opt-in) -------------------------------------------
# 服务侧对一个文件持共享锁(LOCK_SH)表示"有不可打断任务在跑";部署脚本替换容器前
# 申请排他锁(LOCK_EX),拿不到就等,超时(BUSY_LOCK_TIMEOUT)就放弃(rc=3, deferred)。
# BUSY_LOCK_FILE 为空(默认)= 关闭该门禁,行为必须与现状逐字节不变。

def test_busy_lock_optout_leaves_behavior_unchanged(tmp_path):
    """不传 BUSY_LOCK_FILE(或为空)→ 现状不变:不多一次 pull,不多开 fd。"""
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    res = _run(env)
    assert res.returncode == 0, res.stdout + res.stderr
    log = Path(env["DOCKER_LOG"]).read_text()
    # 只有 deploy_tag() 里那一次 pull;门禁的预拉不应该发生(因为门禁没开)
    assert log.count("pull ") == 1, log


def test_busy_lock_free_deploys_normally(tmp_path):
    """忙锁文件存在但空闲(无人持共享锁)→ 排他锁秒到,正常部署。"""
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    lock_file = tmp_path / "busy.lock"
    lock_file.touch()
    env["BUSY_LOCK_FILE"] = str(lock_file)
    res = _run(env)
    assert res.returncode == 0, res.stdout + res.stderr
    log = Path(env["DOCKER_LOG"]).read_text()
    assert "compose up -d" in log


def test_busy_lock_readonly_file_still_deploys(tmp_path):
    """锁文件只读(部署用户无写权限,真实 bootstrap 场景:容器内进程创建,0444/0644)
    → flock(2) 的互斥语义作用在 inode 上,不要求 fd 有写权限,只读打开一样能拿到
    排他锁,部署应正常进行,不能因为 Permission denied 被判死。"""
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    lock_file = tmp_path / "busy.lock"
    lock_file.touch()
    os.chmod(lock_file, 0o444)
    env["BUSY_LOCK_FILE"] = str(lock_file)
    res = _run(env)
    assert res.returncode == 0, res.stdout + res.stderr
    log = Path(env["DOCKER_LOG"]).read_text()
    assert "compose up -d" in log


def test_busy_lock_held_defers_untouched(tmp_path):
    """忙锁被服务侧共享锁占住,超预算仍未空闲 → rc=3,容器/last_good 完全不动。"""
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    lock_file = tmp_path / "busy.lock"
    lock_file.touch()
    env["BUSY_LOCK_FILE"] = str(lock_file)
    env["BUSY_LOCK_TIMEOUT"] = "1"

    holder = subprocess.Popen(["flock", "-s", str(lock_file), "sleep", "5"])
    try:
        time.sleep(0.5)  # ensure the holder has actually grabbed the shared lock
        res = _run(env)
        assert res.returncode == 3, res.stdout + res.stderr
        log = Path(env["DOCKER_LOG"]).read_text()
        assert "compose up" not in log, log
        assert "tag " not in log, log
        good = Path(env["STATE_DIR"]) / "last_good_tag"
        assert not good.exists()
        assert "DEFERRED" in res.stdout
    finally:
        holder.terminate()
        holder.wait()


def test_busy_lock_released_within_budget_deploys(tmp_path):
    """忙锁在等待预算内被释放 → 正常拿到排他锁并完成部署。"""
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    lock_file = tmp_path / "busy.lock"
    lock_file.touch()
    env["BUSY_LOCK_FILE"] = str(lock_file)
    env["BUSY_LOCK_TIMEOUT"] = "15"

    holder = subprocess.Popen(["flock", "-s", str(lock_file), "sleep", "2"])
    try:
        time.sleep(0.5)
        res = _run(env)
        assert res.returncode == 0, res.stdout + res.stderr
    finally:
        holder.terminate()
        holder.wait()


def test_busy_lock_missing_file_warns_and_proceeds(tmp_path):
    """锁文件(及其父目录)都不存在 → 创建 + 打显著 WARN,但不阻止部署(误配不 fail-closed)。"""
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    env["BUSY_LOCK_FILE"] = str(tmp_path / "nope" / "busy.lock")
    res = _run(env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "WARN" in res.stdout


def _mock_docker_admission_probe(log_path: Path) -> str:
    """docker mock:在 `compose` 调用当下,用宿主 flock 非阻塞探一次 BUSY_LOCK_FILE 的共享锁。

    这直接证明部署脚本在 compose up 期间确实握着 LOCK_EX —— 容器侧此刻申请
    LOCK_SH 必然失败(admission 已关闭),没有 TOCTOU 窗口。
    """
    return f"""#!/bin/bash
if [ "$1" = "compose" ]; then
  if flock -n -s "$BUSY_LOCK_FILE" true; then
    echo "sh_probe=open" >> "{log_path}"
  else
    echo "sh_probe=closed" >> "{log_path}"
  fi
  exit 0
fi
echo "$@" >> "{log_path}"
exit 0
"""


def test_admission_closed_during_replace(tmp_path):
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    _write_exec(mock_dir / "docker", _mock_docker_admission_probe(Path(env["DOCKER_LOG"])))

    lock_file = tmp_path / "busy.lock"
    lock_file.touch()
    env["BUSY_LOCK_FILE"] = str(lock_file)
    env["BUSY_LOCK_TIMEOUT"] = "15"

    res = _run(env)
    assert res.returncode == 0, res.stdout + res.stderr
    log = Path(env["DOCKER_LOG"]).read_text()
    assert "sh_probe=closed" in log, log
