"""MCP Server main entry — registers tools, manages lifecycle."""

import asyncio
import logging
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP

from .config import Config
from .context import ContextManager
from .telegram_api import TelegramClient
from .tools import register_tools

logger = logging.getLogger(__name__)

MAX_READY_WAIT = 30  # seconds to wait for Telegram API to respond


async def _wait_ready(bot: TelegramClient, timeout: float = MAX_READY_WAIT) -> dict | None:
    """Call getMe until reachable or timeout. Returns bot info on success."""
    deadline = asyncio.get_event_loop().time() + timeout
    attempt = 0
    while asyncio.get_event_loop().time() < deadline:
        attempt += 1
        try:
            me = await bot.get_me()
            logger.info(
                "Telegram bot ready (@%s, id=%s) after %d attempts",
                me.get("username"), me.get("id"), attempt,
            )
            return me
        except Exception as e:
            logger.debug("wait_ready attempt %d: %s", attempt, e)
            await asyncio.sleep(min(2.0, deadline - asyncio.get_event_loop().time()))
    logger.warning("Telegram API not reachable after %.0fs — starting anyway", timeout)
    return None


def create_server(config: Config) -> FastMCP:
    """Create and configure the MCP Server."""
    bot = TelegramClient(config.api_base_url)
    ctx = ContextManager(config, bot=bot)

    @asynccontextmanager
    async def lifespan(app: FastMCP):
        me = await _wait_ready(bot)
        bot_username = me.get("username", "") if me else ""
        ctx.start(bot_username=bot_username)
        logger.info("Long-polling listener started (bot: @%s)", bot_username)
        try:
            yield {}
        finally:
            await ctx.stop()
            await bot.close()
            logger.info("Context manager and bot client stopped")

    mcp = FastMCP("telegram-agent-mcp", lifespan=lifespan)

    register_tools(mcp, config, bot, ctx)

    return mcp


def run_server(config: Config) -> None:
    """Start the MCP Server with stdio transport (blocking)."""
    mcp = create_server(config)
    logger.info("Starting MCP Server (bot_id: %s)", config.bot_id)
    mcp.run(transport="stdio")
