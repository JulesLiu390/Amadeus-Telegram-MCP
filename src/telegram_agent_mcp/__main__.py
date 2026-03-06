"""CLI entry point for telegram-agent-mcp."""

import argparse
import logging
import sys

from .config import Config
from .server import run_server


def parse_args() -> Config:
    parser = argparse.ArgumentParser(
        prog="telegram-agent-mcp",
        description="MCP Server for Telegram via Bot API",
    )
    parser.add_argument("--token", required=True, help="Telegram Bot API token")
    parser.add_argument(
        "--chat-ids",
        default=None,
        help="Comma-separated chat IDs to monitor (default: all)",
    )
    parser.add_argument(
        "--user-ids",
        default=None,
        help="Comma-separated user IDs to accept private chats from (default: all)",
    )
    parser.add_argument(
        "--buffer-size",
        type=int,
        default=100,
        help="Message buffer size per chat (default: 100)",
    )
    parser.add_argument(
        "--compress-every",
        type=int,
        default=30,
        help="Compress old messages every N new messages (default: 30)",
    )
    parser.add_argument(
        "--polling-timeout",
        type=int,
        default=30,
        help="Long-polling timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["debug", "info", "warning", "error"],
        help="Log level (default: info)",
    )

    args = parser.parse_args()

    chat_ids: set[str] | None = None
    if args.chat_ids:
        chat_ids = set(args.chat_ids.split(","))

    user_ids: set[str] | None = None
    if args.user_ids:
        user_ids = set(args.user_ids.split(","))

    return Config(
        bot_token=args.token,
        chat_ids=chat_ids,
        user_ids=user_ids,
        buffer_size=args.buffer_size,
        compress_every=args.compress_every,
        polling_timeout=args.polling_timeout,
        log_level=args.log_level,
    )


def main() -> None:
    config = parse_args()

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper()),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    run_server(config)


if __name__ == "__main__":
    main()
