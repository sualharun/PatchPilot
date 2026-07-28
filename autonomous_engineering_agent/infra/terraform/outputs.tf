output "namespace" {
  value = kubernetes_namespace_v1.patchpilot.metadata[0].name
}

output "api_service_url_hint" {
  value = "kubectl -n ${kubernetes_namespace_v1.patchpilot.metadata[0].name} port-forward svc/patchpilot-api 8080:80"
}

output "pr_worker_scale_hint" {
  value = "kubectl -n ${kubernetes_namespace_v1.patchpilot.metadata[0].name} scale deployment/patchpilot-pr-worker --replicas=4"
}
