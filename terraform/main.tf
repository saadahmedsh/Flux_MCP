provider "kubernetes" {
  config_path = "~/.kube/config"
}

provider "helm" {
  kubernetes {
    config_path = "~/.kube/config"
  }
}

# Example GKE cluster provisioning placeholder
# In a real environment, you would use google_container_cluster or eks_cluster modules here

resource "kubernetes_namespace" "sentinel_mcp" {
  metadata {
    name = var.namespace
  }
}

# Deploy Sentinel-MCP using Helm chart
resource "helm_release" "sentinel_mcp" {
  name       = "sentinel-mcp"
  chart      = "../k8s/sentinel-mcp"
  namespace  = kubernetes_namespace.sentinel_mcp.metadata[0].name

  set {
    name  = "image.repository"
    value = var.image_repository
  }

  set {
    name  = "image.tag"
    value = var.image_tag
  }
}
