"""Integration test: start MCP server subprocess and test via stdio JSON-RPC."""

import asyncio
import json
import os
import sys

import pytest


@pytest.fixture
def project_dir():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


async def _send(proc, msg: dict) -> None:
    line = json.dumps(msg) + "\n"
    proc.stdin.write(line.encode())
    await proc.stdin.drain()


async def _recv_response(proc, timeout: float = 10.0) -> dict:
    """Read lines, skip notifications, return first response with 'id'."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise TimeoutError("No response received")
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
        if not line:
            raise RuntimeError("Server closed stdout")
        msg = json.loads(line)
        if "id" in msg:
            return msg


@pytest.mark.asyncio
@pytest.mark.integration
async def test_mcp_initialize_and_tools_list(project_dir):
    """Test MCP handshake and tool listing via stdio.

    Uses a dummy token — the server starts but Telegram API calls will fail.
    We only test the MCP protocol layer, not actual Telegram connectivity.
    """
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "telegram_agent_mcp",
        "--token", "000000000:FAKE_TOKEN_FOR_TESTING",
        "--log-level", "error",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=project_dir,
        env={**os.environ, "PYTHONPATH": os.path.join(project_dir, "src")},
    )

    try:
        # Wait for server to start
        await asyncio.sleep(2)
        assert proc.returncode is None, "Server exited prematurely"

        # 1. MCP initialize handshake
        await _send(proc, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0.1.0"},
            },
        })
        resp = await _recv_response(proc, timeout=35)
        assert "result" in resp, f"Initialize failed: {resp}"
        server_info = resp["result"].get("serverInfo", {})
        assert server_info.get("name") == "telegram-agent-mcp"

        # Send initialized notification
        await _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        await asyncio.sleep(0.5)

        # 2. List tools
        await _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        resp = await _recv_response(proc, timeout=10)
        assert "result" in resp, f"tools/list failed: {resp}"
        tools = resp["result"].get("tools", [])
        tool_names = sorted(t["name"] for t in tools)
        assert "check_status" in tool_names
        assert "send_message" in tool_names
        assert "get_recent_context" in tool_names
        assert "batch_get_recent_context" in tool_names
        assert "compress_context" in tool_names
        assert len(tool_names) == 5

        # 3. Call check_status — will report bot_running=False (fake token)
        await _send(proc, {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "check_status", "arguments": {}},
        })
        resp = await _recv_response(proc, timeout=10)
        assert "result" in resp
        content = resp["result"].get("content", [])
        assert len(content) > 0
        # Parse the tool result
        data = json.loads(content[0]["text"])
        # With fake token, bot_running should be False
        assert data.get("bot_running") is False

    finally:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            proc.kill()
