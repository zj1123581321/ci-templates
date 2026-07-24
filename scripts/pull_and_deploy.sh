#!/bin/bash
# SSH-side deploy internals for the ci-templates reusable workflow (T4).
#
# Hardened over the docker-package skill's original pull_and_deploy.sh:
#   - per-host flock      : concurrent deploys to the same host serialize
#   - immutable SHA tag   : deploys ${ACR_IMAGE}:${GIT_SHA}, never :latest
#   - last-good tracking  : records the last healthy tag for rollback
#   - health probe gate   : warmup + retries + expected status
#   - auto rollback       : probe failure -> redeploy previous good tag
#   - busy-lock gate      : opt-in (BUSY_LOCK_FILE); exit codes: 0=healthy,
#                           1=probe failed (rolled back), 3=deferred (service
#                           busy, busy lock not acquired in time, old
#                           container untouched)
#
# All inputs come from the environment so the build-deploy.yml job can export
# them and so tests can inject mocks (DOCKER_BIN / CURL_BIN).
set -euo pipefail

# --- required inputs ---------------------------------------------------------
: "${IMAGE_NAME:?IMAGE_NAME required}"     # local tag, e.g. ops-dispatcher
: "${ACR_IMAGE:?ACR_IMAGE required}"       # full registry path
: "${GIT_SHA:?GIT_SHA required}"           # immutable image tag to deploy
: "${DEPLOY_DIR:?DEPLOY_DIR required}"     # project root holding the compose file

# --- tunables (sane defaults) ------------------------------------------------
STATE_DIR="${STATE_DIR:-${DEPLOY_DIR}/.deploy-state}"
HOST_LOCK="${HOST_LOCK:-/var/lock/fleet-deploy.lock}"   # ONE lock per host
DOCKER_BIN="${DOCKER_BIN:-docker}"
CURL_BIN="${CURL_BIN:-curl}"

BUSY_LOCK_FILE="${BUSY_LOCK_FILE:-}"        # opt-in deploy gate; empty = off
BUSY_LOCK_TIMEOUT="${BUSY_LOCK_TIMEOUT:-600}"

PULL_RETRIES="${PULL_RETRIES:-3}"
PULL_RETRY_DELAY="${PULL_RETRY_DELAY:-10}"  # base seconds; backoff = delay * attempt

HEALTHCHECK_URL="${HEALTHCHECK_URL:-}"
HEALTHCHECK_EXPECT_STATUS="${HEALTHCHECK_EXPECT_STATUS:-200}"
HEALTHCHECK_RETRIES="${HEALTHCHECK_RETRIES:-5}"
HEALTHCHECK_INTERVAL="${HEALTHCHECK_INTERVAL:-3}"   # seconds between probes
HEALTHCHECK_WARMUP="${HEALTHCHECK_WARMUP:-5}"       # seconds before first probe
HEALTHCHECK_TIMEOUT="${HEALTHCHECK_TIMEOUT:-5}"     # per-probe curl timeout

# optional test/observability hooks
DEPLOY_EVENT_LOG="${DEPLOY_EVENT_LOG:-}"
DEPLOY_ID="${DEPLOY_ID:-$$}"

GOOD_TAG_FILE="${STATE_DIR}/last_good_tag"

log()   { echo "[deploy] $*"; }
event() { [ -n "$DEPLOY_EVENT_LOG" ] && echo "$1:${DEPLOY_ID}" >> "$DEPLOY_EVENT_LOG" || true; }

is_positive_integer() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

# --- pull with retry + local fallback ----------------------------------------
# 单发 docker pull 碰上 registry 网络抖动(EOF/reset/timeout)会整场判死;而 SHA tag
# 不可变,本地已有的同 tag 镜像(回滚残留/预热)与远端逐字节一致 —— registry 单独挂
# 不应该拦下部署(2026-07-09 n305→ACR 间歇抖,imflow 因此 9 连败)。
pull_image() {
  local ref="$1" attempt=1
  while [ "$attempt" -le "$PULL_RETRIES" ]; do
    if "$DOCKER_BIN" pull "$ref"; then return 0; fi
    log "pull attempt ${attempt}/${PULL_RETRIES} failed for ${ref}"
    [ "$attempt" -lt "$PULL_RETRIES" ] && sleep $((PULL_RETRY_DELAY * attempt))
    attempt=$((attempt + 1))
  done
  if "$DOCKER_BIN" image inspect "$ref" >/dev/null 2>&1; then
    log "registry unreachable but ${ref} already local — proceeding"
    return 0
  fi
  log "pull failed ${PULL_RETRIES}x and ${ref} not local — aborting"
  return 1
}

