#!/usr/bin/env bash
# SSH-side atomic D3 release deployment for a group of immutable images.
#
# The CI job validates JSON and sends this script a restricted, tab-separated
# manifest.  This host-side code intentionally has no jq/Python dependency.
# Every image is pulled and retagged with D3_RELEASE_TAG before one compose up;
# no mutable registry latest tag is ever used.  The previous manifest and SHA
# are promoted only after all probes pass, and rollback uses one compose up for
# the complete previous group.
set -u -o pipefail

: "${RELEASE_MANIFEST:?RELEASE_MANIFEST required}"
: "${D3_RELEASE_TAG:?D3_RELEASE_TAG required}"
: "${DEPLOY_DIR:?DEPLOY_DIR required}"

STATE_DIR="${STATE_DIR:-${DEPLOY_DIR}/.deploy-state/release}"
HOST_LOCK="${HOST_LOCK:-/var/lock/fleet-deploy.lock}"
DOCKER_BIN="${DOCKER_BIN:-docker}"
CURL_BIN="${CURL_BIN:-curl}"
ACR_REGISTRY="${ACR_REGISTRY:?ACR_REGISTRY required}"
ACR_NAMESPACE="${ACR_NAMESPACE:?ACR_NAMESPACE required}"

PULL_RETRIES="${PULL_RETRIES:-3}"
PULL_RETRY_DELAY="${PULL_RETRY_DELAY:-10}"
HEALTHCHECK_RETRIES="${HEALTHCHECK_RETRIES:-5}"
HEALTHCHECK_INTERVAL="${HEALTHCHECK_INTERVAL:-3}"
HEALTHCHECK_WARMUP="${HEALTHCHECK_WARMUP:-5}"
HEALTHCHECK_TIMEOUT="${HEALTHCHECK_TIMEOUT:-5}"
REMOTE_SCRIPT_PATH="${RELEASE_TEMP_SCRIPT:-}"
REMOTE_MANIFEST_PATH="$RELEASE_MANIFEST"

GOOD_SHA_FILE="$STATE_DIR/last_good_sha"
GOOD_MANIFEST_FILE="$STATE_DIR/last_good_manifest"
GOOD_RELEASE_FILE="$STATE_DIR/last_good_release"
ENV_FILE="$DEPLOY_DIR/.d3-release.env"
LOCK_FD=9
STAGING_PREFIX="$STATE_DIR/.release-${D3_RELEASE_TAG}-$$"
PENDING_SIGNAL=""
ROLLBACK_MODE=0

[[ "$D3_RELEASE_TAG" =~ ^[0-9a-f]{12}$ ]] || {
  echo "[release] D3_RELEASE_TAG must be a 12-character lowercase git SHA" >&2
  exit 2
}
[[ "$ACR_REGISTRY" =~ ^[A-Za-z0-9.:-]+$ && "$ACR_NAMESPACE" =~ ^[A-Za-z0-9._-]+$ ]] || {
  echo "[release] unsafe ACR registry/namespace" >&2
  exit 2
}
[[ "$PULL_RETRIES" =~ ^[1-9][0-9]*$ && "$HEALTHCHECK_RETRIES" =~ ^[1-9][0-9]*$ ]] || {
  echo "[release] retry counts must be positive integers" >&2
  exit 2
}
[[ "$PULL_RETRY_DELAY" =~ ^[0-9]+$ && "$HEALTHCHECK_INTERVAL" =~ ^[0-9]+$ && "$HEALTHCHECK_WARMUP" =~ ^[0-9]+$ && "$HEALTHCHECK_TIMEOUT" =~ ^[1-9][0-9]*$ ]] || {
  echo "[release] timing values must be non-negative/positive integers" >&2
  exit 2
}

log() { echo "[release] $*"; }
die() { log "ERROR: $*" >&2; return 1; }

