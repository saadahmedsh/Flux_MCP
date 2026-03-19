variable "namespace" {
  description = "Kubernetes namespace for Sentinel-MCP"
  type        = string
  default     = "sentinel-system"
}

variable "image_repository" {
  description = "Docker image repository"
  type        = string
  default     = "your-registry/sentinel-mcp"
}

variable "image_tag" {
  description = "Docker image tag"
  type        = string
  default     = "latest"
}
