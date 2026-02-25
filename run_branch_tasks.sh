#!/usr/bin/env bash
set -euo pipefail

if [[ ${#} -lt 1 ]]; then
  branch=$(git rev-parse --abbrev-ref HEAD)
  echo "No branch provided. Using current branch: ${branch}"
else
  branch=${1}
fi

# Directorio del repo actual (donde se ejecuta el script)
REPO_DIR="$(pwd)"

echo "Running chunking-get for branch '${branch}' on repo '${REPO_DIR}'..."
chunking-get "${branch}" --repo "${REPO_DIR}" --output "${REPO_DIR}/chunks"

echo "Running chunking-ingest for branch '${branch}'..."
chunking-ingest "${branch}" --repo "${REPO_DIR}" --chunks-dir "${REPO_DIR}/chunks"

echo "All tasks completed successfully."