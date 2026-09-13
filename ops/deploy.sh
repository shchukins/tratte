#!/usr/bin/env bash
set -Eeuo pipefail

readonly APP_DIR="/srv/tratte"
readonly DEPLOY_REF="refs/remotes/origin/deploy"
readonly STATE_FILE="${APP_DIR}/.deploy-revision"
readonly HEALTH_URL="http://127.0.0.1:8000/health"

log() {
    printf '%s %s\n' "$(date --iso-8601=seconds)" "$*"
}

exec 9>"${APP_DIR}/.deploy.lock"
if ! flock --nonblock 9; then
    log "Another deployment is already running"
    exit 0
fi

cd "${APP_DIR}"

if [[ -n "$(git status --porcelain)" ]]; then
    log "Refusing to deploy: the production checkout has local changes"
    git status --short
    exit 1
fi

log "Fetching the last revision that passed CI"
git fetch --prune origin deploy
target_revision="$(git rev-parse "${DEPLOY_REF}")"
deployed_revision="$(cat "${STATE_FILE}" 2>/dev/null || true)"

if [[ "${target_revision}" == "${deployed_revision}" ]]; then
    log "Revision ${target_revision:0:7} is already deployed"
    exit 0
fi

current_revision="$(git rev-parse HEAD)"
if ! git merge-base --is-ancestor "${current_revision}" "${target_revision}"; then
    log "Refusing non-fast-forward deployment: ${current_revision:0:7} -> ${target_revision:0:7}"
    exit 1
fi

log "Deploying ${current_revision:0:7} -> ${target_revision:0:7}"
git merge --ff-only "${DEPLOY_REF}"

docker compose build migrate api bot scheduler
docker compose run --rm --no-deps migrate
docker compose up -d --no-deps api bot scheduler

for attempt in {1..12}; do
    if curl --fail --silent --show-error "${HEALTH_URL}" >/dev/null; then
        printf '%s\n' "${target_revision}" >"${STATE_FILE}"
        log "Deployment ${target_revision:0:7} completed successfully"
        docker compose ps api bot scheduler
        exit 0
    fi
    log "Health check ${attempt}/12 failed; retrying in 5 seconds"
    sleep 5
done

log "Deployment failed: API health check did not recover"
docker compose ps api bot scheduler || true
docker compose logs --tail=80 api bot scheduler || true
exit 1
