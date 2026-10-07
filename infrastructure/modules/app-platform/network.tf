locals {
  name_prefix = "${var.project_name}-${var.environment}"

  azs = slice(data.aws_availability_zones.available.names, 0, var.availability_zone_count)

  # /24 per subnet out of the VPC's /16: public subnets take the low indexes,
  # private subnets start at 100 so the two ranges stay readable in the console.
  public_subnet_cidrs  = [for i in range(var.availability_zone_count) : cidrsubnet(var.vpc_cidr, 8, i)]
  private_subnet_cidrs = [for i in range(var.availability_zone_count) : cidrsubnet(var.vpc_cidr, 8, i + 100)]

  nat_gateway_count = var.nat_mode != "gateway" ? 0 : (
    var.single_nat_gateway ? 1 : var.availability_zone_count
  )
}

data "aws_availability_zones" "available" {
  state = "available"

  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = local.name_prefix
  }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = {
    Name = local.name_prefix
  }
}

########################################
# Subnets
#
# Public: the ALB and the NAT gateway, nothing else.
# Private: every ECS task, RDS and ElastiCache. Nothing in here has a public IP;
# outbound traffic to the calendar/payment/Twilio APIs leaves through NAT (a
# managed gateway or an instance, per `nat_mode`).
########################################

resource "aws_subnet" "public" {
  count = var.availability_zone_count

  vpc_id            = aws_vpc.this.id
  cidr_block        = local.public_subnet_cidrs[count.index]
  availability_zone = local.azs[count.index]

  # Only the NAT gateway and the ALB live here, and both get their addresses
  # explicitly, so nothing needs an auto-assigned public IP.
  map_public_ip_on_launch = false

  tags = {
    Name = "${local.name_prefix}-public-${local.azs[count.index]}"
    Tier = "public"
  }
}

resource "aws_subnet" "private" {
  count = var.availability_zone_count

  vpc_id            = aws_vpc.this.id
  cidr_block        = local.private_subnet_cidrs[count.index]
  availability_zone = local.azs[count.index]

  tags = {
    Name = "${local.name_prefix}-private-${local.azs[count.index]}"
    Tier = "private"
  }
}

########################################
# NAT
########################################

resource "aws_eip" "nat" {
  count = local.nat_gateway_count

  domain = "vpc"

  tags = {
    Name = "${local.name_prefix}-nat-${count.index}"
  }
}

resource "aws_nat_gateway" "this" {
  count = local.nat_gateway_count

  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id

  tags = {
    Name = "${local.name_prefix}-nat-${count.index}"
  }

  depends_on = [aws_internet_gateway.this]
}

########################################
# NAT instance (nat_mode = "instance")
#
# One EC2 instance forwarding and masquerading the private subnets' traffic --
# the same job the NAT gateway does, for ~$3/month of compute instead of ~$32.
# Built from stock Amazon Linux rather than a community NAT image, so the only
# software on it is what the user data below installs.
#
# The route targets the network interface, not the instance, and the Elastic
# IP is attached to that interface. Both outlive the instance: replacing it (to
# patch it, or after a failure) keeps the routes and the outbound address that
# third parties may have allowlisted, and the new instance has internet access
# from its first boot, which the user data's `dnf install` needs.
#
# What it does not do: survive the loss of its AZ, or patch itself. AWS's
# default instance auto-recovery restarts it on host failure; anything beyond
# that is a `taint` and a new run (see infrastructure/README.md).
########################################

data "aws_ec2_instance_type" "nat" {
  count = var.nat_mode == "instance" ? 1 : 0

  instance_type = var.nat_instance_type
}

data "aws_ami" "nat" {
  count = var.nat_mode == "instance" ? 1 : 0

  owners      = ["amazon"]
  most_recent = true

  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-kernel-*"]
  }

  filter {
    name = "architecture"
    # t4g and the other Graviton types are arm64; everything else is x86_64. Read
    # from the type so changing nat_instance_type cannot pair it with the wrong
    # image.
    values = [
      contains(data.aws_ec2_instance_type.nat[0].supported_architectures, "arm64")
      ? "arm64" : "x86_64"
    ]
  }
}

resource "aws_security_group" "nat" {
  count = var.nat_mode == "instance" ? 1 : 0

  name        = "${local.name_prefix}-nat"
  description = "NAT instance: forwards traffic from the ECS tasks to the internet."
  vpc_id      = aws_vpc.this.id

  tags = {
    Name = "${local.name_prefix}-nat"
  }
}

# Only the tasks route through it. RDS and ElastiCache never open outbound
# connections, so they are left out rather than allowing the whole VPC range.
resource "aws_vpc_security_group_ingress_rule" "nat_from_tasks" {
  count = var.nat_mode == "instance" ? 1 : 0

  security_group_id            = aws_security_group.nat[0].id
  description                  = "Traffic from the ECS tasks to be forwarded."
  referenced_security_group_id = aws_security_group.ecs_tasks.id
  ip_protocol                  = "-1"
}

resource "aws_vpc_security_group_egress_rule" "nat_all" {
  count = var.nat_mode == "instance" ? 1 : 0

  security_group_id = aws_security_group.nat[0].id
  description       = "Forwarded traffic out to the internet."
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_network_interface" "nat" {
  count = var.nat_mode == "instance" ? 1 : 0

  subnet_id       = aws_subnet.public[0].id
  security_groups = [aws_security_group.nat[0].id]

  # A NAT forwards packets addressed to other hosts, which EC2 drops by default.
  source_dest_check = false

  tags = {
    Name = "${local.name_prefix}-nat"
  }
}

