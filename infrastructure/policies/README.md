# Deployer IAM policies

The Scalr deployer (`vinta-schedule-<env>-deployer`) was created when the
workspace only managed S3 and CloudFront, so it has no rights over the VPC,
ECS, RDS, ElastiCache, SQS, Secrets Manager or the IAM roles that
`modules/app-platform` creates. Its first `app` plan fails on
`ec2:DescribeAvailabilityZones`.

These documents grant exactly what the module needs, split into three because a
single document would exceed the managed-policy size limit:

| Policy | Covers |
|---|---|
| `<env>-deployer-network.json` | VPC, subnets, IGW, NAT, EIP, routes, security groups, the S3 gateway endpoint, and the ALB + target group + listeners |
| `<env>-deployer-compute.json` | ECS cluster/services/task definitions, ECR, RDS, ElastiCache, CloudWatch log groups |
| `<env>-deployer-platform.json` | SQS, Secrets Manager, the SSM deploy parameter, the task/deploy IAM roles, the GitHub OIDC provider, KMS via those services, and assuming the DNS-account role |

**Keep the existing inline policy.** It covers the storage half — the buckets,
CloudFront, ACM and the storage IAM user — and nothing here replaces it. The
one statement in it that is now dead weight is `Route53Records`: every Route 53
write goes through the `aws.dns` provider, which assumes
`vinta-schedule-dns-deployer` in the DNS account, so the records are written by
that role rather than by this user. Removing it is optional.

## Attach them

Inline user policies cap at 2048 characters and these are 2.5-4.2 KB, so they
have to be customer-managed policies (6144 characters each, and every one of
these fits).

```bash
ENV=staging
USER="vinta-schedule-${ENV}-deployer"

for part in network compute platform; do
  arn=$(aws iam create-policy \
    --policy-name "${USER}-${part}" \
    --policy-document "file://infrastructure/policies/${ENV}-deployer-${part}.json" \
    --query 'Policy.Arn' --output text)
  aws iam attach-user-policy --user-name "$USER" --policy-arn "$arn"
done
```

To update one after a module change, add a new default version rather than
recreating it:

```bash
aws iam create-policy-version --set-as-default \
  --policy-arn "arn:aws:iam::261390480437:policy/vinta-schedule-staging-deployer-network" \
  --policy-document file://infrastructure/policies/staging-deployer-network.json
```

## How these were derived

Every action traces to a resource `modules/app-platform` actually declares —
the list came from the module's `resource`/`data` blocks, not from a generic
template. Scoping follows what each service supports:

- **ARN-scoped** (114 actions) wherever the service supports resource-level
  permissions: RDS, ElastiCache, SQS, Secrets Manager, SSM, ECR, the ECS
  cluster/services/task-definition families, the log groups, and the
  `vinta-schedule-<env>-*` roles.
- **`Resource: "*"` with an `aws:RequestedRegion` condition** for the actions
  AWS does not support ARNs on — every EC2 and ELB action, plus the `Describe*`
  and `List*` calls in the other services. The region condition is what keeps
  the blast radius down.
- **`iam:PassRole`** is limited to the two task roles and further conditioned on
  `iam:PassedToService = ecs-tasks.amazonaws.com`.
- **`iam:CreateServiceLinkedRole`** is conditioned on `iam:AWSServiceName` for
  the four services that need one.
- **KMS** is conditioned on `kms:ViaService`, so it only reaches the
  AWS-managed keys behind RDS, ElastiCache, Secrets Manager and SQS.

Both properties were checked against AWS's public service reference
(`https://servicereference.us-east-1.amazonaws.com/`): all 217 action names
exist, and no ARN-scoped action lacks resource-level support (which would make
it deny silently, whatever ARN it names).

That verification is static. AWS occasionally requires a permission its own
docs omit, so if a plan still hits `AccessDenied`, add the action the error
names to the matching document and open a new policy version — don't widen a
statement to `service:*` to move on.

## Production

`production-deployer-*.json` are the same documents with the prefix and the SSM
path swapped. Production has never been applied, and its deployer will need
these before the first run.
