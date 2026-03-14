"""
Executes LLM-generated Python code with injected MCP tool wrappers.
"""

import asyncio
import json
import textwrap
import traceback
from typing import Any

from mcp import ClientSession
from mcp.types import Tool

from .config_reader import server_name_to_py


class _ToolNamespace:
    """Proxy object representing a single MCP server's tools in exec namespace."""

    def __init__(self, server_name: str, session: ClientSession, tools: list[Tool]):
        self._server_name = server_name
        self._session = session
        self._tools = {t.name: t for t in tools}

        for tool in tools:
            setattr(self, tool.name, self._make_wrapper(tool.name))

    def _make_wrapper(self, tool_name: str):
        session = self._session
        server = self._server_name

        async def wrapper(**kwargs):
            result = await session.call_tool(tool_name, kwargs)
            texts = []
            for content in result.content:
                if hasattr(content, "text"):
                    texts.append(content.text)
                elif hasattr(content, "data"):
                    texts.append(json.dumps(content.data, ensure_ascii=False))
            combined = "\n".join(texts)
            # Try to parse as JSON for nicer access
            if combined.strip().startswith(("{", "[")):
                try:
                    return json.loads(combined)
                except json.JSONDecodeError:
                    pass
            return combined

        wrapper.__name__ = tool_name
        wrapper.__qualname__ = f"{server}.{tool_name}"
        doc = self._tools[tool_name].description or ""
        wrapper.__doc__ = doc
        return wrapper

    def __repr__(self):
        tool_names = list(self._tools.keys())
        return f"<MCP:{self._server_name} tools={tool_names}>"


class CodeExecutor:
    def __init__(self, pool):
        self.pool = pool

    def _build_namespace(self) -> dict[str, Any]:
        namespace: dict[str, Any] = {
            "__builtins__": __builtins__,
            "asyncio": asyncio,
            "json": json,
        }

        for server_name, session in self.pool.sessions.items():
            py_name = server_name_to_py(server_name)
            tools = self.pool.tools.get(server_name, [])
            namespace[py_name] = _ToolNamespace(server_name, session, tools)

        return namespace

    async def execute(self, code: str, timeout: float = 60.0) -> dict[str, Any]:
        namespace = self._build_namespace()

        output_lines: list[str] = []

        def captured_print(*args, sep=" ", end="\n", **kwargs):
            output_lines.append(sep.join(str(a) for a in args) + end)

        namespace["print"] = captured_print

        # Wrap user code in an async function to allow top-level await
        indented = textwrap.indent(code, "    ")
        wrapped = f"async def __user_code__():\n{indented}\n"

        try:
            exec(wrapped, namespace)
        except SyntaxError as e:
            return {
                "success": False,
                "error": f"SyntaxError: {e}",
                "output": "",
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"CompileError: {e}\n{traceback.format_exc()}",
                "output": "",
            }

        try:
            result = await asyncio.wait_for(
                namespace["__user_code__"](),
                timeout=timeout,
            )
            output = "".join(output_lines)
            if result is not None:
                if isinstance(result, (dict, list)):
                    output += json.dumps(result, ensure_ascii=False, indent=2)
                else:
                    output += str(result)
            return {"success": True, "output": output, "error": None}

        except asyncio.TimeoutError:
            return {
                "success": False,
                "error": f"Execution timed out after {timeout}s",
                "output": "".join(output_lines),
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"{type(e).__name__}: {e}\n{traceback.format_exc()}",
                "output": "".join(output_lines),
            }
