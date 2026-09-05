#!/usr/bin/env bash
#
# Make sure the app secret holds every JSON key the task definitions read.
#
# ECS maps one key per env var (`arn:...:secret:<name>:KEY::`) and fails the
# whole task if a single one is absent:
#
#   ResourceInitializationError: unable to pull secrets or registry auth:
#   retrieved secret from Secrets Manager did not contain json key
#   MERCADOPAGO_ACCESS_TOKEN
#
# Terraform seeds every key on the first apply -- operator-owned ones as empty
# strings -- and then stops managing the value (`ignore_changes`), so a later
# hand-edit that pastes back a subset silently drops the rest. Adding a key to
# `extra_secret_keys` has the same effect: the task definitions start asking
# for it, but the existing secret version has never heard of it.
#
# This compares the secret against the `secrets` array of the deployed web task
# definition -- the exact set ECS resolves at task start -- then adds whatever
# is missing with an empty value, leaving every existing value untouched. Dry
# run by default; re-running once the keys are all present does nothing.
#
# It prints key NAMES only, never a value.
#
# Usage:
#   infrastructure/scripts/sync-app-secret-keys.sh                  # dry run, staging
#   infrastructure/scripts/sync-app-secret-keys.sh --apply
#   infrastructure/scripts/sync-app-secret-keys.sh --env production --apply

set -euo pipefail
umask 077

ENVIRONMENT=staging
APPLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --env) ENVIRONMENT="${2:?--env needs a value}"; shift 2 ;;
    -h|--help) sed -n '2,26p' "$0" | sed 's/^#\{1,2\} \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

for tool in aws jq; do
  command -v "$tool" >/dev/null || { echo "$tool is not on PATH" >&2; exit 2; }
done

INFRA="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_TG="$INFRA/environments/$ENVIRONMENT/terragrunt.hcl"

[[ -f "$ENV_TG" ]] || { echo "no such environment: $ENVIRONMENT" >&2; exit 2; }

PROJECT="$(grep -E '^\s*project_name\s*=' "$ENV_TG" | head -1 | sed -E 's/.*"(.*)".*/\1/')"
NAME_PREFIX="${PROJECT}-${ENVIRONMENT}"
SECRET_ID="${NAME_PREFIX}/app"

# Ask the deployed task definition rather than parsing the Terraform. Its
# `secrets` array IS what ECS will look for, so this stays correct across
# changes to how the module builds the list -- required vs optional keys,
# disabled_secret_keys, extra_secret_keys, or the next refactor.
REQUIRED="$(aws ecs describe-task-definition \
  --task-definition "${NAME_PREFIX}-web" \
  --query 'taskDefinition.containerDefinitions[0].secrets[].name' \
  --output text 2>/dev/null | tr '\t' '\n' | sort -u)"

if [[ -z "$REQUIRED" ]]; then
  echo "could not read the secrets list from task definition ${NAME_PREFIX}-web." >&2
  echo "Has the stack been applied in this environment yet?" >&2
  exit 2
fi

echo "environment : $ENVIRONMENT"
echo "secret      : $SECRET_ID"
echo "key list    : task definition ${NAME_PREFIX}-web"
echo "mode        : $([[ $APPLY -eq 1 ]] && echo APPLY || echo 'DRY RUN (pass --apply to add them)')"
echo

PRESENT="$(aws secretsmanager get-secret-value --secret-id "$SECRET_ID" \
  --query SecretString --output text | jq -r 'keys[]' | sort -u)"

MISSING="$(comm -23 <(echo "$REQUIRED") <(echo "$PRESENT") || true)"
EXTRA="$(comm -13 <(echo "$REQUIRED") <(echo "$PRESENT") || true)"

echo "$(echo "$REQUIRED" | wc -l | tr -d ' ') keys read by the containers, $(echo "$PRESENT" | wc -l | tr -d ' ') present in the secret"

if [[ -n "$EXTRA" ]]; then
  echo
  echo "In the secret but not read by any task definition (harmless, left alone):"
  echo "$EXTRA" | sed 's/^/  /'
fi

if [[ -z "$MISSING" ]]; then
  echo
  echo "Nothing missing -- every key the task definitions read is present."
  exit 0
fi

echo
echo "MISSING -- every ECS task fails to start until these exist:"
echo "$MISSING" | sed 's/^/  /'

if [[ $APPLY -eq 0 ]]; then
  echo
  echo "Dry run. Re-run with --apply to add them as empty strings."
  exit 0
fi

TMP="$(mktemp -t app-secret)"
trap 'rm -f "$TMP"' EXIT

# Read-modify-write: Secrets Manager replaces the whole document, so there is
# no way to add a key without sending the existing ones back with it.
aws secretsmanager get-secret-value --secret-id "$SECRET_ID" \
  --query SecretString --output text \
  | jq --arg keys "$MISSING" \
      'reduce ($keys | split("\n")[]) as $k (.; if has($k) then . else .[$k] = "" end)' \
  > "$TMP"

# Guard against writing a truncated document over a good one.
jq -e 'type == "object" and length > 0' "$TMP" >/dev/null \
  || { echo "refusing to write: the merged document is not a non-empty object" >&2; exit 1; }

aws secretsmanager put-secret-value --secret-id "$SECRET_ID" \
  --secret-string "file://$TMP" >/dev/null

echo
echo "Added $(echo "$MISSING" | wc -l | tr -d ' ') key(s) as empty strings."
echo "Existing values were preserved; the previous version is still in Secrets"
echo "Manager as AWSPREVIOUS if you need to compare."
echo
echo "ECS reads the secret when a task STARTS, so re-run the deploy (or"
echo "aws ecs update-service --force-new-deployment) to pick this up."
