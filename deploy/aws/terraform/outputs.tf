output "app_url" {
  description = "Open this once the deploy workflow has run."
  value       = local.app_url
}

output "load_balancer_dns" {
  description = "Point your DNS name (CNAME) here."
  value       = aws_lb.app.dns_name
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "github_variables" {
  description = "Set these as variables of the GitHub environment used by the deploy workflow."
  value = {
    AWS_REGION          = var.region
    AWS_DEPLOY_ROLE_ARN = aws_iam_role.github_deploy.arn
    ECR_REPOSITORY      = aws_ecr_repository.app.name
    ECS_CLUSTER         = aws_ecs_cluster.main.name
    ECS_SERVICE         = aws_ecs_service.app.name
    ECS_TASK_FAMILY     = aws_ecs_task_definition.app.family
  }
}

output "secret_name" {
  description = "Fill ANTHROPIC_API_KEY, GRAPH_CLIENT_SECRET and the Teams webhook URLs here when you need them."
  value       = aws_secretsmanager_secret.app.name
}
