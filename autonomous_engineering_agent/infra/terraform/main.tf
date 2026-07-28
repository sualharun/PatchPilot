provider "kubernetes" {
  config_path    = pathexpand(var.kubeconfig_path)
  config_context = var.kube_context
}

locals {
  namespace = var.project_name
  labels = {
    app     = var.project_name
    region  = var.region
    managed = "terraform"
  }
}

resource "kubernetes_namespace_v1" "patchpilot" {
  metadata {
    name   = local.namespace
    labels = local.labels
  }
}

resource "kubernetes_config_map_v1" "patchpilot" {
  metadata {
    name      = "patchpilot-config"
    namespace = kubernetes_namespace_v1.patchpilot.metadata[0].name
    labels    = local.labels
  }

  data = {
    DATABASE_URL                  = "postgresql://patchpilot:patchpilot@postgres:5432/patchpilot"
    AGENT_LOGS_DIR                = "/data/logs"
    ARTIFACT_STORAGE_DIR          = "/data/artifacts"
    DASHBOARD_AUTH_ENABLED        = "true"
    DASHBOARD_SECURE_COOKIES      = "true"
    DASHBOARD_DEMO_DATA_ENABLED   = "false"
    PATCHPILOT_PRODUCTION         = "true"
    PATCHPILOT_WORKER_LEASE_SECONDS = "900"
    PATCHPILOT_WORKER_MAX_ATTEMPTS  = "3"
    KAFKA_BOOTSTRAP_SERVERS       = "redpanda:9092"
    KAFKA_PR_ANALYSIS_TOPIC       = "pr-analysis-jobs"
    KAFKA_CONSUMER_GROUP          = "patchpilot-pr-workers"
    PR_ANALYSIS_STATUS_CONTEXT    = "patchpilot/pr-analysis"
  }
}

resource "kubernetes_secret_v1" "patchpilot" {
  metadata {
    name      = "patchpilot-secrets"
    namespace = kubernetes_namespace_v1.patchpilot.metadata[0].name
    labels    = local.labels
  }

  data = {
    GITHUB_TOKEN              = var.github_token
    GITHUB_WEBHOOK_SECRET     = var.github_webhook_secret
    GITHUB_OAUTH_CLIENT_ID    = var.github_oauth_client_id
    GITHUB_OAUTH_CLIENT_SECRET = var.github_oauth_client_secret
    GITHUB_OAUTH_CALLBACK_URL = var.github_oauth_callback_url
    GITHUB_APP_INSTALL_URL    = var.github_app_install_url
    GITHUB_APP_ID             = var.github_app_id
    GITHUB_APP_INSTALLATION_ID = var.github_app_installation_id
    GITHUB_APP_PRIVATE_KEY    = var.github_app_private_key
    OPENAI_API_KEY            = var.openai_api_key
    ANTHROPIC_API_KEY         = var.anthropic_api_key
    DASHBOARD_USERNAME        = var.dashboard_username
    DASHBOARD_PASSWORD        = var.dashboard_password
    DASHBOARD_SESSION_SECRET  = var.dashboard_session_secret
    POSTGRES_PASSWORD         = "patchpilot"
  }
}

resource "null_resource" "workloads" {
  count = var.apply_workloads ? 1 : 0

  triggers = {
    image                 = var.patchpilot_image
    api_replicas          = tostring(var.api_replicas)
    pr_worker_replicas    = tostring(var.pr_worker_replicas)
    agent_worker_replicas = tostring(var.agent_worker_replicas)
  }

  provisioner "local-exec" {
    command = <<EOT
kubectl apply -f ${path.module}/../../deploy/kubernetes/postgres.yaml
kubectl apply -f ${path.module}/../../deploy/kubernetes/redpanda.yaml
kubectl apply -f ${path.module}/../../deploy/kubernetes/api.yaml
kubectl apply -f ${path.module}/../../deploy/kubernetes/agent-worker.yaml
kubectl apply -f ${path.module}/../../deploy/kubernetes/pr-worker.yaml
kubectl -n ${local.namespace} set image deployment/patchpilot-api api=${var.patchpilot_image}
kubectl -n ${local.namespace} set image deployment/patchpilot-agent-worker worker=${var.patchpilot_image}
kubectl -n ${local.namespace} set image deployment/patchpilot-pr-worker pr-worker=${var.patchpilot_image}
kubectl -n ${local.namespace} scale deployment/patchpilot-api --replicas=${var.api_replicas}
kubectl -n ${local.namespace} scale deployment/patchpilot-agent-worker --replicas=${var.agent_worker_replicas}
kubectl -n ${local.namespace} scale deployment/patchpilot-pr-worker --replicas=${var.pr_worker_replicas}
EOT
  }

  depends_on = [
    kubernetes_config_map_v1.patchpilot,
    kubernetes_secret_v1.patchpilot,
  ]
}
