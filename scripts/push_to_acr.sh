#!/bin/bash
# Build + push to ACR with an IMMUTABLE git-SHA tag (build-deploy.yml internals).
#
# Hardened over the docker-package skill's push_to_acr.sh: the image is tagged
# with the git SHA (not just :latest) so deploys and rollbacks pin an exact,
# immutable artifact.
set -euo pipefail

: "${ACR_REGISTRY:?ACR_REGISTRY required}"
: "${ACR_NAMESPACE:?ACR_NAMESPACE required}"
: "${IMAGE_NAME:?IMAGE_NAME required}"
: "${GIT_SHA:?GIT_SHA required}"

BUILD_CONTEXT="${BUILD_CONTEXT:-.}"
DOCKERFILE="${DOCKERFILE:-Dockerfile}"
DOCKER_BIN="${DOCKER_BIN:-docker}"
ACR_IMAGE="${ACR_REGISTRY}/${ACR_NAMESPACE}/${IMAGE_NAME}"

echo "[push] building ${ACR_IMAGE}:${GIT_SHA}"
"$DOCKER_BIN" build \
  --build-arg "GIT_SHA=${GIT_SHA}" \
  -f "${BUILD_CONTEXT}/${DOCKERFILE}" \
  -t "${ACR_IMAGE}:${GIT_SHA}" \
  -t "${ACR_IMAGE}:latest" \
  "${BUILD_CONTEXT}"

echo "[push] pushing immutable tag ${ACR_IMAGE}:${GIT_SHA}"
"$DOCKER_BIN" push "${ACR_IMAGE}:${GIT_SHA}"
echo "[push] pushing :latest convenience tag"
"$DOCKER_BIN" push "${ACR_IMAGE}:latest"

echo "[push] done: ${ACR_IMAGE}:${GIT_SHA}"
