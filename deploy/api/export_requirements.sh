#!/usr/bin/env sh
# Regenerate the API's pinned requirements for Vercel (run after uv.lock changes).
set -e
cd "$(dirname "$0")/../.."
uv export --package vball-api --no-hashes --no-emit-workspace --no-dev --frozen \
  > deploy/api/requirements.txt
