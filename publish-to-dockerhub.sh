#!/usr/bin/env bash
set -Eeuo pipefail

: "${DOCKERHUB_USERNAME:?Set DOCKERHUB_USERNAME first}"
if [[ -z "${DOCKERHUB_TOKEN:-}" ]]; then
  read -rsp 'Docker Hub write token: ' DOCKERHUB_TOKEN
  printf '\n'
fi
if [[ -z "${DOCKERHUB_TOKEN}" ]]; then
  printf 'Docker Hub token cannot be empty.\n' >&2
  exit 1
fi

REPOSITORY="${DOCKERHUB_REPOSITORY:-h264-timestamp-repair}"
VERSION="${IMAGE_VERSION:-1.0.0}"
IMAGE="docker.io/${DOCKERHUB_USERNAME}/${REPOSITORY}"

printf '%s' "${DOCKERHUB_TOKEN}" | docker login --username "${DOCKERHUB_USERNAME}" --password-stdin

docker buildx inspect h264-timestamp-builder >/dev/null 2>&1 \
  || docker buildx create --name h264-timestamp-builder --use
docker buildx use h264-timestamp-builder
docker buildx inspect --bootstrap

docker buildx build \
  --platform linux/amd64,linux/arm64 \
  --tag "${IMAGE}:${VERSION}" \
  --tag "${IMAGE}:latest" \
  --provenance=true \
  --sbom=true \
  --push \
  .

docker logout
unset DOCKERHUB_TOKEN
printf 'Published %s:%s and %s:latest\n' "${IMAGE}" "${VERSION}" "${IMAGE}"
