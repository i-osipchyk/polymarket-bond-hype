terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

data "aws_caller_identity" "current" {}

locals {
  secret_names = ["deepseek-api-key", "telegram-bot-token", "telegram-chat-id"]
  secret_arns = [
    for name in local.secret_names :
    "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${var.ssm_prefix}/${name}"
  ]

  # Every job reads storage and the three secrets (the shared runtime builds all of them).
  # `writes` jobs may put objects; none may delete. Scanner, tracker and overdue rewrite the
  # state tables (state/trades.csv, state/decisions.csv) with conditional writes.
  jobs = {
    scanner   = { timeout = 900, reserved = true, writes = true }
    tracker   = { timeout = 600, reserved = true, writes = true }
    overdue   = { timeout = 600, reserved = false, writes = true }
    report    = { timeout = 300, reserved = false, writes = true }
    heartbeat = { timeout = 60, reserved = false, writes = false }
  }
  functions = merge(local.jobs, {
    alarm = { timeout = 60, reserved = false, writes = false }
  })

  # Scanner and tracker run one after the other in the hourly state machine (cycle.tf), on the
  # hour. Overdue also writes the trades table, so it runs at 06:40, after the 06:00 cycle has
  # normally finished; report and heartbeat only read the tables.
  schedules = {
    overdue   = "cron(40 6 * * ? *)"
    report    = "cron(0 7 * * ? *)"
    heartbeat = "rate(1 hour)"
  }
}

# ---- storage -------------------------------------------------------------

resource "aws_s3_bucket" "data" {
  bucket = var.bucket_name
}

resource "aws_s3_bucket_versioning" "data" {
  bucket = aws_s3_bucket.data.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ---- image ---------------------------------------------------------------

resource "aws_ecr_repository" "app" {
  name                 = "bondhype"
  image_tag_mutability = "MUTABLE"
}

# ---- IAM (one role per function) -----------------------------------------

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "fn" {
  for_each           = local.functions
  name               = "bondhype-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

data "aws_iam_policy_document" "fn" {
  for_each = local.functions

  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.fn[each.key].arn}:*"]
  }
  statement {
    actions   = ["ssm:GetParameter"]
    resources = local.secret_arns
  }
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.data.arn]
  }
  statement {
    actions   = each.value.writes ? ["s3:GetObject", "s3:PutObject"] : ["s3:GetObject"]
    resources = ["${aws_s3_bucket.data.arn}/*"]
  }
}

resource "aws_iam_role_policy" "fn" {
  for_each = local.functions
  name     = "least-privilege"
  role     = aws_iam_role.fn[each.key].id
  policy   = data.aws_iam_policy_document.fn[each.key].json
}

# ---- functions and schedules ---------------------------------------------

resource "aws_cloudwatch_log_group" "fn" {
  for_each          = local.functions
  name              = "/aws/lambda/bondhype-${each.key}"
  retention_in_days = 90
}

resource "aws_lambda_function" "fn" {
  for_each      = local.functions
  function_name = "bondhype-${each.key}"
  role          = aws_iam_role.fn[each.key].arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"
  timeout       = each.value.timeout
  memory_size   = 512

  image_config {
    command = ["bondhype.lambda_entry.${each.key}"]
  }

  reserved_concurrent_executions = each.value.reserved && var.reserve_concurrency ? 1 : -1

  environment {
    variables = {
      BONDHYPE_BUCKET          = aws_s3_bucket.data.bucket
      BONDHYPE_CONFIG          = "/var/task/config/config.yaml"
      BONDHYPE_PROMPTS_DIR     = "/var/task/prompts"
      BONDHYPE_PRICING         = "/var/task/deepseek_pricing.yaml"
      DEEPSEEK_API_KEY_PARAM   = "${var.ssm_prefix}/deepseek-api-key"
      TELEGRAM_BOT_TOKEN_PARAM = "${var.ssm_prefix}/telegram-bot-token"
      TELEGRAM_CHAT_ID_PARAM   = "${var.ssm_prefix}/telegram-chat-id"
    }
  }

  depends_on = [aws_cloudwatch_log_group.fn, aws_iam_role_policy.fn]
}

resource "aws_cloudwatch_event_rule" "job" {
  for_each            = local.schedules
  name                = "bondhype-${each.key}"
  schedule_expression = each.value
}

resource "aws_cloudwatch_event_target" "job" {
  for_each = local.schedules
  rule     = aws_cloudwatch_event_rule.job[each.key].name
  arn      = aws_lambda_function.fn[each.key].arn
}

resource "aws_lambda_permission" "events" {
  for_each      = local.schedules
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.fn[each.key].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.job[each.key].arn
}

# ---- alarms: Lambda errors -> SNS -> alarm Lambda -> Telegram -------------

resource "aws_sns_topic" "alarms" {
  name = "bondhype-alarms"
}

resource "aws_sns_topic_subscription" "alarm_fn" {
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.fn["alarm"].arn
}

resource "aws_lambda_permission" "sns" {
  statement_id  = "AllowSNS"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.fn["alarm"].function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.alarms.arn
}

resource "aws_cloudwatch_metric_alarm" "errors" {
  for_each            = local.jobs
  alarm_name          = "bondhype-${each.key}-errors"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.fn[each.key].function_name }
  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}
