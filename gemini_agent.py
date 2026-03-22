"""
Gemini Agent for Sentinel-MCP.

Connects to the Sentinel-MCP server via SSE, discovers available tools,
and runs an interactive chat loop powered by the Gemini API.
Gemini decides which tools to call and the agent forwards those calls
to the MCP server, returning results back to Gemini until a final
text answer is produced.

Model: gemini-2.5-flash
"""

import asyncio
import logging
import os
import sys
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from google import genai
from mcp import ClientSession
from mcp.client.sse import sse_client

load_dotenv()

GEMINI_API_KEY: Optional[str] = os.getenv("GEMINI_API_KEY")
MCP_SERVER_URL: str = os.getenv("MCP_SERVER_URL", "http://localhost:8000/sse")
MODEL_ID: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
LOG_DIR: Path = Path("./logs")
LOG_DIR.mkdir(exist_ok=True)

SYSTEM_INSTRUCTION: str = (
    "You are an autonomous Kubernetes SRE. "
    "Use the diagnose_cluster_health tool to find issues "
    "and apply_k8s_fix to resolve them. Explain your steps."
)

if not GEMINI_API_KEY or GEMINI_API_KEY == "your-gemini-api-key-here":
    print("ERROR: Set GEMINI_API_KEY in the .env file.")
    sys.exit(1)

gemini_client: genai.Client = genai.Client(api_key=GEMINI_API_KEY)


def _configure_logging() -> logging.Logger:
    """Configure file and console logging and return the agent logger."""
    log_file: Path = LOG_DIR / f"agent_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    file_handler: logging.FileHandler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s")
    )

    console_handler: logging.StreamHandler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s")
    )

    logging.basicConfig(level=logging.DEBUG, handlers=[file_handler, console_handler])

    for noisy in ("urllib3", "google", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("mcp").setLevel(logging.INFO)

    agent_logger: logging.Logger = logging.getLogger("gemini-agent")
    agent_logger.info("Log file: %s", log_file)
    return agent_logger


logger: logging.Logger = _configure_logging()


def build_tool_declarations(mcp_tools: List[Any]) -> List[Dict[str, Any]]:
    """
    Convert MCP tool objects to Gemini function declaration dicts.

    Each MCP tool exposes a name, description, and JSON Schema for its
    input parameters. This function maps them into the format expected
    by the google-genai SDK.
    """
    declarations: List[Dict[str, Any]] = []
    for tool in mcp_tools:
        decl: Dict[str, Any] = {
            "name": tool.name,
            "description": tool.description or tool.name,
            "parameters": tool.inputSchema if tool.inputSchema else None,
        }
        declarations.append(decl)
        logger.info("Registered tool: %s", tool.name)
    return [{"function_declarations": declarations}]


async def _execute_tool(
    session: ClientSession, name: str, args: Optional[Dict[str, Any]]
) -> str:
    """
    Call a single MCP tool and return its text result.

    Errors are caught and returned as a string so the conversation can
    continue even when a tool fails.
    """
    logger.info("Tool call: %s(%s)", name, args)
    try:
        result = await session.call_tool(name, arguments=args or {})
        texts: List[str] = [
            c.text if hasattr(c, "text") else str(c) for c in result.content
        ]
        output: str = "\n".join(texts)
        logger.info("Tool result [%s]: %s", name, output)
        return output
    except Exception as exc:
        logger.error("Tool error [%s]: %s", name, exc)
        return f"Error: {exc}"


async def _read_input(prompt: str) -> Optional[str]:
    """
    Read user input without blocking the async event loop.

    Running input() in a thread executor prevents the SSE connection
    from being starved on Windows.
    """
    loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, partial(input, prompt))
    except (EOFError, KeyboardInterrupt):
        return None


async def _handle_response(
    session: ClientSession,
    response: Any,
    chat_history: List[Any],
    gemini_tools: List[Dict[str, Any]],
) -> None:
    """
    Process a Gemini response, executing tool calls in a loop until
    Gemini produces a final text answer.
    """
    current = response
    while True:
        if current.text:
            logger.info("Gemini: %s", current.text)
            print(f"\nGemini > {current.text}\n")
            chat_history.append(current.candidates[0].content)
            return

        parts = current.candidates[0].content.parts
        tool_calls = [p.function_call for p in parts if p.function_call]

        if not tool_calls:
            return

        tool_responses: List[Dict[str, Any]] = []
        for fc in tool_calls:
            result_text: str = await _execute_tool(session, fc.name, fc.args)
            tool_responses.append({
                "role": "tool",
                "parts": [{
                    "function_response": {
                        "name": fc.name,
                        "response": {"result": result_text},
                    }
                }],
            })

        chat_history.append(current.candidates[0].content)
        chat_history.extend(tool_responses)

        current = gemini_client.models.generate_content(
            model=MODEL_ID,
            contents=chat_history,
            config={
                "tools": gemini_tools,
                "system_instruction": SYSTEM_INSTRUCTION,
            },
        )


async def run_agent() -> None:
    """
    Connect to the MCP server, discover tools, and run an interactive
    chat loop where Gemini can call MCP tools autonomously.
    """
    logger.info("Starting Gemini Agent")
    logger.info("MCP Server: %s", MCP_SERVER_URL)
    logger.info("Model: %s", MODEL_ID)

    async with sse_client(MCP_SERVER_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            logger.info("MCP session initialized")

            tools_result = await session.list_tools()
            mcp_tools = tools_result.tools
            logger.info("Discovered %d tools", len(mcp_tools))

            gemini_tools: List[Dict[str, Any]] = build_tool_declarations(mcp_tools)

            print("\n" + "=" * 60)
            print(f"Gemini Agent ready  |  Model: {MODEL_ID}")
            print("Type 'quit' to exit.")
            print("=" * 60 + "\n")

            chat_history: List[Any] = []

            while True:
                raw: Optional[str] = await _read_input("You > ")
                if raw is None:
                    print("\nSession ended.")
                    break

                user_input: str = raw.strip()
                if not user_input or user_input.lower() in ("quit", "exit", "q"):
                    break

                logger.info("User: %s", user_input)
                chat_history.append({"role": "user", "parts": [{"text": user_input}]})

                try:
                    response = gemini_client.models.generate_content(
                        model=MODEL_ID,
                        contents=chat_history,
                        config={
                            "tools": gemini_tools,
                            "system_instruction": SYSTEM_INSTRUCTION,
                        },
                    )
                    await _handle_response(
                        session, response, chat_history, gemini_tools
                    )
                except Exception as exc:
                    logger.error("Generation error: %s", exc)
                    print(f"Error: {exc}")


if __name__ == "__main__":
    asyncio.run(run_agent())
