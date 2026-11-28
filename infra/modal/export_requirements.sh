#!/usr/bin/env sh
# Regenerate the pinned requirement lists used by the Modal images (run after uv.lock changes).
set -e
cd "$(dirname "$0")/../.."
uv export --package vball-worker --no-hashes --no-emit-workspace --no-dev --frozen \
  | grep -v '^torch==' > infra/modal/requirements-worker.txt   # CUDA torch is installed separately
uv export --package vball-api --no-hashes --no-emit-workspace --no-dev --frozen \
  > infra/modal/requirements-api.txt