cleanup() {
  local rc=$?
  rm -f -- "${STAGING_PREFIX}.manifest" "${STAGING_PREFIX}.env" "${STAGING_PREFIX}.sha" "${STAGING_PREFIX}.release" "${STAGING_PREFIX}.previous" 2>/dev/null || true
  # These are exact per-run paths supplied by the workflow; never glob /tmp.
  case "$REMOTE_SCRIPT_PATH" in
    /tmp/d3-release-[0-9]*-[0-9]*.sh) rm -f -- "$REMOTE_SCRIPT_PATH" 2>/dev/null || true ;;
  esac
  case "$REMOTE_MANIFEST_PATH" in
    /tmp/d3-release-[0-9]*-[0-9]*.manifest) rm -f -- "$REMOTE_MANIFEST_PATH" 2>/dev/null || true ;;
  esac
  if [[ -n "${LOCK_HELD:-}" ]]; then
    flock -u 9 2>/dev/null || true
  fi
  trap - EXIT INT TERM
  exit "$rc"
}
on_signal() {
  PENDING_SIGNAL="$1"
  log "received ${1}; will finish current command and rollback without promotion"
}
trap cleanup EXIT
trap 'on_signal INT' INT
trap 'on_signal TERM' TERM

check_pending() {
  (( ROLLBACK_MODE == 1 )) && return 0
  [[ -z "$PENDING_SIGNAL" ]] || return 130
  return 0
}

validate_scalar() {
  local value="$1"
  [[ -n "$value" ]] || return 1
  [[ "$value" != *$'\n'* && "$value" != *$'\r'* && "$value" != *$'\t'* ]] || return 1
  return 0
}

