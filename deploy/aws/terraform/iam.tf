data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:ecs:${var.region}:${local.account_id}:*"]
    }
  }
}

# ---------- execution role: ECS pulls the image, reads secrets, writes logs ----------
resource "aws_iam_role" "execution" {
  name               = "${local.prefix}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution" {
  statement {
    sid       = "ReadAppSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.app.arn]
  }
  statement {
    sid       = "DecryptAppSecret"
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.main.arn]
  }
}

resource "aws_iam_role_policy" "execution" {
  name   = "secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution.json
}

# ---------- task role: what the running app may do (least privilege) ----------
resource "aws_iam_role" "task" {
  name               = "${local.prefix}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "task" {
  statement {
    sid       = "DataVolume"
    actions   = ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite"]
    resources = [aws_efs_file_system.data.arn]
    condition {
      test     = "StringEquals"
      variable = "elasticfilesystem:AccessPointArn"
      values   = [aws_efs_access_point.data.arn]
    }
  }

  # Claude on Bedrock, only Anthropic models and only from EU regions.
  dynamic "statement" {
    for_each = var.llm_provider == "bedrock" ? [1] : []
    content {
      sid       = "ClaudeOnBedrock"
      actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
      resources = concat(
        [for r in var.bedrock_regions : "arn:aws:bedrock:${r}::foundation-model/anthropic.*"],
        [for r in var.bedrock_regions : "arn:aws:bedrock:${r}:${local.account_id}:inference-profile/*"],
      )
    }
  }

  # The Anthropic Messages API endpoint of Bedrock (bedrock-mantle), used by the anthropic SDK.
  dynamic "statement" {
    for_each = var.llm_provider == "bedrock" ? [1] : []
    content {
      sid       = "ClaudeMessagesApiOnBedrock"
      actions   = ["bedrock-mantle:CreateInference"]
      resources = ["*"]
      condition {
        test     = "StringEquals"
        variable = "aws:RequestedRegion"
        values   = var.bedrock_regions
      }
    }
  }

  dynamic "statement" {
    for_each = var.email_backend == "ses" ? [1] : []
    content {
      sid       = "SendEmailFromOneAddress"
      actions   = ["ses:SendEmail"]
      resources = ["*"]
      condition {
        test     = "StringEquals"
        variable = "ses:FromAddress"
        values   = [var.email_from]
      }
    }
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "app"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}
