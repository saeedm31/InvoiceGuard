# VPC with two public subnets (load balancer, NAT) and two private subnets (the app and EFS).
# The tasks have no public IP; they reach Bedrock, Microsoft Graph and Teams through the NAT gateway.

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = local.prefix }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = local.prefix }
}

resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index)
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = false
  tags                    = { Name = "${local.prefix}-public-${count.index}" }
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + 10)
  availability_zone = local.azs[count.index]
  tags              = { Name = "${local.prefix}-private-${count.index}" }
}

resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = { Name = "${local.prefix}-nat" }
}

# One NAT gateway keeps the cost down; use one per AZ for production high availability.
resource "aws_nat_gateway" "main" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id
  tags          = { Name = local.prefix }
  depends_on    = [aws_internet_gateway.main]
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = { Name = "${local.prefix}-public" }
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main.id
  }
  tags = { Name = "${local.prefix}-private" }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "private" {
  count          = 2
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# Free gateway endpoint: image layers from ECR (stored in S3) do not go through the NAT.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]
  tags              = { Name = "${local.prefix}-s3" }
}

# ---------- security groups ----------
resource "aws_security_group" "alb" {
  name        = "${local.prefix}-alb"
  description = "Load balancer: HTTPS (and HTTP redirect) from allowed networks"
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_security_group_ingress_rule" "alb" {
  for_each          = { for pair in setproduct(var.allowed_cidrs, local.https ? [80, 443] : [80]) : "${pair[0]}-${pair[1]}" => pair }
  security_group_id = aws_security_group.alb.id
  cidr_ipv4         = each.value[0]
  from_port         = each.value[1]
  to_port           = each.value[1]
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "alb_to_app" {
  security_group_id            = aws_security_group.alb.id
  referenced_security_group_id = aws_security_group.app.id
  from_port                    = 8501
  to_port                      = 8501
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "alb_to_idp" {
  count             = var.oidc == null ? 0 : 1
  description       = "The load balancer calls the identity provider's token endpoint"
  security_group_id = aws_security_group.alb.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_security_group" "app" {
  name        = "${local.prefix}-app"
  description = "ECS task: app port from the load balancer only"
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_security_group_ingress_rule" "app_from_alb" {
  security_group_id            = aws_security_group.app.id
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = 8501
  to_port                      = 8501
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "app_https" {
  description       = "Bedrock, ECR, Secrets Manager, Microsoft Graph, Teams"
  security_group_id = aws_security_group.app.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "app_smtp" {
  count             = var.email_backend == "smtp" ? 1 : 0
  description       = "SMTP submission (STARTTLS)"
  security_group_id = aws_security_group.app.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 587
  to_port           = 587
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "app_to_efs" {
  security_group_id            = aws_security_group.app.id
  referenced_security_group_id = aws_security_group.efs.id
  from_port                    = 2049
  to_port                      = 2049
  ip_protocol                  = "tcp"
}

resource "aws_security_group" "efs" {
  name        = "${local.prefix}-efs"
  description = "EFS: NFS from the app tasks only"
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_security_group_ingress_rule" "efs_from_app" {
  security_group_id            = aws_security_group.efs.id
  referenced_security_group_id = aws_security_group.app.id
  from_port                    = 2049
  to_port                      = 2049
  ip_protocol                  = "tcp"
}