# --- deploy a specific tag: pull (if remote) + retag + compose up ------------
deploy_tag() {
  local tag="$1"
  log "deploying ${ACR_IMAGE}:${tag}"
  pull_image "${ACR_IMAGE}:${tag}"
  "$DOCKER_BIN" tag "${ACR_IMAGE}:${tag}" "${IMAGE_NAME}:latest"
  ( cd "$DEPLOY_DIR" && "$DOCKER_BIN" compose up -d )
}

# --- health probe: returns 0 if the service answers as expected --------------
health_probe() {
  [ -z "$HEALTHCHECK_URL" ] && { log "no HEALTHCHECK_URL, skipping probe"; return 0; }
  log "warmup ${HEALTHCHECK_WARMUP}s before probing ${HEALTHCHECK_URL}"
  sleep "$HEALTHCHECK_WARMUP"
  local attempt=1
  while [ "$attempt" -le "$HEALTHCHECK_RETRIES" ]; do
    local code
    code="$("$CURL_BIN" -s -o /dev/null -w '%{http_code}' \
             --max-time "$HEALTHCHECK_TIMEOUT" "$HEALTHCHECK_URL" || true)"
    if [ "$code" = "$HEALTHCHECK_EXPECT_STATUS" ]; then
      log "health probe OK (attempt ${attempt}, status ${code})"
      return 0
    fi
    log "health probe attempt ${attempt}/${HEALTHCHECK_RETRIES} got '${code}', want ${HEALTHCHECK_EXPECT_STATUS}"
    attempt=$((attempt + 1))
    [ "$attempt" -le "$HEALTHCHECK_RETRIES" ] && sleep "$HEALTHCHECK_INTERVAL"
  done
  return 1
}

# --- critical section, serialized per host via flock -------------------------
do_deploy() {
  event enter
  mkdir -p "$STATE_DIR"

  local prev_good=""
  [ -f "$GOOD_TAG_FILE" ] && prev_good="$(cat "$GOOD_TAG_FILE")"

  deploy_tag "$GIT_SHA"

  if health_probe; then
    echo "$GIT_SHA" > "$GOOD_TAG_FILE"
    log "deploy of ${GIT_SHA} healthy; recorded as last good"
    event exit
    return 0
  fi

  log "health probe FAILED for ${GIT_SHA}"
  if [ -n "$prev_good" ] && [ "$prev_good" != "$GIT_SHA" ]; then
    log "rolling back to previous good tag ${prev_good}"
    deploy_tag "$prev_good"
    # last_good_tag intentionally left at ${prev_good}; the bad tag is NOT promoted
    log "rollback to ${prev_good} complete"
  else
    log "no previous good tag to roll back to"
  fi
  event exit
  return 1
}

# --- busy-lock deploy gate (opt-in; BUSY_LOCK_FILE empty = skip entirely) ----
# 服务侧每个不可打断任务存续期间对这个文件持共享锁(LOCK_SH);这里在替换容器前
# 申请排他锁(LOCK_EX) —— 拿到即证明"无进行中任务,且新任务进不来"。
# 锁申请顺序固定:先忙锁(fd 8,服务级) 后 HOST_LOCK(fd 9,整机级)——每个服务的忙锁
# 文件互不相同,HOST_LOCK 全局只有一把,顺序固定的两级锁不构成环,不会死锁。
# 但"申请顺序固定"不等于"等待期间可以互相攥着":两把锁只应该在真正的替换窗口
# (即将 compose up 之前)同时持有;在等待阶段,任何时刻最多只持有正在等的那一把,
# 绝不允许"因为在等 HOST_LOCK,所以顺手一直攥着已经到手的忙锁"——那会让 admission
# 在纯排队等待、尚未开始替换容器的时间里被误关,反而伤到这套门禁本该保护的对象。
# 做法是一个循环:申请忙锁(带预算)→ 非阻塞探 HOST_LOCK → HOST_LOCK 被占就立即
# 放掉忙锁、sleep 5s 后重试整对锁 → 总预算(BUSY_LOCK_TIMEOUT)耗尽仍未能同时拿到
# 两把锁,则本次 deferred。
mkdir -p "$(dirname "$HOST_LOCK")" 2>/dev/null || true
exec 9>"$HOST_LOCK"

