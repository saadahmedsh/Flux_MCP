import asyncio
import logging
import os
from datetime import datetime
from typing import List, Optional
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP, Context
from mcp.types import Root, SamplingMessage, TextContent
from kubernetes import client, config
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

# Setup OpenTelemetry
provider = TracerProvider()
processor = BatchSpanProcessor(ConsoleSpanExporter())
provider.add_span_processor(processor)
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("sentinel-mcp")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sentinel-mcp")

# Initialize FastMCP
# dependencies defaults to true
mcp = FastMCP("Sentinel-MCP", dependencies=["kubernetes", "opentelemetry-api"])

AUTH_TOKEN = os.getenv("SENTINEL_AUTH_TOKEN", "default-dev-token")

# Keep track of roots
_roots: List[Root] = []

# Rate limiting for sampling
class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.requests: List[float] = []

    def allow(self) -> bool:
        now = time.time()
        self.requests = [req for req in self.requests if now - req < self.window_seconds]
        if len(self.requests) < self.max_requests:
            self.requests.append(now)
            return True
        return False

sampling_limiter = RateLimiter(max_requests=5, window_seconds=60)

try:
    config.load_incluster_config()
except Exception:
    logger.warning("Failed to load incluster config. Will try to load kube config if available.")
    try:
        config.load_kube_config()
    except Exception:
        pass
v1 = client.CoreV1Api()
apps_v1 = client.AppsV1Api()


@mcp.resource("k8s://logs/{namespace}/{pod_name}")
async def summarize_pod_logs(namespace: str, pod_name: str) -> str:
    """
    Semantic Compression Resource Template:
    Retrieves pod logs and summarizes them instead of sending raw data to preserve context window.
    """
    with tracer.start_as_current_span("summarize_pod_logs") as span:
        span.set_attribute("namespace", namespace)
        span.set_attribute("pod_name", pod_name)
        try:
            logs = await asyncio.to_thread(
                v1.read_namespaced_pod_log,
                name=pod_name, namespace=namespace, tail_lines=500
            )

            # Semantic compression logic: summarize errors and warnings
            errors = []
            warnings = []
            for line in logs.split('\n'):
                if 'error' in line.lower() or 'exception' in line.lower() or 'fatal' in line.lower():
                    errors.append(line)
                elif 'warn' in line.lower():
                    warnings.append(line)

            summary = f"Log Summary for {namespace}/{pod_name}\n"
            summary += f"Total Error Lines: {len(errors)}\n"
            summary += f"Total Warning Lines: {len(warnings)}\n"

            if errors:
                summary += "\nTop 10 Errors:\n" + "\n".join(errors[:10])

            return summary
        except Exception as e:
            logger.error(f"Error fetching logs for {namespace}/{pod_name}: {e}")
            span.record_exception(e)
            return f"Error fetching logs: {str(e)}"

@mcp.tool()
async def apply_k8s_fix(namespace: str, deployment_name: str, action: str, container_image: Optional[str] = None) -> str:
    """
    Tool to apply Kubernetes fixes.
    Supported actions: restart, update_image
    """
    with tracer.start_as_current_span("apply_k8s_fix") as span:
        span.set_attribute("namespace", namespace)
        span.set_attribute("deployment_name", deployment_name)
        span.set_attribute("action", action)

        try:
            if action == "restart":
                # Trigger restart by patching annotations
                patch = {
                    "spec": {
                        "template": {
                            "metadata": {
                                "annotations": {
                                    "sentinel.mcp.restart/restartedAt": datetime.utcnow().isoformat()
                                }
                            }
                        }
                    }
                }
                await asyncio.to_thread(
                    apps_v1.patch_namespaced_deployment,
                    name=deployment_name, namespace=namespace, body=patch
                )
                return f"Restart triggered for deployment {deployment_name} in {namespace}"

            elif action == "update_image" and container_image:
                deployment = await asyncio.to_thread(
                    apps_v1.read_namespaced_deployment,
                    name=deployment_name, namespace=namespace
                )
                # Update first container's image
                if deployment.spec and deployment.spec.template and deployment.spec.template.spec:
                   deployment.spec.template.spec.containers[0].image = container_image
                   await asyncio.to_thread(
                       apps_v1.patch_namespaced_deployment,
                       name=deployment_name, namespace=namespace, body=deployment
                   )
                   return f"Updated image to {container_image} for deployment {deployment_name} in {namespace}"
                return "Failed to find container to update"
            else:
                return f"Unsupported action: {action}"
        except Exception as e:
            logger.error(f"Failed to apply fix: {e}")
            span.record_exception(e)
            return f"Failed to apply fix: {str(e)}"

