#!/usr/bin/env bash
# Publish a second immutable tag for an already-built image without rebuilding.
set -euo pipefail

: "${SOURCE_IMAGE:?SOURCE_IMAGE required}"  # retained for audit/logging
: "${TARGET_IMAGE:?TARGET_IMAGE required}"
DOCKER_BIN="${DOCKER_BIN:-docker}"
TIMEOUT_BIN="${TIMEOUT_BIN:-timeout}"
PUSH_TIMEOUT_SECONDS="${PUSH_TIMEOUT_SECONDS:-300}"
PUSH_TIMEOUT_KILL_AFTER_SECONDS="${PUSH_TIMEOUT_KILL_AFTER_SECONDS:-15}"
PUSH_MAX_ATTEMPTS="${PUSH_MAX_ATTEMPTS:-3}"
PUSH_RETRY_DELAY_SECONDS="${PUSH_RETRY_DELAY_SECONDS:-10}"

is_pos() { [[ "$1" =~ ^[1-9][0-9]*$ ]]; }
is_nonneg() { [[ "$1" =~ ^[0-9]+$ ]]; }
is_pos "$PUSH_TIMEOUT_SECONDS" && [ "$PUSH_TIMEOUT_SECONDS" -le 300 ] || {
  echo "PUSH_TIMEOUT_SECONDS must be 1..300" >&2; exit 2;
}
is_pos "$PUSH_TIMEOUT_KILL_AFTER_SECONDS" && [ "$PUSH_TIMEOUT_KILL_AFTER_SECONDS" -le 15 ] || {
  echo "PUSH_TIMEOUT_KILL_AFTER_SECONDS must be 1..15" >&2; exit 2;
}
is_pos "$PUSH_MAX_ATTEMPTS" && [ "$PUSH_MAX_ATTEMPTS" -le 3 ] || {
  echo "PUSH_MAX_ATTEMPTS must be 1..3" >&2; exit 2;
}
is_nonneg "$PUSH_RETRY_DELAY_SECONDS" || { echo "PUSH_RETRY_DELAY_SECONDS must be >=0" >&2; exit 2; }

attempt=1
while :; do
  echo "[alias-push] ${TARGET_IMAGE} (attempt ${attempt}/${PUSH_MAX_ATTEMPTS})"
  rc=0
  "$TIMEOUT_BIN" --kill-after="${PUSH_TIMEOUT_KILL_AFTER_SECONDS}s" \
    "${PUSH_TIMEOUT_SECONDS}s" "$DOCKER_BIN" push "$TARGET_IMAGE" || rc=$?
  if [ "$rc" -eq 0 ]; then exit 0; fi
  if [ "$attempt" -ge "$PUSH_MAX_ATTEMPTS" ]; then
    echo "alias push ${TARGET_IMAGE} failed after ${PUSH_MAX_ATTEMPTS} attempts" >&2
    exit 1
  fi
  attempt=$((attempt + 1))
  sleep "$PUSH_RETRY_DELAY_SECONDS"
done

