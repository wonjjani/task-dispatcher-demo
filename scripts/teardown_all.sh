#!/usr/bin/env bash

set -u

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/docker-compose.yml"

CONTAINERS=(
  dispatcher-locust
  dispatcher-worker-2
  dispatcher-worker-1
  dispatcher-api
  dispatcher-redis
)

log() {
  printf '[cleanup] %s\n' "$*"
}

run_quiet() {
  "$@" >/dev/null 2>&1
}

compose_down() {
  local engine="$1"

  if ! command -v "$engine" >/dev/null 2>&1; then
    return 0
  fi

  if [[ ! -f "$COMPOSE_FILE" ]]; then
    return 0
  fi

  log "Trying $engine compose down"
  run_quiet "$engine" compose -f "$COMPOSE_FILE" down -v --remove-orphans || \
    run_quiet "$engine" compose -f "$COMPOSE_FILE" down -v || \
    true
}

remove_named_containers() {
  local engine="$1"
  local name

  if ! command -v "$engine" >/dev/null 2>&1; then
    return 0
  fi

  for name in "${CONTAINERS[@]}"; do
    run_quiet "$engine" rm -f "$name" || true
  done
}

cleanup_podman_machine() {
  local remote_script

  if ! command -v podman >/dev/null 2>&1; then
    return 0
  fi

  if ! run_quiet podman machine ssh true; then
    return 0
  fi

  log "Trying podman machine cleanup"

  remote_script='
set +e
systemctl --user stop dispatcher-locust.service dispatcher-worker-2.service dispatcher-worker-1.service dispatcher-api.service dispatcher-redis.service >/dev/null 2>&1
podman rm -f dispatcher-locust dispatcher-worker-2 dispatcher-worker-1 dispatcher-api dispatcher-redis >/dev/null 2>&1
podman volume rm -f redis-data >/dev/null 2>&1
'

  podman machine ssh "$remote_script" >/dev/null 2>&1 || true
}

main() {
  log "Starting cleanup"

  compose_down docker
  compose_down podman

  remove_named_containers docker
  remove_named_containers podman

  cleanup_podman_machine

  log "Cleanup completed"
}

main "$@"
