#!/bin/bash
# Build + push to ACR with an IMMUTABLE git-SHA tag (build-deploy.yml internals).
#
# Hardened over the docker-package skill's push_to_acr.sh: the image is tagged
# with the git SHA so deploys and rollbacks pin an exact, immutable artifact.
set -euo pipefail

: "${ACR_REGISTRY:?ACR_REGISTRY required}"
: "${ACR_NAMESPACE:?ACR_NAMESPACE required}"
: "${IMAGE_NAME:?IMAGE_NAME required}"
: "${GIT_SHA:?GIT_SHA required}"

BUILD_CONTEXT="${BUILD_CONTEXT:-.}"
DOCKERFILE="${DOCKERFILE:-Dockerfile}"
DOCKER_BIN="${DOCKER_BIN:-docker}"
ACR_IMAGE="${ACR_REGISTRY}/${ACR_NAMESPACE}/${IMAGE_NAME}"
PUSH_TIMEOUT_SECONDS="${PUSH_TIMEOUT_SECONDS:-300}"
PUSH_TIMEOUT_KILL_AFTER_SECONDS="${PUSH_TIMEOUT_KILL_AFTER_SECONDS:-15}"
PUSH_MAX_ATTEMPTS="${PUSH_MAX_ATTEMPTS:-3}"
PUSH_RETRY_DELAY_SECONDS="${PUSH_RETRY_DELAY_SECONDS:-10}"

is_positive_integer() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

is_non_negative_integer() {
  [[ "$1" =~ ^[0-9]+$ ]]
}

if ! is_positive_integer "$PUSH_TIMEOUT_SECONDS"; then
  echo "PUSH_TIMEOUT_SECONDS must be a positive integer, got: $PUSH_TIMEOUT_SECONDS" >&2
  exit 2
fi
if [ "$PUSH_TIMEOUT_SECONDS" -gt 300 ]; then
  echo "PUSH_TIMEOUT_SECONDS must not exceed 300, got: $PUSH_TIMEOUT_SECONDS" >&2
  exit 2
fi
if ! is_positive_integer "$PUSH_TIMEOUT_KILL_AFTER_SECONDS"; then
  echo "PUSH_TIMEOUT_KILL_AFTER_SECONDS must be a positive integer, got: $PUSH_TIMEOUT_KILL_AFTER_SECONDS" >&2
  exit 2
fi
if [ "$PUSH_TIMEOUT_KILL_AFTER_SECONDS" -gt 15 ]; then
  echo "PUSH_TIMEOUT_KILL_AFTER_SECONDS must not exceed 15, got: $PUSH_TIMEOUT_KILL_AFTER_SECONDS" >&2
  exit 2
fi
if ! is_positive_integer "$PUSH_MAX_ATTEMPTS"; then
  echo "PUSH_MAX_ATTEMPTS must be a positive integer, got: $PUSH_MAX_ATTEMPTS" >&2
  exit 2
fi
if [ "$PUSH_MAX_ATTEMPTS" -gt 3 ]; then
  echo "PUSH_MAX_ATTEMPTS must not exceed 3, got: $PUSH_MAX_ATTEMPTS" >&2
  exit 2
fi
if ! is_non_negative_integer "$PUSH_RETRY_DELAY_SECONDS"; then
  echo "PUSH_RETRY_DELAY_SECONDS must be a non-negative integer, got: $PUSH_RETRY_DELAY_SECONDS" >&2
  exit 2
fi

# An immutable SHA push is safe to retry: it always points to the same image
# bytes. Bound every attempt so a stalled blob upload cannot keep a deployment
# job silent for tens of minutes. Do not publish a mutable registry `latest`:
# the deploy host retags the verified SHA locally for compose compatibility.
push_with_retry() {
  local tag="$1"
  local max_attempts="$2"
  local attempt=1 rc=0

  while true; do
    echo "[push] pushing ${tag} (attempt ${attempt}/${max_attempts}, timeout ${PUSH_TIMEOUT_SECONDS}s)"
    # Do not use timeout --foreground: it would leave docker's child processes
    # alive and holding the Actions log pipe after the client is terminated.
    if timeout --kill-after="${PUSH_TIMEOUT_KILL_AFTER_SECONDS}s" \
      "${PUSH_TIMEOUT_SECONDS}s" "$DOCKER_BIN" push "$tag"; then
      return 0
    else
      rc=$?
    fi

    if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
      echo "::warning::push ${tag} timed out after ${PUSH_TIMEOUT_SECONDS}s"
    else
      echo "::warning::push ${tag} failed (rc=${rc})"
    fi

    if [ "$attempt" -ge "$max_attempts" ]; then
      echo "::error::push ${tag} failed after ${max_attempts} attempts"
      return 1
    fi

    attempt=$((attempt + 1))
    echo "[push] retrying ${tag} in ${PUSH_RETRY_DELAY_SECONDS}s"
    sleep "$PUSH_RETRY_DELAY_SECONDS"
  done
}

echo "[push] building ${ACR_IMAGE}:${GIT_SHA}"
"$DOCKER_BIN" build \
  --build-arg "GIT_SHA=${GIT_SHA}" \
  -f "${BUILD_CONTEXT}/${DOCKERFILE}" \
  -t "${ACR_IMAGE}:${GIT_SHA}" \
  "${BUILD_CONTEXT}"

push_with_retry "${ACR_IMAGE}:${GIT_SHA}" "$PUSH_MAX_ATTEMPTS"

echo "[push] done: ${ACR_IMAGE}:${GIT_SHA}"
