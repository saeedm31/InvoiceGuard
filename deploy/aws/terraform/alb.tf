# Application Load Balancer in front of Streamlit. With a certificate it serves HTTPS and can
# require the company's single sign-on (OIDC) before any request reaches the app.

resource "aws_lb" "app" {
  name                       = local.prefix
  load_balancer_type         = "application"
  internal                   = var.internal_alb
  security_groups            = [aws_security_group.alb.id]
  subnets                    = var.internal_alb ? aws_subnet.private[*].id : aws_subnet.public[*].id
  idle_timeout               = 300 # Streamlit keeps a websocket open
  drop_invalid_header_fields = true
  enable_deletion_protection = var.environment == "prod"
}

resource "aws_lb_target_group" "app" {
  name                 = local.prefix
  port                 = 8501
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = aws_vpc.main.id
  deregistration_delay = 30

  health_check {
    path                = "/_stcore/health"
    matcher             = "200"
    interval            = 15
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  stickiness {
    type            = "lb_cookie"
    cookie_duration = 86400
  }
}

# HTTPS: optional SSO, then the app.
resource "aws_lb_listener" "https" {
  count             = local.https ? 1 : 0
  load_balancer_arn = aws_lb.app.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn

  dynamic "default_action" {
    for_each = var.oidc == null ? [] : [var.oidc]
    content {
      type  = "authenticate-oidc"
      order = 1
      authenticate_oidc {
        issuer                     = default_action.value.issuer
        authorization_endpoint     = default_action.value.authorization_endpoint
        token_endpoint             = default_action.value.token_endpoint
        user_info_endpoint         = default_action.value.user_info_endpoint
        client_id                  = default_action.value.client_id
        client_secret              = var.oidc_client_secret
        on_unauthenticated_request = "authenticate"
        scope                      = "openid email profile"
        session_timeout            = 28800
      }
    }
  }

  default_action {
    type             = "forward"
    order            = 2
    target_group_arn = aws_lb_target_group.app.arn
  }
}

# Port 80: redirect to HTTPS, or serve the app directly when there is no certificate yet.
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.app.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = local.https ? "redirect" : "forward"
    target_group_arn = local.https ? null : aws_lb_target_group.app.arn

    dynamic "redirect" {
      for_each = local.https ? [1] : []
      content {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }
}
