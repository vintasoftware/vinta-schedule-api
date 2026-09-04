#!/usr/bin/env bash
#
# Move an already-applied `storage` stack under the `module.storage` path used by
# the single per-environment stack (infrastructure/modules/environment).
#
# Why: `storage` and `app` used to be separate root modules with one Scalr
# workspace each. The environment now has one stack and one workspace, and for
# staging that workspace is the one the storage-only stack already used
# (VintaScheduleStaging). Its state therefore holds addresses like
# `aws_s3_bucket.media`, which the composed module knows as
# `module.storage.aws_s3_bucket.media`.
#
# `terraform state mv` only rewrites addresses -- nothing is created, destroyed
# or touched in AWS. Running this on an already-migrated (or empty) state is a
# no-op, so it is safe to re-run.
#
# Usage:
#   export SCALR_HOSTNAME=... SCALR_ENVIRONMENT=...
#   terraform login "$SCALR_HOSTNAME"
#
#   infrastructure/scripts/migrate-storage-state.sh                     # dry run, staging
#   infrastructure/scripts/migrate-storage-state.sh --apply             # do it
#   infrastructure/scripts/migrate-storage-state.sh --env production    # dry run, production
#
# After it succeeds, `terragrunt plan` in the environment folder should show the
# new app-platform resources being created and NO changes to the buckets, the
# CloudFront distributions, the signing key or the storage IAM user. If it wants
# to replace any of those, stop and restore the backup this script writes.

set -euo pipefail

ENVIRONMENT=staging
APPLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --env) ENVIRONMENT="${2:?--env needs a value}"; shift 2 ;;
    -h|--help) sed -n '2,30p' "$0" | sed 's/^#\{1,2\} \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_DIR="$SCRIPT_DIR/../environments/$ENVIRONMENT"

if [[ ! -f "$ENV_DIR/terragrunt.hcl" ]]; then
  echo "no such environment: $ENVIRONMENT (looked in $ENV_DIR)" >&2
  exit 2
fi

command -v terragrunt >/dev/null || { echo "terragrunt is not on PATH" >&2; exit 2; }

cd "$ENV_DIR"

WORKSPACE="$(grep -E '^\s*scalr_workspace\s*=' env.hcl | sed -E 's/.*"(.*)".*/\1/')"

echo "environment : $ENVIRONMENT"
echo "workspace   : $WORKSPACE"
echo "mode        : $([[ $APPLY -eq 1 ]] && echo APPLY || echo 'DRY RUN (pass --apply to move)')"
echo

# Production is the one case where a mistake here is expensive, so make the
# operator name the workspace out loud.
if [[ $APPLY -eq 1 && "$ENVIRONMENT" == "production" ]]; then
  read -r -p "Type the workspace name to confirm you mean production: " typed
  [[ "$typed" == "$WORKSPACE" ]] || { echo "mismatch -- aborting." >&2; exit 1; }
fi

echo "==> terragrunt init"
terragrunt run -- init -input=false >/dev/null

echo "==> reading state"
ADDRESSES="$(terragrunt run -- state list 2>/dev/null \
  | grep -E '^(data\.)?[A-Za-z0-9_]+\.[^ ]+$' || true)"

if [[ -z "$ADDRESSES" ]]; then
  echo "state holds no resources -- nothing to migrate."
  exit 0
fi

TO_MOVE="$(echo "$ADDRESSES" | grep -v '^module\.' || true)"

if [[ -z "$TO_MOVE" ]]; then
  echo "every address is already inside a module -- nothing to migrate."
  exit 0
fi

COUNT="$(echo "$TO_MOVE" | wc -l | tr -d ' ')"
echo "$COUNT address(es) to move under module.storage:"
echo "$TO_MOVE" | sed 's/^/  /'
echo

if [[ $APPLY -eq 0 ]]; then
  echo "Dry run -- these are the moves that would run:"
  while IFS= read -r addr; do
    echo "  terragrunt run -- state mv '$addr' 'module.storage.$addr'"
  done <<< "$TO_MOVE"
  exit 0
fi

BACKUP="$ENV_DIR/state-backup-$(date -u +%Y%m%dT%H%M%SZ).tfstate"
echo "==> backing state up to $BACKUP"
terragrunt run -- state pull > "$BACKUP" 2>/dev/null
if [[ "$(head -c 1 "$BACKUP")" != "{" ]]; then
  echo "state pull did not produce JSON -- aborting before touching anything." >&2
  exit 1
fi
echo "    $(wc -c < "$BACKUP" | tr -d ' ') bytes. Restore with:"
echo "    terragrunt run -- state push '$BACKUP'"
echo

# One round trip to Scalr per move, so this takes a couple of minutes.
while IFS= read -r addr; do
  echo "==> $addr -> module.storage.$addr"
  terragrunt run -- state mv "$addr" "module.storage.$addr"
done <<< "$TO_MOVE"

echo
echo "Done. Now run:  cd $ENV_DIR && terragrunt plan"
echo "Expect: app-platform resources created, storage resources unchanged."