probe_url_safe() {
  local value="$1" ch
  [[ "$value" == http://* || "$value" == https://* ]] || return 1
  # Keep this check in bash: the remote host only needs bash/docker/curl/flock.
  for ch in $' ' $'\t' $'\n' $'\r' ';' '|' '&' '$' '(' ')' '{' '}' '<' '>' '[' ']' '\\' '"' "'"; do
    [[ "$value" == *"$ch"* ]] && return 1
  done
  return 0
}

IMAGE_NAMES=()
IMAGE_REFS=()
PROBE_URLS=()
PROBE_STATUS=()

load_manifest() {
  local file="$1" line kind a b
  [[ -f "$file" ]] || { log "manifest not found: $file" >&2; return 1; }
  IMAGE_NAMES=(); IMAGE_REFS=(); PROBE_URLS=(); PROBE_STATUS=()
  local header_seen=0
  while IFS=$'\t' read -r kind a b; do
    [[ -z "$kind" ]] && continue
    if [[ "$kind" == "D3_RELEASE_MANIFEST=1" ]]; then
      header_seen=1
      continue
    fi
    [[ "$header_seen" -eq 1 ]] || { log "manifest header missing" >&2; return 1; }
    case "$kind" in
      image)
        validate_scalar "$a" || { log "unsafe image name in manifest" >&2; return 1; }
        validate_scalar "$b" || { log "unsafe image ref in manifest" >&2; return 1; }
        [[ "$a" =~ ^[a-z0-9][a-z0-9._-]{0,127}$ ]] || { log "invalid image name: $a" >&2; return 1; }
        [[ "$b" != *$'\t'* ]] || { log "image ref contains a tab" >&2; return 1; }
        [[ "$b" =~ ^[A-Za-z0-9._:/-]+$ ]] || { log "invalid image ref: $b" >&2; return 1; }
        IMAGE_NAMES+=("$a")
        if [[ "$b" == */* ]]; then
          IMAGE_REFS+=("$b")
        else
          IMAGE_REFS+=("$ACR_REGISTRY/$ACR_NAMESPACE/$b")
        fi
        ;;
      probe)
        validate_scalar "$a" || { log "unsafe probe URL" >&2; return 1; }
        probe_url_safe "$a" || { log "invalid probe URL: $a" >&2; return 1; }
        [[ "$b" =~ ^[1-5][0-9][0-9]$ ]] || { log "invalid probe status: $b" >&2; return 1; }
        PROBE_URLS+=("$a")
        PROBE_STATUS+=("$b")
        ;;
      *) log "unknown manifest record: $kind" >&2; return 1 ;;
    esac
  done < "$file"
  [[ "$header_seen" -eq 1 && "${#IMAGE_NAMES[@]}" -gt 0 ]] || { log "manifest has no images" >&2; return 1; }
  [[ "${#PROBE_URLS[@]}" -gt 0 ]] || { log "manifest has no release probes" >&2; return 1; }
}

pull_and_retag() {
  local tag="$1" i ref local_ref attempt
  for ((i = 0; i < ${#IMAGE_NAMES[@]}; i++)); do
    ref="${IMAGE_REFS[$i]}:${tag}"
    local_ref="${IMAGE_NAMES[$i]}:${tag}"
    attempt=1
    while (( attempt <= PULL_RETRIES )); do
      if "$DOCKER_BIN" pull "$ref"; then break; fi
      log "pull ${ref} failed (${attempt}/${PULL_RETRIES})"
      if (( attempt == PULL_RETRIES )); then
        log "pull failed; compose will not run" >&2
        return 1
      fi
      sleep "$((PULL_RETRY_DELAY * attempt))"
      attempt=$((attempt + 1))
    done
    check_pending || return 130
    "$DOCKER_BIN" tag "$ref" "$local_ref" || {
      log "retag failed for ${ref}; compose will not run" >&2
      return 1
    }
    check_pending || return 130
  done
  return 0
}

compose_release() {
  local tag="$1" env_tmp="${STAGING_PREFIX}.env"
  check_pending || return 130
  printf 'D3_RELEASE_TAG=%s\n' "$tag" > "$env_tmp" || return 1
  mv -f -- "$env_tmp" "$ENV_FILE" || return 1
  local compose_args=(compose)
  # Compose does not load the project .env when --env-file is supplied.  Keep
  # the caller's variables and overlay only D3_RELEASE_TAG afterwards.
  if [[ -f "$DEPLOY_DIR/.env" ]]; then
    compose_args+=(--env-file "$DEPLOY_DIR/.env")
  fi
  compose_args+=(--env-file "$ENV_FILE" up -d)
  local compose_rc=0
  (cd "$DEPLOY_DIR" && "$DOCKER_BIN" "${compose_args[@]}") || compose_rc=$?
  check_pending || return 130
  return "$compose_rc"
}

probe_release() {
  local i attempt code
  (( ${#PROBE_URLS[@]} == 0 )) && { log "no release probes declared"; return 0; }
  check_pending || return 130
  sleep "$HEALTHCHECK_WARMUP"
  check_pending || return 130
  for ((i = 0; i < ${#PROBE_URLS[@]}; i++)); do
    attempt=1
    while (( attempt <= HEALTHCHECK_RETRIES )); do
      check_pending || return 130
      code="$("$CURL_BIN" -s -o /dev/null -w '%{http_code}' --max-time "$HEALTHCHECK_TIMEOUT" "${PROBE_URLS[$i]}" 2>/dev/null || true)"
      check_pending || return 130
      if [[ "$code" == "${PROBE_STATUS[$i]}" ]]; then break; fi
      log "probe ${PROBE_URLS[$i]} got ${code}, want ${PROBE_STATUS[$i]} (${attempt}/${HEALTHCHECK_RETRIES})"
      if (( attempt == HEALTHCHECK_RETRIES )); then return 1; fi
      sleep "$HEALTHCHECK_INTERVAL"
      attempt=$((attempt + 1))
    done
  done
  return 0
}

promote() {
  local sha="$1" source="$2"
  mkdir -p "$STATE_DIR" || return 1
  cp -- "$source" "${STAGING_PREFIX}.manifest" || return 1
  printf '%s\n' "$sha" > "${STAGING_PREFIX}.sha" || return 1
  {
    printf '%s\n' "$sha"
    cat -- "$source"
  } > "${STAGING_PREFIX}.release" || return 1
  mv -f -- "${STAGING_PREFIX}.manifest" "$GOOD_MANIFEST_FILE" || return 1
  mv -f -- "${STAGING_PREFIX}.sha" "$GOOD_SHA_FILE" || return 1
  # last_good_release is the canonical atomic pair (SHA + complete manifest).
  # The two legacy files remain for operator visibility/backward compatibility.
  mv -f -- "${STAGING_PREFIX}.release" "$GOOD_RELEASE_FILE" || return 1
}

deploy_group() {
  local sha="$1" manifest="$2"
  load_manifest "$manifest" || return 1
  check_pending || return 130
  pull_and_retag "$sha" || return 1
  compose_release "$sha" || return 1
}

do_release() {
  local previous_sha="" previous_manifest="" current_rc=0 rollback_rc=0 first_line=1 line
  mkdir -p "$STATE_DIR" || return 1
  [[ -z "$PENDING_SIGNAL" ]] || return 130
  if [[ -f "$GOOD_RELEASE_FILE" ]]; then
    previous_manifest="${STAGING_PREFIX}.previous"
    : > "$previous_manifest" || return 1
    while IFS= read -r line || [[ -n "$line" ]]; do
      if [[ "$first_line" -eq 1 ]]; then
        previous_sha="$line"
        first_line=0
      else
        printf '%s\n' "$line" >> "$previous_manifest" || return 1
      fi
    done < "$GOOD_RELEASE_FILE"
    if [[ ! "$previous_sha" =~ ^[0-9a-f]{12}$ ]]; then
      log "ignoring malformed previous release SHA" >&2
      previous_sha=""
      previous_manifest=""
    fi
  elif [[ -f "$GOOD_SHA_FILE" && -f "$GOOD_MANIFEST_FILE" ]]; then
    previous_sha="$(<"$GOOD_SHA_FILE")"
    previous_manifest="$GOOD_MANIFEST_FILE"
    [[ "$previous_sha" =~ ^[0-9a-f]{12}$ ]] || previous_sha=""
  fi

  deploy_group "$D3_RELEASE_TAG" "$RELEASE_MANIFEST" || current_rc=$?
  if (( current_rc != 0 )) || [[ -n "$PENDING_SIGNAL" ]]; then
    log "new release failed before health gate"
  else
    if probe_release && check_pending; then
      # Ignore a second signal for the short canonical commit: once promotion
      # starts, both SHA and manifest move as one protected release decision.
      trap ':' INT TERM
      promote "$D3_RELEASE_TAG" "$RELEASE_MANIFEST" || return 1
      log "release ${D3_RELEASE_TAG} healthy; promoted atomically"
      return 0
    fi
    log "release ${D3_RELEASE_TAG} probe gate failed"
    current_rc=1
  fi

  if [[ -n "$previous_sha" && -n "$previous_manifest" && -f "$previous_manifest" && "$previous_sha" != "$D3_RELEASE_TAG" ]]; then
    log "rolling back complete image group to ${previous_sha}"
    # Once rollback starts, a second signal must not interrupt the group
    # transition or leave the host on a half-staged release.
    ROLLBACK_MODE=1
    trap ':' INT TERM
    deploy_group "$previous_sha" "$previous_manifest" || rollback_rc=$?
    if (( rollback_rc == 0 )); then
      if probe_release; then
        log "rollback to ${previous_sha} healthy"
      else
        log "rollback compose succeeded but probes still fail" >&2
        rollback_rc=1
      fi
    else
      log "rollback failed; last_good remains ${previous_sha}" >&2
    fi
  else
    log "no previous good release available; refusing pseudo-rollback" >&2
  fi
  [[ -n "$PENDING_SIGNAL" ]] && return 130
  return 1
}

mkdir -p "$(dirname "$HOST_LOCK")" 2>/dev/null || true
exec 9>"$HOST_LOCK"
flock 9 || exit 1
LOCK_HELD=1
do_release
