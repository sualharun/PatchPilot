variable "project_name" {
  type        = string
  default     = "patchpilot"
  description = "Name used for Kubernetes namespace and labels."
}

variable "region" {
  type        = string
  default     = "local"
  description = "Deployment region label for cloud-ready environments."
}

variable "kubeconfig_path" {
  type        = string
  default     = "~/.kube/config"
  description = "Path to kubeconfig for local Kubernetes, minikube, kind, or a cloud cluster."
}

variable "kube_context" {
  type        = string
  default     = null
  description = "Optional kubeconfig context."
}

variable "patchpilot_image" {
  type        = string
  default     = "patchpilot:latest"
  description = "Container image for API and worker deployments."
}

variable "api_replicas" {
  type    = number
  default = 2
}

variable "pr_worker_replicas" {
  type    = number
  default = 2
}

variable "agent_worker_replicas" {
  type    = number
  default = 1
}

variable "apply_workloads" {
  type        = bool
  default     = false
  description = "When true, Terraform shells out to kubectl to apply Kubernetes workload manifests."
}

variable "github_token" {
  type      = string
  default   = ""
  sensitive = true
}

variable "github_webhook_secret" {
  type      = string
  sensitive = true
}

variable "github_oauth_client_id" {
  type      = string
  default   = ""
  sensitive = true
}

variable "github_oauth_client_secret" {
  type      = string
  default   = ""
  sensitive = true
}

variable "github_oauth_callback_url" {
  type    = string
  default = "https://app.example.com/auth/github/callback"
}

variable "github_app_install_url" {
  type    = string
  default = ""
}

variable "github_app_id" {
  type    = string
  default = ""
}

variable "github_app_installation_id" {
  type    = string
  default = ""
}

variable "github_app_private_key" {
  type      = string
  default   = ""
  sensitive = true
}

variable "openai_api_key" {
  type      = string
  default   = ""
  sensitive = true
}

variable "anthropic_api_key" {
  type      = string
  default   = ""
  sensitive = true
}

variable "dashboard_username" {
  type    = string
  default = "admin"
}

variable "dashboard_password" {
  type      = string
  sensitive = true
}

variable "dashboard_session_secret" {
  type      = string
  sensitive = true
}
