# ---- hourly cycle: scanner, then tracker, as one Step Functions execution -----
#
# The two jobs that write the trades table every hour run one after the other, so they never
# overlap. The execution input is the EventBridge event; both tasks get its `time`, so a retried
# task reuses the scheduled time. A failed scan still lets the tracker run, then fails the
# execution so the alarm fires.

locals {
  cycle_jobs = ["scanner", "tracker"]

  cycle_task_retry = [{
    ErrorEquals     = ["States.ALL"]
    IntervalSeconds = 60
    MaxAttempts     = 1
  }]

  cycle_definition = {
    Comment        = "Hourly scan, then track"
    StartAt        = "Scan"
    TimeoutSeconds = 3300 # ends before the next hour's execution starts
    States = {
      Scan = {
        Type           = "Task"
        Resource       = "arn:aws:states:::lambda:invoke"
        Parameters     = { FunctionName = aws_lambda_function.fn["scanner"].arn, "Payload" = { "time.$" = "$.time" } }
        ResultPath     = null
        TimeoutSeconds = local.jobs.scanner.timeout + 60
        Retry          = local.cycle_task_retry
        Catch          = [{ ErrorEquals = ["States.ALL"], ResultPath = "$.scan_error", Next = "Track" }]
        Next           = "Track"
      }
      Track = {
        Type           = "Task"
        Resource       = "arn:aws:states:::lambda:invoke"
        Parameters     = { FunctionName = aws_lambda_function.fn["tracker"].arn, "Payload" = { "time.$" = "$.time" } }
        ResultPath     = null
        TimeoutSeconds = local.jobs.tracker.timeout + 60
        Retry          = local.cycle_task_retry
        Next           = "ScanOk"
      }
      ScanOk = {
        Type    = "Choice"
        Choices = [{ Variable = "$.scan_error", IsPresent = true, Next = "ScanFailed" }]
        Default = "Done"
      }
      ScanFailed = { Type = "Fail", Error = "ScanFailed", Cause = "the scanner failed; the tracker ran" }
      Done       = { Type = "Succeed" }
    }
  }
}

data "aws_iam_policy_document" "states_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cycle" {
  name               = "bondhype-cycle"
  assume_role_policy = data.aws_iam_policy_document.states_assume.json
}

data "aws_iam_policy_document" "cycle" {
  statement {
    actions = ["lambda:InvokeFunction"]
    resources = flatten([
      for job in local.cycle_jobs :
      [aws_lambda_function.fn[job].arn, "${aws_lambda_function.fn[job].arn}:*"]
    ])
  }
  # Step Functions log delivery is account-level, so these cannot be scoped to one log group.
  statement {
    actions = [
      "logs:CreateLogDelivery",
      "logs:GetLogDelivery",
      "logs:UpdateLogDelivery",
      "logs:DeleteLogDelivery",
      "logs:ListLogDeliveries",
      "logs:PutResourcePolicy",
      "logs:DescribeResourcePolicies",
      "logs:DescribeLogGroups",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "cycle" {
  name   = "invoke-scanner-and-tracker"
  role   = aws_iam_role.cycle.id
  policy = data.aws_iam_policy_document.cycle.json
}

resource "aws_cloudwatch_log_group" "cycle" {
  name              = "/aws/vendedlogs/states/bondhype-cycle"
  retention_in_days = 90
}

resource "aws_sfn_state_machine" "cycle" {
  name       = "bondhype-cycle"
  role_arn   = aws_iam_role.cycle.arn
  definition = jsonencode(local.cycle_definition)

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.cycle.arn}:*"
    include_execution_data = false
    level                  = "ERROR"
  }

  depends_on = [aws_iam_role_policy.cycle]
}

# ---- schedule: EventBridge starts one execution on every hour ---------------

data "aws_iam_policy_document" "events_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cycle_schedule" {
  name               = "bondhype-cycle-schedule"
  assume_role_policy = data.aws_iam_policy_document.events_assume.json
}

data "aws_iam_policy_document" "cycle_schedule" {
  statement {
    actions   = ["states:StartExecution"]
    resources = [aws_sfn_state_machine.cycle.arn]
  }
}

resource "aws_iam_role_policy" "cycle_schedule" {
  name   = "start-cycle"
  role   = aws_iam_role.cycle_schedule.id
  policy = data.aws_iam_policy_document.cycle_schedule.json
}

resource "aws_cloudwatch_event_rule" "cycle" {
  name                = "bondhype-cycle"
  schedule_expression = "cron(0 * * * ? *)"
}

resource "aws_cloudwatch_event_target" "cycle" {
  rule     = aws_cloudwatch_event_rule.cycle.name
  arn      = aws_sfn_state_machine.cycle.arn
  role_arn = aws_iam_role.cycle_schedule.arn
}

# ---- alarm: a failed, timed-out or aborted execution goes to the alarm topic --

resource "aws_cloudwatch_metric_alarm" "cycle" {
  for_each            = toset(["ExecutionsFailed", "ExecutionsTimedOut", "ExecutionsAborted"])
  alarm_name          = "bondhype-cycle-${each.key}"
  namespace           = "AWS/States"
  metric_name         = each.key
  dimensions          = { StateMachineArn = aws_sfn_state_machine.cycle.arn }
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}
