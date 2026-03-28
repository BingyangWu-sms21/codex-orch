#!/usr/bin/env bash
set -euo pipefail

if ! command -v codex-orch >/dev/null 2>&1; then
  echo "codex-orch is required in PATH" >&2
  exit 127
fi

exec codex-orch assistant request create "$@"
