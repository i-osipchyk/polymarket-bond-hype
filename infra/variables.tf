variable "region" {
  type    = string
  default = "eu-west-1"
}

variable "bucket_name" {
  description = "Globally unique name for the data bucket."
  type        = string
}

variable "image_tag" {
  description = "Tag of the container image pushed to the ECR repository."
  type        = string
  default     = "latest"
}

variable "reserve_concurrency" {
  description = "Reserve concurrency 1 on scanner and tracker. Needs an account concurrency limit above 10; new accounts must request an increase first."
  type        = bool
  default     = true
}

variable "ssm_prefix" {
  description = "SSM SecureString parameters {prefix}/deepseek-api-key, /telegram-bot-token, /telegram-chat-id are created by hand, never by Terraform."
  type        = string
  default     = "/bondhype"
}
