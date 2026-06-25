#!/bin/bash
# SSH-side deploy internals for the ci-templates reusable workflow (T4).
#
# Hardened over the docker-package skill's original pull_and_deploy.sh:
#   - per-host flock      : concurrent deploys to the same host serialize
#   - immutable SHA tag   : deploys ${ACR_IMAGE}:${GIT_SHA}, never :latest
#   - last-good tracking  : records the last healthy tag for rollback
#   - health probe gate   : warmup + retries + expected status
#   - auto rollback       : probe failure -> redeploy previous good tag
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

# --- deploy a specific tag: pull (if remote) + retag + compose up ------------
deploy_tag() {
  local tag="$1"
  log "deploying ${ACR_IMAGE}:${tag}"
  # the SHA image may already be local (rollback); pull is best-effort idempotent
  "$DOCKER_BIN" pull "${ACR_IMAGE}:${tag}"
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

mkdir -p "$(dirname "$HOST_LOCK")" 2>/dev/null || true
exec 9>"$HOST_LOCK"
flock 9          # blocks until this host's deploy lock is free
do_deploy
rc=$?
flock -u 9
exit $rc
