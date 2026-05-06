#!/usr/bin/env bash
# Polls docker compose healthchecks until all named services report healthy.
set -euo pipefail

SERVICES=("$@")
[[ ${#SERVICES[@]} -gt 0 ]] || { echo "usage: $0 svc1 [svc2 ...]"; exit 2; }

DEADLINE=$(( SECONDS + 90 ))

state() {
  docker compose ps --format '{{.Service}} {{.Health}}' \
    | awk -v s="$1" '$1==s {print $2}'
}

while (( SECONDS < DEADLINE )); do
  pending=()
  for svc in "${SERVICES[@]}"; do
    h=$(state "$svc" || true)
    if [[ "$h" != "healthy" ]]; then
      pending+=("$svc=${h:-unknown}")
    fi
  done

  if [[ ${#pending[@]} -eq 0 ]]; then
    echo "[health] all healthy: ${SERVICES[*]}"
    exit 0
  fi

  printf '[health] waiting: %s\n' "${pending[*]}"
  sleep 2
done

echo "[health] timeout after 90s. final status:" >&2
docker compose ps >&2
exit 1
