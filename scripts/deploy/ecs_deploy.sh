#!/usr/bin/env bash
#
# Roll a freshly built image out to one ECS environment.
#
# Order matters and is the whole point of this script:
#   1. register a new task-definition revision per family, with the new image
#   2. run the release task (migrate + collectstatic) and WAIT for it
#   3. only if that exited 0, point the services at their new revisions
#
# A migration that fails therefore stops the deploy before a single container
# serving traffic has been replaced.
#
# Everything about the environment -- cluster name, service names, subnets,
# security groups -- is read from one SSM parameter written by Terraform
# (modules/app-platform/ecs.tf), so renaming a resource in Terraform does not
# require touching this script or the workflow.
#
# Usage: ecs_deploy.sh <ssm-parameter-name> <image-uri> <commit-sha>

set -euo pipefail

PARAMETER_NAME="${1:?usage: ecs_deploy.sh <ssm-parameter-name> <image-uri> <commit-sha>}"
IMAGE="${2:?missing image URI}"
COMMIT_SHA="${3:?missing commit sha}"

WORKDIR="$(mktemp -d)"
trap 'rm -rf -- "$WORKDIR"' EXIT

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

log "Reading deploy metadata from ${PARAMETER_NAME}"
aws ssm get-parameter --name "$PARAMETER_NAME" --query 'Parameter.Value' --output text \
  > "$WORKDIR/deploy.json"

CLUSTER=$(jq -r '.cluster' "$WORKDIR/deploy.json")
RELEASE_FAMILY=$(jq -r '.release_task_family' "$WORKDIR/deploy.json")
RELEASE_LOG_GROUP=$(jq -r '.release_log_group' "$WORKDIR/deploy.json")
NETWORK_CONFIG=$(jq -r '
  "awsvpcConfiguration={subnets=[" + (.subnets | join(",")) +
  "],securityGroups=[" + (.security_groups | join(",")) +
  "],assignPublicIp=DISABLED}"
' "$WORKDIR/deploy.json")

# Registers a copy of the family's current revision with the new image and commit
# SHA. Terraform owns everything else in the definition (cpu, memory, secrets,
# environment) and its services ignore `task_definition`, so the two never fight:
# Terraform decides the shape, this decides the tag.
register_revision() {
  local family="$1"
  local source="$WORKDIR/${family}.json"

  aws ecs describe-task-definition --task-definition "$family" --query 'taskDefinition' \
    > "$source"

  jq --arg image "$IMAGE" --arg sha "$COMMIT_SHA" '
      .containerDefinitions |= map(
        .image = $image
        # Sentry tags every event with the release it came from. Injected here
        # rather than in Terraform because only the deploy knows the SHA.
        | .environment = (
            [.environment[]? | select(.name != "COMMIT_SHA")]
            + [{name: "COMMIT_SHA", value: $sha}]
          )
      )
      # Fields the API returns but refuses to accept back.
      | del(
          .taskDefinitionArn, .revision, .status, .requiresAttributes,
          .compatibilities, .registeredAt, .registeredBy, .deregisteredAt
        )
    ' "$source" > "$WORKDIR/${family}-new.json"

  aws ecs register-task-definition \
    --cli-input-json "file://$WORKDIR/${family}-new.json" \
    --query 'taskDefinition.taskDefinitionArn' --output text
}

########################################
# 1. New revisions
########################################

log "Registering task definitions for ${IMAGE}"

RELEASE_TASK_DEF=$(register_revision "$RELEASE_FAMILY")
echo "  release -> $RELEASE_TASK_DEF"

SERVICE_COUNT=$(jq -r '.services | length' "$WORKDIR/deploy.json")
declare -a SERVICE_NAMES=()
declare -a SERVICE_TASK_DEFS=()

for index in $(seq 0 $((SERVICE_COUNT - 1))); do
  name=$(jq -r ".services[$index].name" "$WORKDIR/deploy.json")
  family=$(jq -r ".services[$index].family" "$WORKDIR/deploy.json")
  arn=$(register_revision "$family")
  SERVICE_NAMES+=("$name")
  SERVICE_TASK_DEFS+=("$arn")
  echo "  ${name} -> ${arn}"
done

########################################
# 2. Release task -- migrations gate the rollout
########################################

log "Running release task (migrate + collectstatic)"

TASK_ARN=$(aws ecs run-task \
  --cluster "$CLUSTER" \
  --task-definition "$RELEASE_TASK_DEF" \
  --launch-type FARGATE \
  --network-configuration "$NETWORK_CONFIG" \
  --started-by "github-actions-${COMMIT_SHA:0:12}" \
  --query 'tasks[0].taskArn' --output text)

echo "  task: $TASK_ARN"

# `wait tasks-stopped` polls for up to 10 minutes. A migration that outlasts that
# is not a deploy problem to paper over with a longer timeout -- run it by hand.
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK_ARN"

EXIT_CODE=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK_ARN" \
  --query 'tasks[0].containers[0].exitCode' --output text)
STOP_REASON=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK_ARN" \
  --query 'tasks[0].stoppedReason' --output text)

log "Release task log"
# `<task-id>` is the last path segment of the ARN; the stream name pattern is set
# by the awslogs stream prefix in the task definition.
TASK_ID="${TASK_ARN##*/}"
# `--output json | jq` rather than `--output text`, which tab-joins the messages
# onto one unreadable line.
aws logs get-log-events \
  --log-group-name "$RELEASE_LOG_GROUP" \
  --log-stream-name "release/release/${TASK_ID}" \
  --start-from-head \
  --output json 2>/dev/null | jq -r '.events[].message' \
  || echo "  (log stream not available yet)"

if [ "$EXIT_CODE" != "0" ]; then
  log "Release task failed (exit ${EXIT_CODE}: ${STOP_REASON})"
  echo "::error::Migrations failed; services were left on the previous image."
  exit 1
fi

########################################
# 3. Roll the services
########################################

log "Updating services"

for index in "${!SERVICE_NAMES[@]}"; do
  echo "  ${SERVICE_NAMES[$index]}"
  aws ecs update-service \
    --cluster "$CLUSTER" \
    --service "${SERVICE_NAMES[$index]}" \
    --task-definition "${SERVICE_TASK_DEFS[$index]}" \
    --query 'service.serviceName' --output text > /dev/null
done

log "Waiting for services to stabilise"
# The deployment circuit breaker (see ecs.tf) rolls a service back on its own if
# the new tasks will not start; this wait is what makes the workflow fail when
# that happens instead of reporting a green deploy.
aws ecs wait services-stable --cluster "$CLUSTER" --services "${SERVICE_NAMES[@]}"

log "Deployed ${IMAGE}"
