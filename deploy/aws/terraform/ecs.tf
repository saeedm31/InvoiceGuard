# One Fargate task = the app plus the four MCP servers as sidecar containers. They share the task's
# network, so the app reaches each server on 127.0.0.1 over Streamable HTTP, like docker-compose.
# An "init" container builds the simulated company on first start (only when the data is missing).

locals {
  mcp_servers = { documents = 8101, erp = 8102, mail = 8103, notify = 8104 }
  app_url     = var.app_domain != "" ? "https://${var.app_domain}" : "${local.https ? "https" : "http"}://${aws_lb.app.dns_name}"
  secret      = aws_secretsmanager_secret.app.arn

  logs = {
    for c in concat(["init", "app"], keys(local.mcp_servers)) : c => {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.app.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = c
      }
    }
  }
  data_mount = [{ sourceVolume = "data", containerPath = "/data", readOnly = false }]

  init_container = {
    name             = "init"
    image            = local.image
    essential        = false
    command          = ["python", "-m", "invoiceguard", "ensure"]
    environment      = [{ name = "IG_DATA_DIR", value = "/data" }]
    mountPoints      = local.data_mount
    logConfiguration = local.logs["init"]
  }

  mcp_containers = [
    for name, port in local.mcp_servers : {
      name        = name
      image       = local.image
      essential   = true
      command     = ["python", "-m", "invoiceguard.mcp_servers.${name}", "--http", tostring(port)]
      mountPoints = local.data_mount
      environment = concat(
        [{ name = "IG_DATA_DIR", value = "/data" }, { name = "MCP_ALLOWED_HOSTS", value = "127.0.0.1:*,localhost:*" }],
        name == "notify" ? [
          { name = "NOTIFY_MODE", value = var.notify_mode },
          { name = "EMAIL_BACKEND", value = var.email_backend },
          { name = "EMAIL_FROM", value = var.email_from },
          { name = "GRAPH_SENDER", value = var.email_from },
          { name = "GRAPH_TENANT_ID", value = var.graph_tenant_id },
          { name = "GRAPH_CLIENT_ID", value = var.graph_client_id },
          { name = "APP_URL", value = local.app_url },
        ] : []
      )
      secrets = concat(
        name == "erp" ? [{ name = "APPROVAL_SECRET", valueFrom = "${local.secret}:APPROVAL_SECRET::" }] : [],
        name == "notify" && var.notify_mode == "live" ? [
          { name = "GRAPH_CLIENT_SECRET", valueFrom = "${local.secret}:GRAPH_CLIENT_SECRET::" },
          { name = "TEAMS_WEBHOOK_AP_APPROVALS", valueFrom = "${local.secret}:TEAMS_WEBHOOK_AP_APPROVALS::" },
          { name = "TEAMS_WEBHOOK_AP_ALERTS", valueFrom = "${local.secret}:TEAMS_WEBHOOK_AP_ALERTS::" },
        ] : []
      )
      dependsOn = [{ containerName = "init", condition = "SUCCESS" }]
      healthCheck = {
        command     = ["CMD", "python", "-c", "import socket; socket.create_connection(('127.0.0.1', ${port}), 2)"]
        interval    = 15
        timeout     = 5
        retries     = 3
        startPeriod = 20
      }
      logConfiguration = local.logs[name]
    }
  ]

  app_container = {
    name         = "app"
    image        = local.image
    essential    = true
    portMappings = [{ containerPort = 8501, protocol = "tcp" }]
    mountPoints  = local.data_mount
    environment = [
      { name = "IG_DATA_DIR", value = "/data" },
      { name = "MCP_URLS", value = join(",", [for n, p in local.mcp_servers : "${n}=http://127.0.0.1:${p}/mcp"]) },
      { name = "APP_URL", value = local.app_url },
      { name = "LLM_PROVIDER", value = var.llm_provider },
      { name = "AWS_REGION", value = var.region },
      { name = "BEDROCK_MODEL_IDS", value = jsonencode(var.bedrock_model_ids) },
      { name = "PRICE_FACTOR", value = var.llm_provider == "bedrock" ? "1.1" : "1" },
      { name = "STREAMLIT_SERVER_HEADLESS", value = "true" },
      { name = "STREAMLIT_BROWSER_GATHER_USAGE_STATS", value = "false" },
    ]
    secrets = concat(
      [{ name = "APPROVAL_SECRET", valueFrom = "${local.secret}:APPROVAL_SECRET::" }],
      var.llm_provider == "anthropic" ? [{ name = "ANTHROPIC_API_KEY", valueFrom = "${local.secret}:ANTHROPIC_API_KEY::" }] : []
    )
    dependsOn = [for n in keys(local.mcp_servers) : { containerName = n, condition = "HEALTHY" }]
    healthCheck = {
      command     = ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=3)"]
      interval    = 15
      timeout     = 5
      retries     = 3
      startPeriod = 60
    }
    logConfiguration = local.logs["app"]
  }
}

resource "aws_ecs_cluster" "main" {
  name = local.prefix
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_ecs_task_definition" "app" {
  family                   = local.prefix
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  volume {
    name = "data"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.data.id
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.data.id
        iam             = "ENABLED"
      }
    }
  }

  container_definitions = jsonencode(concat([local.init_container], local.mcp_containers, [local.app_container]))
}

resource "aws_ecs_service" "app" {
  name             = local.prefix
  cluster          = aws_ecs_cluster.main.id
  task_definition  = aws_ecs_task_definition.app.arn
  launch_type      = "FARGATE"
  platform_version = "LATEST"
  # Starts at 0: the first run of the deploy workflow pushes an image and sets it to 1.
  desired_count = 0
  # SQLite on EFS takes one writer: stop the old task before the new one starts (short downtime).
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100
  health_check_grace_period_seconds  = 120
  propagate_tags                     = "SERVICE"
  enable_execute_command             = false

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = "app"
    container_port   = 8501
  }

  # The deploy workflow owns the running image and the task count; Terraform owns the rest.
  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }

  depends_on = [aws_lb_listener.http, aws_efs_mount_target.data]
}

# ---------- alarms ----------
resource "aws_sns_topic" "alarms" {
  name              = "${local.prefix}-alarms"
  kms_master_key_id = "alias/aws/sns"
}

resource "aws_sns_topic_subscription" "alarms" {
  count     = var.alarm_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

resource "aws_cloudwatch_metric_alarm" "app_down" {
  alarm_name          = "${local.prefix}-app-down"
  alarm_description   = "No healthy InvoiceGuard task behind the load balancer"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HealthyHostCount"
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 5
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  treat_missing_data  = "breaching"
  dimensions = {
    LoadBalancer = aws_lb.app.arn_suffix
    TargetGroup  = aws_lb_target_group.app.arn_suffix
  }
  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "errors" {
  alarm_name          = "${local.prefix}-5xx"
  alarm_description   = "The app returns server errors"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 5
  treat_missing_data  = "notBreaching"
  dimensions = {
    LoadBalancer = aws_lb.app.arn_suffix
    TargetGroup  = aws_lb_target_group.app.arn_suffix
  }
  alarm_actions = [aws_sns_topic.alarms.arn]
}