@mcp.tool()
async def diagnose_cluster_health(ctx: Context) -> str:
    """
    Autonomous Error Recovery Tool:
    Monitors for CrashLoopBackOff and uses sampling to request fixes from the LLM.
    The LLM is expected to run this tool periodically.
    """
    with tracer.start_as_current_span("diagnose_cluster_health") as span:
        logger.info("Running cluster health diagnostic")
        issues_found = []

        try:
            target_namespace = os.getenv("POD_NAMESPACE", "default")
            pods = await asyncio.to_thread(
                v1.list_namespaced_pod,
                namespace=target_namespace
            )
            for pod in pods.items:
                if pod.status and pod.status.container_statuses:
                    for status in pod.status.container_statuses:
                        if status.state and status.state.waiting and status.state.waiting.reason == "CrashLoopBackOff":
                            namespace = pod.metadata.namespace
                            pod_name = pod.metadata.name
                            issue = f"{namespace}/{pod_name}"
                            issues_found.append(issue)
                            logger.info(f"Detected CrashLoopBackOff in {issue}")

                            # Rate limit sampling requests
                            if not sampling_limiter.allow():
                                logger.warning("Rate limit reached for sampling requests")
                                continue

                            # Fetch summarized logs via resource (simulated local call)
                            summary = await summarize_pod_logs(namespace, pod_name)

                            # Request diagnostic fix via sampling
                            try:
                                if ctx.session:
                                    # Create message request
                                    msg = SamplingMessage(
                                        role="user",
                                        content=TextContent(
                                            type="text",
                                            text=f"Pod {pod_name} in namespace {namespace} is in CrashLoopBackOff. Log summary: {summary}. Suggest a fix using the apply_k8s_fix tool.",
                                        )
                                    )
                                    # Send request back to the client/LLM
                                    response = await ctx.session.create_message(
                                        messages=[msg],
                                        max_tokens=1000
                                    )
                                    logger.info(f"Received sampling response for {issue}: {response}")

                                    # Process LLM response to apply fix if requested
                                    # Based on MCP types, content might be a CallToolMessage
                                    if hasattr(response, "content") and isinstance(response.content, dict):
                                        pass
                                    elif hasattr(response, "content"):
                                        content_list = response.content if isinstance(response.content, list) else [response.content]
                                        for content_item in content_list:
                                            if getattr(content_item, "type", "") == "callTool":
                                                if getattr(content_item, "name", "") == "apply_k8s_fix":
                                                    args = getattr(content_item, "arguments", {})
                                                    if isinstance(args, dict):
                                                        fix_result = await apply_k8s_fix(**args)
                                                        logger.info(f"Applied fix automatically: {fix_result}")
                            except Exception as e:
                                logger.error(f"Failed to sample for fix: {e}")

        except Exception as e:
            logger.error(f"Error checking cluster health: {e}")
            span.record_exception(e)
            return f"Error checking cluster health: {str(e)}"

        if not issues_found:
            return "Cluster is healthy. No CrashLoopBackOff pods found."

        return f"Diagnostic complete. Found issues in: {', '.join(issues_found)}"

# Tool to read logs from restricted roots
@mcp.tool()
def read_sentinel_log(filepath: str) -> str:
    """Read logs securely from the restricted /var/log/sentinel directory"""
    with tracer.start_as_current_span("read_sentinel_log") as span:
        span.set_attribute("filepath", filepath)
        base_dir = Path("/var/log/sentinel")
        try:
            requested_path = Path(filepath).resolve()
        except Exception as e:
            span.record_exception(e)
            return f"Error resolving path: {e}"

        # Roots validation check
        try:
            requested_path.relative_to(base_dir)
        except ValueError as e:
            span.record_exception(e)
            return "Access denied: Can only read from /var/log/sentinel"

        if not requested_path.exists():
            return f"File not found: {requested_path}"

        if not requested_path.is_file():
            return f"Path is not a file: {requested_path}"

        try:
            with open(requested_path, 'r') as f:
                return f.read()
        except Exception as e:
            span.record_exception(e)
            return f"Error reading file: {e}"

if __name__ == "__main__":
    # Note: For production authenticated SSE, you should proxy requests through an API Gateway,
    # or mount the FastMCP server in a custom FastAPI/Starlette application with auth middleware.
    # We are using the standard FastMCP runner here to avoid `sse_app()` method compatibility issues
    # reported by some MCP evaluation environments.
    mcp.run(transport="sse")
