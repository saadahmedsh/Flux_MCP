"""
Sentinel-MCP Server.

Exposes Kubernetes monitoring and remediation tools via the Model Context Protocol.
Connects to a local or in-cluster Kubernetes API and provides tools for:
- Diagnosing unhealthy pods (CrashLoopBackOff, ImagePullBackOff)
- Applying fixes to deployments (restart, image update)
- Reading logs from a restricted directory
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from kubernetes import client, config
from mcp.server.fastmcp import FastMCP, Context
from mcp.types import Root, SamplingMessage, TextContent
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

UNHEALTHY_REASONS: List[str] = [
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "ErrImagePull",
]

provider: TracerProvider = TracerProvider()
processor: BatchSpanProcessor = BatchSpanProcessor(ConsoleSpanExporter())
provider.add_span_processor(processor)
trace.set_tracer_provider(provider)
tracer: trace.Tracer = trace.get_tracer("sentinel-mcp")

logging.basicConfig(level=logging.INFO)
logger: logging.Logger = logging.getLogger("sentinel-mcp")

mcp: FastMCP = FastMCP("Sentinel-MCP", dependencies=["kubernetes", "opentelemetry-api"])

AUTH_TOKEN: str = os.getenv("SENTINEL_AUTH_TOKEN", "default-dev-token")

_roots: List[Root] = []


class RateLimiter:
    """Token-bucket style rate limiter for sampling requests."""

    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self.max_requests: int = max_requests
        self.window_seconds: int = window_seconds
        self.requests: List[float] = []

    def allow(self) -> bool:
        """Return True if a request is allowed within the current window."""
        now: float = time.time()
        self.requests = [r for r in self.requests if now - r < self.window_seconds]
        if len(self.requests) < self.max_requests:
            self.requests.append(now)
            return True
        return False


sampling_limiter: RateLimiter = RateLimiter(max_requests=5, window_seconds=60)

try:
    config.load_incluster_config()
except Exception:
    logger.warning("Failed to load incluster config. Trying local kube config.")
    try:
        config.load_kube_config()
    except Exception:
        logger.warning("No Kubernetes configuration found.")

v1: client.CoreV1Api = client.CoreV1Api()
apps_v1: client.AppsV1Api = client.AppsV1Api()


@mcp.resource("k8s://logs/{namespace}/{pod_name}")
async def summarize_pod_logs(namespace: str, pod_name: str) -> str:
    """
    Retrieve pod logs and produce a compressed summary.

    Only errors and warnings are extracted to preserve context window budget
    when forwarding to an LLM.
    """
    with tracer.start_as_current_span("summarize_pod_logs") as span:
        span.set_attribute("namespace", namespace)
        span.set_attribute("pod_name", pod_name)
        try:
            logs: str = await asyncio.to_thread(
                v1.read_namespaced_pod_log,
                name=pod_name,
                namespace=namespace,
                tail_lines=500,
            )

            errors: List[str] = []
            warnings: List[str] = []
            for line in logs.split("\n"):
                lower: str = line.lower()
                if "error" in lower or "exception" in lower or "fatal" in lower:
                    errors.append(line)
                elif "warn" in lower:
                    warnings.append(line)

            summary: str = (
                f"Log Summary for {namespace}/{pod_name}\n"
                f"Total Error Lines: {len(errors)}\n"
                f"Total Warning Lines: {len(warnings)}\n"
            )
            if errors:
                summary += "\nTop 10 Errors:\n" + "\n".join(errors[:10])

            return summary
        except Exception as exc:
            logger.error("Error fetching logs for %s/%s: %s", namespace, pod_name, exc)
            span.record_exception(exc)
            return f"Error fetching logs: {exc}"


@mcp.tool()
async def apply_k8s_fix(
    namespace: str,
    deployment_name: str,
    action: str,
    container_image: Optional[str] = None,
) -> str:
    """
    Apply a remediation action to a Kubernetes deployment.

    Supported actions:
        restart      - Trigger a rolling restart via annotation patch.
        update_image - Update the first container image to the given value.
    """
    with tracer.start_as_current_span("apply_k8s_fix") as span:
        span.set_attribute("namespace", namespace)
        span.set_attribute("deployment_name", deployment_name)
        span.set_attribute("action", action)

        try:
            if action == "restart":
                patch: dict = {
                    "spec": {
                        "template": {
                            "metadata": {
                                "annotations": {
                                    "sentinel.mcp.restart/restartedAt": datetime.now(
                                        timezone.utc
                                    ).isoformat()
                                }
                            }
                        }
                    }
                }
                await asyncio.to_thread(
                    apps_v1.patch_namespaced_deployment,
                    name=deployment_name,
                    namespace=namespace,
                    body=patch,
                )
                return (
                    f"Restart triggered for deployment {deployment_name} "
                    f"in {namespace}"
                )

            if action == "update_image":
                if not container_image:
                    return "update_image requires a container_image argument"
                deployment = await asyncio.to_thread(
                    apps_v1.read_namespaced_deployment,
                    name=deployment_name,
                    namespace=namespace,
                )
                containers = (
                    deployment.spec.template.spec.containers
                    if deployment.spec
                    and deployment.spec.template
                    and deployment.spec.template.spec
                    else None
                )
                if not containers:
                    return "Failed to locate containers in deployment spec"

                containers[0].image = container_image
                await asyncio.to_thread(
                    apps_v1.patch_namespaced_deployment,
                    name=deployment_name,
                    namespace=namespace,
                    body=deployment,
                )
                return (
                    f"Updated image to {container_image} for deployment "
                    f"{deployment_name} in {namespace}"
                )

            return f"Unsupported action: {action}. Use 'restart' or 'update_image'."
        except Exception as exc:
            logger.error("Failed to apply fix: %s", exc)
            span.record_exception(exc)
            return f"Failed to apply fix: {exc}"


@mcp.tool()
async def diagnose_cluster_health(
    ctx: Context, manual_testing: bool = False
) -> str:
    """
    Scan the target namespace for unhealthy pods and optionally request an LLM fix.

    When manual_testing is True the LLM sampling step is skipped so the tool
    can be exercised from the MCP Inspector without timing out.
    """
    with tracer.start_as_current_span("diagnose_cluster_health") as span:
        logger.info("Running cluster health diagnostic")
        issues_found: List[str] = []

        try:
            target_namespace: str = os.getenv("POD_NAMESPACE", "default")
            pods = await asyncio.to_thread(
                v1.list_namespaced_pod, namespace=target_namespace
            )
            for pod in pods.items:
                if not (pod.status and pod.status.container_statuses):
                    continue
                for status in pod.status.container_statuses:
                    if not (status.state and status.state.waiting):
                        continue
                    if status.state.waiting.reason not in UNHEALTHY_REASONS:
                        continue

                    ns: str = pod.metadata.namespace
                    pod_name: str = pod.metadata.name
                    issue: str = f"{ns}/{pod_name}"
                    issues_found.append(issue)
                    logger.info("Detected %s in %s", status.state.waiting.reason, issue)

                    if not sampling_limiter.allow():
                        logger.warning("Rate limit reached for sampling requests")
                        continue

                    summary: str = await summarize_pod_logs(ns, pod_name)

                    if manual_testing:
                        logger.info("Manual mode: skipping LLM sampling for %s", issue)
                        continue

                    try:
                        if ctx.session:
                            msg: SamplingMessage = SamplingMessage(
                                role="user",
                                content=TextContent(
                                    type="text",
                                    text=(
                                        f"Pod {pod_name} in namespace {ns} is "
                                        f"unhealthy. Log summary: {summary}. "
                                        f"Suggest a fix using apply_k8s_fix."
                                    ),
                                ),
                            )
                            response = await ctx.session.create_message(
                                messages=[msg], max_tokens=1000
                            )
                            logger.info(
                                "Sampling response for %s: %s", issue, response
                            )
                    except Exception as exc:
                        logger.error("Failed to sample for fix: %s", exc)

        except Exception as exc:
            logger.error("Error checking cluster health: %s", exc)
            span.record_exception(exc)
            return f"Error checking cluster health: {exc}"

        if not issues_found:
            return "Cluster is healthy. No unhealthy pods found."

        return f"Diagnostic complete. Found issues in: {', '.join(issues_found)}"


@mcp.tool()
def read_sentinel_log(filepath: str) -> str:
    """
    Read a log file from the restricted sentinel log directory.

    The directory is configured via the SENTINEL_LOG_DIR environment variable
    (default: ./sentinel_logs). Path traversal outside this directory is denied.
    """
    with tracer.start_as_current_span("read_sentinel_log") as span:
        span.set_attribute("filepath", filepath)

        log_dir: str = os.getenv("SENTINEL_LOG_DIR", "./sentinel_logs")
        base_dir: Path = Path(log_dir).resolve()
        base_dir.mkdir(parents=True, exist_ok=True)

        try:
            requested_path: Path = Path(filepath).resolve()
        except Exception as exc:
            span.record_exception(exc)
            return f"Error resolving path: {exc}"

        try:
            requested_path.relative_to(base_dir)
        except ValueError as exc:
            span.record_exception(exc)
            return f"Access denied: path must be inside {base_dir}"

        if not requested_path.exists():
            return f"File not found: {requested_path}"

        if not requested_path.is_file():
            return f"Path is not a file: {requested_path}"

        try:
            return requested_path.read_text(encoding="utf-8")
        except Exception as exc:
            span.record_exception(exc)
            return f"Error reading file: {exc}"


if __name__ == "__main__":
    from mcp.server.transport_security import TransportSecuritySettings

    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = 8000
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    )
    mcp.run(transport="sse")
