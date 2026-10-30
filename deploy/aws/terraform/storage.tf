# ---------- encryption key ----------
resource "aws_kms_key" "main" {
  description             = "${local.prefix}: secrets and the data file system"
  enable_key_rotation     = true
  deletion_window_in_days = 30
}

resource "aws_kms_alias" "main" {
  name          = "alias/${local.prefix}"
  target_key_id = aws_kms_key.main.key_id
}

# ---------- container registry ----------
resource "aws_ecr_repository" "app" {
  name                 = local.prefix
  image_tag_mutability = "IMMUTABLE" # a tag (the commit SHA) always means the same image

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.main.arn
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 20 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 20 }
      action       = { type = "expire" }
    }]
  })
}

# ---------- data: SQLite databases and invoice PDFs on EFS ----------
# Fine for one task and a department's volume. For several tasks, move cases and audit to RDS
# PostgreSQL and the inbox to S3 (see deploy/aws/README.md).
resource "aws_efs_file_system" "data" {
  creation_token   = local.prefix
  encrypted        = true
  kms_key_id       = aws_kms_key.main.arn
  performance_mode = "generalPurpose"
  throughput_mode  = "elastic"

  lifecycle_policy {
    transition_to_ia = "AFTER_30_DAYS"
  }

  tags = { Name = "${local.prefix}-data" }
}

resource "aws_efs_backup_policy" "data" {
  file_system_id = aws_efs_file_system.data.id
  backup_policy {
    status = "ENABLED" # daily AWS Backup, 35 days
  }
}

resource "aws_efs_mount_target" "data" {
  count           = 2
  file_system_id  = aws_efs_file_system.data.id
  subnet_id       = aws_subnet.private[count.index].id
  security_groups = [aws_security_group.efs.id]
}

# The containers run as the non-root "app" user (uid 1000 in the Dockerfile).
resource "aws_efs_access_point" "data" {
  file_system_id = aws_efs_file_system.data.id

  posix_user {
    uid = 1000
    gid = 1000
  }

  root_directory {
    path = "/invoiceguard"
    creation_info {
      owner_uid   = 1000
      owner_gid   = 1000
      permissions = "750"
    }
  }
}

resource "aws_efs_file_system_policy" "data" {
  file_system_id = aws_efs_file_system.data.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "TlsOnly"
        Effect    = "Deny"
        Principal = { AWS = "*" }
        Action    = "*"
        Resource  = aws_efs_file_system.data.arn
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
      {
        Sid       = "AppViaAccessPoint"
        Effect    = "Allow"
        Principal = { AWS = aws_iam_role.task.arn }
        Action    = ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite"]
        Resource  = aws_efs_file_system.data.arn
        Condition = { StringEquals = { "elasticfilesystem:AccessPointArn" = aws_efs_access_point.data.arn } }
      },
    ]
  })
}

# ---------- secrets ----------
# One JSON secret. Terraform creates it with a random approval key and empty placeholders;
# fill the rest in the console or CLI. Terraform will not overwrite your values.
resource "random_password" "approval_secret" {
  length  = 64
  special = false
}

resource "aws_secretsmanager_secret" "app" {
  name                    = "${local.prefix}/app"
  description             = "InvoiceGuard runtime secrets"
  kms_key_id              = aws_kms_key.main.arn
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "app" {
  secret_id = aws_secretsmanager_secret.app.id
  secret_string = jsonencode({
    APPROVAL_SECRET            = random_password.approval_secret.result
    ANTHROPIC_API_KEY          = ""
    GRAPH_CLIENT_SECRET        = ""
    TEAMS_WEBHOOK_AP_APPROVALS = ""
    TEAMS_WEBHOOK_AP_ALERTS    = ""
  })

  lifecycle {
    ignore_changes = [secret_string]
  }
}

# ---------- logs ----------
resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${local.prefix}"
  retention_in_days = var.log_retention_days
}
