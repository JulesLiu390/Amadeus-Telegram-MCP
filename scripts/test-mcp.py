#!/usr/bin/env python3
"""
Telegram MCP 连接测试脚本

启动 MCP Server 子进程，通过 stdio JSON-RPC 协议依次执行：
  1. initialize 握手
  2. tools/list  列出所有工具
  3. check_status 检查机器人状态

用法:
  python3 scripts/test-mcp.py                          # 自动从 .env 读取 token
  python3 scripts/test-mcp.py --token <BOT_TOKEN>      # 手动指定
"""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys

# 颜色
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RESET = "\033[0m"
BOLD = "\033[1m"


def ok(msg: str) -> None:
    print(f"  {GREEN}✅ {msg}{RESET}")


def fail(msg: str) -> None:
    print(f"  {RED}❌ {msg}{RESET}")


def info(msg: str) -> None:
    print(f"  {CYAN}ℹ️  {msg}{RESET}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}⚠️  {msg}{RESET}")


async def send(proc, msg: dict) -> None:
    line = json.dumps(msg) + "\n"
    proc.stdin.write(line.encode())
    await proc.stdin.drain()


async def recv_response(proc, timeout: float = 10.0) -> dict:
    """读取响应，跳过通知消息。"""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise TimeoutError("等待响应超时")
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
        if not line:
            raise RuntimeError("Server 关闭了 stdout")
        msg = json.loads(line)
        if "id" in msg:
            return msg


def _find_run_cmd() -> list[str] | None:
    """查找 MCP Server 启动命令。优先 uv，回退 python -m。"""
    if shutil.which("uv"):
        return ["uv", "run", "telegram-agent-mcp"]
    try:
        subprocess.run(
            [sys.executable, "-c", "import telegram_agent_mcp"],
            capture_output=True, check=True,
        )
        return [sys.executable, "-m", "telegram_agent_mcp"]
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return None


def _read_token_from_env() -> str | None:
    """从 .env 读取 TELEGRAM_BOT_TOKEN。"""
    env_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        ".env",
    )
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    return line.split("=", 1)[1].strip()
    except FileNotFoundError:
        pass
    return None


async def main(token: str) -> int:
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(project_dir)

    print(f"\n{BOLD}=== Telegram MCP 连接测试 ==={RESET}\n")

    # ── 启动 MCP Server ─────────────────────────────────────
    print(f"{BOLD}[1/4] 启动 MCP Server ...{RESET}")
    cmd = _find_run_cmd()
    if cmd is None:
        fail("找不到 uv 或 telegram-agent-mcp，请先运行 scripts/setup.sh")
        return 1

    args_cmd = cmd + ["--token", token]
    info(f"启动命令: {args_cmd[0]} ... --token {token[:10]}...")

    try:
        proc = await asyncio.create_subprocess_exec(
            *args_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        fail(f"命令不存在: {args_cmd[0]}")
        return 1

    await asyncio.sleep(2)

    if proc.returncode is not None:
        stderr = (await proc.stderr.read()).decode()
        fail(f"Server 启动失败 (exit code {proc.returncode})")
        print(f"    stderr: {stderr[:500]}")
        return 1

    ok("Server 进程已启动")
    passed = 0
    total = 3

    try:
        # ── 测试 1: MCP initialize 握手 ──────────────────────
        print(f"\n{BOLD}[2/4] MCP 协议握手 (initialize) ...{RESET}")
        await send(proc, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-mcp", "version": "0.1.0"},
            },
        })
        resp = await recv_response(proc, timeout=35)

        if "result" in resp:
            server_info = resp["result"].get("serverInfo", {})
            protocol = resp["result"].get("protocolVersion", "?")
            ok(f"握手成功 — server: {server_info.get('name', '?')}, protocol: {protocol}")
            passed += 1
        else:
            error = resp.get("error", {})
            fail(f"握手失败: {error.get('message', resp)}")

        await send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        await asyncio.sleep(0.5)

        # ── 测试 2: 列出工具 ─────────────────────────────────
        print(f"\n{BOLD}[3/4] 获取工具列表 (tools/list) ...{RESET}")
        await send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        resp = await recv_response(proc, timeout=10)

        if "result" in resp:
            tools = resp["result"].get("tools", [])
            tool_names = [t["name"] for t in tools]
            ok(f"获取到 {len(tools)} 个工具: {', '.join(tool_names)}")
            passed += 1
        else:
            fail(f"获取工具列表失败: {resp.get('error', {}).get('message', resp)}")

        # ── 测试 3: check_status ─────────────────────────────
        print(f"\n{BOLD}[4/4] 调用 check_status ...{RESET}")
        await send(proc, {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "check_status", "arguments": {}},
        })
        resp = await recv_response(proc, timeout=15)

        if "result" in resp:
            contents = resp["result"].get("content", [])
            for c in contents:
                if c.get("type") == "text":
                    data = json.loads(c["text"])
                    bot_running = data.get("bot_running", False)
                    username = data.get("bot_username", "")
                    buffers = data.get("active_buffers", 0)

                    if bot_running:
                        ok(f"Bot 运行中 — @{username}")
                        ok(f"活跃缓冲区: {buffers}")
                        passed += 1
                    else:
                        warn("Bot 未运行（token 可能无效或网络不通）")
                        info("请检查 token 是否正确，以及网络是否能访问 api.telegram.org")
        else:
            fail(f"check_status 调用失败: {resp.get('error', {}).get('message', resp)}")

    except TimeoutError as e:
        fail(f"超时: {e}")
    except Exception as e:
        fail(f"异常: {e}")
    finally:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            proc.kill()

    # ── 汇总 ─────────────────────────────────────────────────
    print(f"\n{BOLD}=== 测试结果: {passed}/{total} 通过 ==={RESET}")
    if passed == total:
        print(f"{GREEN}🎉 MCP Server 连接正常，一切就绪！{RESET}\n")
        return 0
    else:
        print(f"{YELLOW}部分测试未通过，请检查上方输出。{RESET}\n")
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="测试 Telegram MCP Server 连接")
    parser.add_argument("--token", default=None, help="Bot Token (默认从 .env 读取)")
    args = parser.parse_args()

    token = args.token or _read_token_from_env()
    if not token:
        fail("未指定 Token。请使用 --token 参数，或先运行 scripts/setup.sh 生成 .env")
        sys.exit(1)

    sys.exit(asyncio.run(main(token)))
