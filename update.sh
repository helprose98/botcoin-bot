#!/bin/bash
# ── BotCoin host update script ────────────────────────────────────────────────
# Runs on the HOST (never inside a container). Orchestrates a safe, idempotent,
# self-healing update of the two-container bot stack.
#
# Triggered by either:
#   - the dashboard "Update Bot" button, which makes the botapi container write
#     $REPO_DIR/data/update.trigger (see botapi/api.py /api/update), OR
#   - a manual run from SSH: bash /root/kraken-btc-bot/update.sh
#
# Watched by: /etc/cron.d/botcoin-update (installed by install-update-watcher.sh),
# which invokes this script once per minute whenever the trigger file exists.
#
# Safety properties (why this script is the way it is — see v2.2.1 incident):
#   1. NEVER leaves the bot offline with no recovery. We BUILD the new images
#      while the old containers are still running, and only recreate containers
#      once the build succeeds. If the build fails, the old stack is untouched.
#   2. Idempotent. If local VERSION already matches remote, we exit early — a
#      re-click on an already-current bot is a no-op, not a destructive cycle.
#   3. Observable. Every step appends a timestamped line to logs/update.log.
#   4. Self-healing. After recreating containers we poll /api/health for up to
#      60s. If it never goes healthy, we roll back to the previous git commit,
#      rebuild, and bring the old version back up — then log a LOUD failure.
#   5. Single-flight. A lock file prevents two overlapping cron invocations from
#      racing on the same git tree / docker stack.
# ──────────────────────────────────────────────────────────────────────────────

set -uo pipefail

REPO_DIR="/root/kraken-btc-bot"
DATA_DIR="$REPO_DIR/data"
LOG_DIR="$REPO_DIR/logs"
LOG="$LOG_DIR/update.log"
TRIGGER="$DATA_DIR/update.trigger"
LOCK="$DATA_DIR/update.lock"
# Machine-readable status, written to data/ (which the botapi container mounts)
# so the dashboard can poll /api/update-status and surface progress/failure.
STATUS="$DATA_DIR/update.status"

# Health check tuning. The botapi container publishes /api/health on host port
# 8081 (see docker-compose.yml). 60s budget at 3s intervals = up to 20 polls.
HEALTH_URL="http://localhost:8081/api/health"
HEALTH_TIMEOUT_SECS=60
HEALTH_INTERVAL_SECS=3

# Keep the log from growing unbounded. Rotate once it crosses ~1 MB; retain a
# single .1 backup. Simple, dependency-free, good enough for a single-user box.
LOG_MAX_BYTES=1048576

# Populated during the run; referenced by the rollback helper.
PREVIOUS_SHA=""
LOCAL_VERSION=""
NEW_VERSION=""

# ── Logging helpers ───────────────────────────────────────────────────────────

# Append a timestamped line to the update log (and stdout, so a manual SSH run
# shows progress live).
log() {
  local line="[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"
  echo "$line" | tee -a "$LOG"
}

# Write a single-line JSON status the dashboard can poll via /api/update-status.
# Args: <state> <message>. state ∈ running|success|failed|rolled_back|manual.
# Kept dependency-free (no jq); message is escaped for the few JSON-hostile
# characters we might emit (backslash, double-quote).
set_status() {
  local state="$1"; shift
  local msg="$*"
  msg="${msg//\\/\\\\}"
  msg="${msg//\"/\\\"}"
  printf '{"state":"%s","message":"%s","version":"%s","ts":"%s"}\n' \
    "$state" "$msg" "${NEW_VERSION:-$LOCAL_VERSION}" \
    "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" > "$STATUS"
}

# Rotate the log if it has grown past LOG_MAX_BYTES.
rotate_log_if_needed() {
  if [ -f "$LOG" ]; then
    local size
    size=$(wc -c < "$LOG" 2>/dev/null || echo 0)
    if [ "$size" -gt "$LOG_MAX_BYTES" ]; then
      mv "$LOG" "$LOG.1"
    fi
  fi
}

