# ---- CI: GitHub Actions deploys through OIDC (no stored AWS keys) -----------

variable "github_repo" {
  description = "GitHub repository (owner/name) whose main branch may deploy."
  type        = string
  default     = "i-osipchyk/polymarket-bond-hype"
}

variable "github_owner_id" {
  description = "Numeric GitHub owner id; GitHub now puts it in the OIDC subject (repo:owner@id/name@id:...)."
  type        = string
  default     = "94493032"
}

variable "github_repo_id" {
  description = "Numeric GitHub repository id, as in the OIDC subject. Ids survive renames, and a recreated repo gets a new one."
  type        = string
  default     = "1404411689"
}

variable "create_github_oidc_provider" {
  description = "Create the account's GitHub OIDC provider. Set false if it already exists (one per account)."
  type        = bool
  default     = true
}

resource "aws_iam_openid_connect_provider" "github" {
  count          = var.create_github_oidc_provider ? 1 : 0
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_openid_connect_provider" "github" {
  count = var.create_github_oidc_provider ? 0 : 1
  url   = "https://token.actions.githubusercontent.com"
}

locals {
  github_owner = split("/", var.github_repo)[0]
  github_name  = split("/", var.github_repo)[1]
  github_main_subject = (
    "repo:${local.github_owner}@${var.github_owner_id}/${local.github_name}@${var.github_repo_id}:ref:refs/heads/main"
  )

  github_oidc_arn = (
    var.create_github_oidc_provider
    ? aws_iam_openid_connect_provider.github[0].arn
    : data.aws_iam_openid_connect_provider.github[0].arn
  )
}

data "aws_iam_policy_document" "deploy_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [local.github_main_subject]
    }
  }
}

resource "aws_iam_role" "deploy" {
  name               = "bondhype-deploy"
  assume_role_policy = data.aws_iam_policy_document.deploy_assume.json
}

data "aws_iam_policy_document" "deploy" {
  statement {
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [aws_ecr_repository.app.arn]
  }
  statement {
    actions = [
      "lambda:GetFunction",
      "lambda:GetFunctionConfiguration",
      "lambda:UpdateFunctionCode",
    ]
    resources = [for fn in aws_lambda_function.fn : fn.arn]
  }
}

resource "aws_iam_role_policy" "deploy" {
  name   = "push-image-and-update-functions"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}

output "deploy_role_arn" {
  description = "Set as the AWS_DEPLOY_ROLE_ARN variable in the GitHub repository."
  value       = aws_iam_role.deploy.arn
}