resource "aws_eip" "nat_instance" {
  count = var.nat_mode == "instance" ? 1 : 0

  domain            = "vpc"
  network_interface = aws_network_interface.nat[0].id

  tags = {
    Name = "${local.name_prefix}-nat-instance"
  }

  depends_on = [aws_internet_gateway.this]
}

resource "aws_instance" "nat" {
  count = var.nat_mode == "instance" ? 1 : 0

  ami           = data.aws_ami.nat[0].id
  instance_type = var.nat_instance_type

  network_interface {
    network_interface_id = aws_network_interface.nat[0].id
    device_index         = 0
  }

  # No key pair and no instance profile: nothing logs in, and nothing on it calls
  # AWS. A broken instance is replaced, not debugged in place.
  metadata_options {
    http_tokens = "required"
  }

  maintenance_options {
    auto_recovery = "default"
  }

  # t4g defaults to `unlimited`, which bills for CPU past the burst baseline.
  # NAT needs almost no CPU; anything that does is a problem to notice, not pay
  # for.
  credit_specification {
    cpu_credits = "standard"
  }

  root_block_device {
    volume_type = "gp3"
    encrypted   = true
  }

  # Masquerade only the VPC's own range, and leave the FORWARD chain open: the
  # security group above is what limits who can use it. iptables-services ships
  # a rule set that rejects forwarding, so it is started, then that chain is
  # cleared and the result saved over the shipped defaults.
  #
  # A t4g.nano has 512 MB of RAM, which is not enough for dnf: loading the
  # Amazon Linux repository metadata got it killed by the kernel, and the
  # instance came up forwarding nothing. So the script first stops the SSM
  # agent, which can do nothing here without an instance profile, and adds 1 GB
  # of swap for the install. The swap is not persisted: nothing after the first
  # boot needs it. IP forwarding is set directly rather than through
  # `sysctl --system`, so an unrelated setting failing to apply cannot stop the
  # script.
  user_data = <<-EOT
    #!/bin/bash
    set -euo pipefail
    systemctl disable --now amazon-ssm-agent || true
    dd if=/dev/zero of=/swapfile bs=1M count=1024 status=none
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    dnf install -y --setopt=install_weak_deps=False iptables-services
    echo 'net.ipv4.ip_forward = 1' > /etc/sysctl.d/90-nat.conf
    sysctl -w net.ipv4.ip_forward=1
    systemctl enable --now iptables
    iface=$(ip -o route show default | awk 'NR==1 {print $5}')
    iptables -F FORWARD
    iptables -t nat -A POSTROUTING -o "$iface" -s ${var.vpc_cidr} -j MASQUERADE
    iptables-save > /etc/sysconfig/iptables
  EOT

  user_data_replace_on_change = true

  lifecycle {
    # A newer Amazon Linux release must not replace the instance -- and cut
    # outbound traffic -- on whatever apply happens to run next. Patching is a
    # deliberate `taint`; see infrastructure/README.md.
    ignore_changes = [ami]
  }

  tags = {
    Name = "${local.name_prefix}-nat"
  }

  depends_on = [aws_eip.nat_instance]
}

########################################
# Routing
########################################

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  tags = {
    Name = "${local.name_prefix}-public"
  }
}

resource "aws_route" "public_default" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

resource "aws_route_table_association" "public" {
  count = var.availability_zone_count

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# One private route table per AZ even when a single NAT serves them all:
# flipping `single_nat_gateway` or `nat_mode` then only rewires the default
# routes, instead of forcing every private subnet through a table rebuild.
resource "aws_route_table" "private" {
  count = var.availability_zone_count

  vpc_id = aws_vpc.this.id

  tags = {
    Name = "${local.name_prefix}-private-${local.azs[count.index]}"
  }
}

resource "aws_route" "private_default" {
  count = var.availability_zone_count

  route_table_id         = aws_route_table.private[count.index].id
  destination_cidr_block = "0.0.0.0/0"

  nat_gateway_id = (
    var.nat_mode == "gateway"
    ? aws_nat_gateway.this[var.single_nat_gateway ? 0 : count.index].id
    : null
  )
  network_interface_id = var.nat_mode == "instance" ? aws_network_interface.nat[0].id : null

  # The route may only move to the ENI once an instance is behind it. Without
  # this, a failed instance launch would still leave the routes pointing at a
  # bare ENI -- and Terraform, having updated them, would go on to delete the
  # NAT gateway, leaving the tasks with no way out at all.
  depends_on = [aws_instance.nat]
}

resource "aws_route_table_association" "private" {
  count = var.availability_zone_count

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

########################################
# VPC endpoints
#
# The S3 gateway endpoint is free and pays for itself immediately: ECR stores
# image layers in S3, so without it every task start pulls the whole image
# through the NAT gateway at $0.045/GB. Interface endpoints for ECR/logs/SQS/
# Secrets Manager are deliberately NOT created -- each costs ~$7/month per AZ,
# which is more than the NAT data they would save at this traffic level.
########################################

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = aws_route_table.private[*].id

  tags = {
    Name = "${local.name_prefix}-s3"
  }
}