# Poll /api/health until it returns HTTP 200 or the timeout elapses.
# Returns 0 if healthy within the budget, 1 otherwise.
wait_for_health() {
  local deadline=$(( SECONDS + HEALTH_TIMEOUT_SECS ))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if curl -sf --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; then
      return 0
    fi
    sleep "$HEALTH_INTERVAL_SECS"
  done
  return 1
}

# Roll the git tree + containers back to PREVIOUS_SHA and bring them up.
# Shared by both the `up` failure path and the health-check failure path.
rollback_to_previous() {
  if [ -z "$PREVIOUS_SHA" ]; then
    log "ROLLBACK SKIPPED: no previous commit recorded. MANUAL RECOVERY REQUIRED."
    log "  Run: cd $REPO_DIR && docker compose up -d --build"
    return 1
  fi
  log "ROLLING BACK to previous commit $PREVIOUS_SHA ($LOCAL_VERSION)..."
  git reset --hard "$PREVIOUS_SHA" >>"$LOG" 2>&1
  docker compose build >>"$LOG" 2>&1
  docker compose up -d --remove-orphans >>"$LOG" 2>&1
}

# ── Preconditions ─────────────────────────────────────────────────────────────

mkdir -p "$DATA_DIR" "$LOG_DIR"
rotate_log_if_needed

cd "$REPO_DIR" || { echo "[update] FATAL: cannot cd to $REPO_DIR"; exit 1; }

# Single-flight: acquire the lock or exit. flock holds the lock for the lifetime
# of this process; it is released automatically when the script exits.
exec 9>"$LOCK"
if ! flock -n 9; then
  log "Another update is already in progress (lock held). Skipping this run."
  exit 0
fi

# Always clear the trigger up front so a failed run can't loop every minute.
# A genuine retry is an explicit re-click (or manual run), not an infinite cron
# storm.
rm -f "$TRIGGER"

# ── Idempotency guard: only rebuild if the remote VERSION actually differs ─────

LOCAL_VERSION="$(cat VERSION 2>/dev/null || echo '0.0.0')"
REMOTE_VERSION="$(curl -sf https://raw.githubusercontent.com/helprose98/botcoin-bot/main/VERSION 2>/dev/null || echo "$LOCAL_VERSION")"
REMOTE_VERSION="${REMOTE_VERSION:-$LOCAL_VERSION}"

log "Update requested. Local VERSION=$LOCAL_VERSION, remote VERSION=$REMOTE_VERSION."
set_status "running" "Update requested; checking for new version."

if [ "$LOCAL_VERSION" = "$REMOTE_VERSION" ]; then
  log "Already up to date ($LOCAL_VERSION). No-op."
  set_status "success" "Already up to date on $LOCAL_VERSION."
  exit 0
fi

# Record the commit we are on now, so we can roll back to it if the new version
# fails its health check. Captured BEFORE we pull.
PREVIOUS_SHA="$(git rev-parse HEAD 2>/dev/null || echo '')"
log "Current commit before update: ${PREVIOUS_SHA:-unknown}"

# ── Pull the new code ──────────────────────────────────────────────────────────

log "Fetching latest code from GitHub (origin/main)..."
if ! git fetch origin main >>"$LOG" 2>&1; then
  log "FAILURE: git fetch failed. Stack untouched, bot still running on $LOCAL_VERSION."
  set_status "failed" "git fetch failed; bot still running $LOCAL_VERSION."
  exit 1
fi

TARGET_SHA="$(git rev-parse origin/main 2>/dev/null || echo '')"
log "Target commit: ${TARGET_SHA:-unknown}"

# Move the working tree to the new release. reset --hard is safe here: the repo
# dir holds only tracked source; runtime state lives in data/ and logs/, which
# are gitignored and bind-mounted, so they are never touched by this.
if ! git reset --hard origin/main >>"$LOG" 2>&1; then
  log "FAILURE: git reset to origin/main failed. Stack untouched, bot still running on $LOCAL_VERSION."
  set_status "failed" "git checkout failed; bot still running $LOCAL_VERSION."
  exit 1
