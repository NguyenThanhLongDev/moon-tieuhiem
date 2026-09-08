#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

if ! command -v grapuco >/dev/null 2>&1; then
  echo "grapuco CLI is not installed."
  exit 1
fi

if [[ -z "${GRAPUCO_API_KEY:-}" ]]; then
  echo "Missing GRAPUCO_API_KEY env var."
  echo "Example:"
  echo "  export GRAPUCO_API_KEY='your_key'"
  exit 1
fi

echo "[1/3] Re-ingest graph..."
grapuco ingest --all

echo "[2/3] Refresh impact report..."
python3 tools/grapuco_impact_refresh.py --project-root "$PROJECT_ROOT" "$@"

echo "[3/3] Done. Check outputs:"
echo "  - GRAPUCO_IMPACT_REPORT.json"
echo "  - GRAPUCO_IMPACT_SUMMARY.md"
