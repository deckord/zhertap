#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${LAND_SCOUT_ROOT:-/opt/land-scout/land-scout-bot}"
BRANCH="${LAND_SCOUT_PRODUCTION_BRANCH:-production}"
BACKUP_DIR="${LAND_SCOUT_BACKUP_DIR:-/opt/land-scout/backups}"
HEALTH_URL="${LAND_SCOUT_HEALTH_URL:-http://127.0.0.1:8000/health}"

cd "$ROOT"

if [[ ! -d .git ]]; then
  echo "Production checkout is not initialized as a Git repository" >&2
  exit 1
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "Tracked production files contain local changes; refusing to overwrite them" >&2
  git status --short >&2
  exit 1
fi

git fetch --quiet --prune origin "$BRANCH"
current_revision="$(git rev-parse HEAD)"
target_revision="$(git rev-parse "origin/$BRANCH")"
deployed_revision="$(cat .codex_deployed_sha 2>/dev/null || true)"

if [[ "$current_revision" == "$target_revision" && "$deployed_revision" == "$target_revision" ]]; then
  exit 0
fi

if [[ "$current_revision" != "$target_revision" ]]; then
  if ! git merge-base --is-ancestor "$current_revision" "$target_revision"; then
    echo "Production update is not a fast-forward; manual review is required" >&2
    exit 1
  fi

  mkdir -p "$BACKUP_DIR"
  backup_path="$BACKUP_DIR/land_scout_$(date -u +%Y%m%d_%H%M%S)_${current_revision:0:8}.sql"
  docker compose exec -T db pg_dump -U land_scout -d land_scout > "$backup_path"
  chmod 600 "$backup_path"

  git merge --ff-only "origin/$BRANCH"
fi

services=(web worker auction_worker beat monitor bot)
docker compose build "${services[@]}"
docker compose run --rm -T --no-deps web python -m alembic upgrade heads
docker compose up -d --force-recreate "${services[@]}"

healthy=0
for _attempt in $(seq 1 30); do
  if curl --fail --silent --show-error --max-time 5 "$HEALTH_URL" >/dev/null; then
    healthy=1
    break
  fi
  sleep 4
done

if [[ "$healthy" -ne 1 ]]; then
  echo "Production health check failed after deployment" >&2
  docker compose ps >&2
  docker compose logs --tail=120 web >&2
  exit 1
fi

printf '%s\n' "$target_revision" > .codex_deployed_sha
docker compose ps
echo "Production deployed: $target_revision"