if [ -n "$BUSY_LOCK_FILE" ]; then
  if ! is_positive_integer "$BUSY_LOCK_TIMEOUT"; then
    log "BUSY_LOCK_TIMEOUT must be a positive integer, got: ${BUSY_LOCK_TIMEOUT}"
    exit 1
  fi
  # pre-pull outside all locks: shrinks the admission-closed window to seconds
  pull_image "${ACR_IMAGE}:${GIT_SHA}"
  if [ ! -e "$BUSY_LOCK_FILE" ]; then
    log "WARN: busy lock file ${BUSY_LOCK_FILE} missing — service side may not hold locks yet; creating it, proceeding WITHOUT drain protection"
    mkdir -p "$(dirname "$BUSY_LOCK_FILE")"
    : >> "$BUSY_LOCK_FILE"   # append-open 的空操作：绝不 truncate，只是把文件创建出来
  fi
  # 只读打开：服务侧(容器内进程)创建的锁文件通常属主是容器内用户/root、权限较窄
  # (如 0644),宿主上跑部署脚本的用户往往只有读权限、没有写权限。flock(2) 的互斥
  # 语义作用在文件的 inode 上,不要求持有该锁的 fd 具备写权限——只读 fd 一样能申请
  # LOCK_EX/LOCK_SH。这里改成只读打开以兼容"部署用户对锁文件只读"的真实场景，
  # 语义与之前的 append-open 完全一致，只是不再要求写权限。
  exec 8<"$BUSY_LOCK_FILE"

  _deadline=$(( SECONDS + BUSY_LOCK_TIMEOUT ))
  while :; do
    _remain=$(( _deadline - SECONDS ))
    if [ "$_remain" -le 0 ]; then
      log "service busy: host deploy lock busy through the whole ${BUSY_LOCK_TIMEOUT}s budget — DEFERRED, old container kept"
      exit 3
    fi
    _frc=0; flock -w "$_remain" -x 8 || _frc=$?
    if [ "$_frc" -eq 1 ]; then
      log "service busy: busy lock not acquired within budget — DEFERRED, old container kept"
      exit 3
    elif [ "$_frc" -ne 0 ]; then
      log "flock on busy lock failed with rc=${_frc} (not a lock timeout — config or host problem)"
      exit 1
    fi
    if flock -n 9; then
      break            # 两把锁同时在手 → 替换窗口开始
    fi
    flock -u 8         # 整机锁被别的部署占着:立即放掉忙锁,admission 重新打开
    sleep 5            # 稍后重试整对锁(预算内)
  done
  log "busy lock + host deploy lock both acquired (admission closed until replace completes)"
fi

# opt-in 路径:fd 9 上面已经通过 flock -n 9 拿到锁了,这一行只是确认——同一进程对
# 同一 fd 重复 flock 是空操作,立即成功返回,不会阻塞。
# opt-out 路径:busy-lock if 块整体跳过,fd 9 尚未加锁,这一行就是原来的行为——
# 阻塞直到这台主机的部署锁空闲。
flock 9
do_deploy
rc=$?
flock -u 9
# fd 8(忙锁,若开启)必须活过整个 do_deploy()(含探针失败后的回滚),并且晚于
# fd 9 释放,才能保证 admission 在 compose up + 探针 + 回滚全程都是关闭的。
# 这里选择显式 flock -u 8(而不是依赖脚本 exit 时内核自动释放两把锁):两者都
# 安全(内核保证进程退出必然释放所有 flock),但显式释放让代码里的锁生命周期
# 一目了然,也让"9 先于 8 释放"的顺序不依赖读者去脑补 exit 的隐式行为。
[ -n "$BUSY_LOCK_FILE" ] && flock -u 8
exit $rc