fi

NEW_VERSION="$(cat VERSION 2>/dev/null || echo "$REMOTE_VERSION")"
log "Code now at VERSION=$NEW_VERSION (commit ${TARGET_SHA:-unknown})."

# ── Build FIRST (old containers keep running during the build) ─────────────────
# This is the core safety inversion vs the v2.2.0 script, which ran
# `docker compose down` BEFORE building. A build failure there left ZERO
# containers running. By building first, a failed build leaves the OLD stack
# fully intact and serving.

log "Building new images (old containers still running)..."
set_status "running" "Building $NEW_VERSION (bot stays online during build)."
if ! docker compose build >>"$LOG" 2>&1; then
  log "FAILURE: docker compose build failed. Rolling code back to ${PREVIOUS_SHA:-previous}."
  if [ -n "$PREVIOUS_SHA" ]; then
    git reset --hard "$PREVIOUS_SHA" >>"$LOG" 2>&1
  fi
  log "Old stack ($LOCAL_VERSION) is untouched and still running. No downtime incurred."
  set_status "failed" "Build of $NEW_VERSION failed; bot still running $LOCAL_VERSION (no downtime)."
  exit 1
fi

# ── Recreate containers with the freshly built images ──────────────────────────
# `up -d` recreates only the containers whose image/config changed, with a brief
# swap. We deliberately do NOT run a separate `down` first — that is what created
# the zero-container window in the incident. --remove-orphans cleans up any stale
# services left over from older compose files.

log "Bringing up new containers (docker compose up -d --remove-orphans)..."
set_status "running" "Restarting containers onto $NEW_VERSION."
if ! docker compose up -d --remove-orphans >>"$LOG" 2>&1; then
  log "FAILURE: docker compose up failed. Attempting rollback to ${PREVIOUS_SHA:-previous}."
  rollback_to_previous
  set_status "rolled_back" "Restart of $NEW_VERSION failed; rolled back to $LOCAL_VERSION."
  exit 1
fi

# ── Health check: poll /api/health until healthy or timeout ────────────────────

log "Waiting up to ${HEALTH_TIMEOUT_SECS}s for $HEALTH_URL to report healthy..."
if wait_for_health; then
  log "SUCCESS: bot is healthy on VERSION=$NEW_VERSION (commit ${TARGET_SHA:-unknown})."
  set_status "success" "Updated to $NEW_VERSION and healthy."
  exit 0
fi

# Health never came up — the new version is bad. Roll back to the last known
# good commit and rebuild, so the box is never left dark.
log "FAILURE: bot did NOT become healthy within ${HEALTH_TIMEOUT_SECS}s on VERSION=$NEW_VERSION."
rollback_to_previous

log "Verifying rolled-back stack health..."
if wait_for_health; then
  log "RECOVERED: rolled back to $LOCAL_VERSION (commit ${PREVIOUS_SHA:-unknown}) and it is healthy."
  log "The new version $NEW_VERSION was rejected. Investigate before retrying."
  set_status "rolled_back" "$NEW_VERSION was unhealthy; rolled back to $LOCAL_VERSION (healthy)."
  exit 1
fi

# Both the new version AND the rollback failed health. This is the loud, last
# resort. Leave a breadcrumb file so the dashboard/Andy can see manual recovery
# is needed, and tell exactly what to run.
log "CRITICAL: rollback ALSO failed health check. MANUAL RECOVERY REQUIRED."
log "  SSH to the box and run:"
log "    cd $REPO_DIR && docker compose up -d --build && docker compose logs --tail 50"
set_status "manual" "Both $NEW_VERSION and rollback to $LOCAL_VERSION failed health. Manual SSH recovery required."
echo "manual-recovery-needed version=$NEW_VERSION at $(date -u '+%Y-%m-%dT%H:%M:%SZ')" > "$DATA_DIR/update.failed"
exit 2
