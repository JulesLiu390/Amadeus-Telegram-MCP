# Telegram-Agent-MCP

MCP Server，通过 Telegram Bot API 让 AI 客户端收发 Telegram 消息。支持群聊和私聊。

## 功能

- **5 个 MCP 工具**：`check_status`、`get_recent_context`、`batch_get_recent_context`、`send_message`、`compress_context`
- Long-polling 实时消息监听 + 自动重连
- 消息按自然语义分段发送（句号/逗号/破折号等），模拟真人打字节奏
- 支持 AI 自主控制消息拆分段数（`num_chunks` 参数）
- 发送前自动显示 "正在输入..." 状态
- Chat / 用户白名单控制
- 发送速率限制（3s/目标）
- 重复消息检测（60s 窗口）

## 前置条件

- Python 3.11+
- [uv](https://github.com/astral-sh/uv)
- 一个 Telegram Bot Token（从 [@BotFather](https://t.me/BotFather) 获取）

## 快速开始

### 1. 安装依赖

```bash
uv sync
```

### 2. 获取 Bot Token

1. 在 Telegram 中找到 @BotFather
2. 发送 `/newbot`，按提示完成创建
3. 复制 Bot Token（格式：`123456789:ABCdefGHI...`）

### 3. 启动 MCP Server

```bash
# 监听所有聊天
uv run telegram-agent-mcp --token YOUR_BOT_TOKEN

# 指定监听的聊天
uv run telegram-agent-mcp --token YOUR_BOT_TOKEN --chat-ids -100123456,-100789012

# 全部参数
uv run telegram-agent-mcp --token YOUR_BOT_TOKEN \
  --chat-ids -100123456,-100789012 \
  --user-ids 111222333 \
  --buffer-size 100 \
  --polling-timeout 30 \
  --log-level info
```

### 4. 配置 MCP 客户端

将以下内容添加到你的 AI 客户端的 MCP 配置中：

```json
{
  "mcpServers": {
    "telegram-agent": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/telegram-mcp", "telegram-agent-mcp", "--token", "YOUR_BOT_TOKEN"]
    }
  }
}
```

---

## CLI 参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--token` (必填) | — | Telegram Bot API Token |
| `--chat-ids` | 全部 | 监听的聊天 ID（逗号分隔） |
| `--user-ids` | 全部 | 接受私聊的用户 ID（逗号分隔） |
| `--buffer-size` | `100` | 每个聊天的消息缓冲区大小 |
| `--compress-every` | `30` | 每 N 条消息触发压缩提示 |
| `--polling-timeout` | `30` | Long-polling 超时秒数 |
| `--log-level` | `info` | 日志级别 |

## MCP 工具

| 工具 | 说明 |
|------|------|
| `check_status` | 检查 Bot 运行状态、监控的聊天列表 |
| `get_recent_context(chat_id, limit?)` | 获取消息上下文（JSON 格式，含 is_self/is_at_me 标记） |
| `batch_get_recent_context(chat_ids, limit?)` | 批量查询多个聊天的消息上下文 |
| `send_message(chat_id, content, reply_to?, split_content?, num_chunks?)` | 发消息，自动分段+打字延迟 |
| `compress_context(chat_id)` | 压缩历史消息为摘要 |

## 架构

```
MCP Client (stdio)
  ↕ JSON-RPC
telegram-agent-mcp (Python)
  ├── HTTP API → Telegram Bot API
  └── Long Polling ← Telegram (消息更新)
       ↓
  Message Buffer (滑动窗口)
```

## 目录结构

```
telegram-mcp/
├── src/telegram_agent_mcp/     # MCP Server 源码
│   ├── __main__.py             # CLI 入口
│   ├── config.py               # 配置
│   ├── server.py               # MCP Server 生命周期
│   ├── telegram_api.py         # Telegram Bot API 客户端
│   ├── context.py              # 消息缓冲区 + Long-polling
│   └── tools.py                # MCP 工具定义
├── mcp.json                    # MCP 客户端配置示例
├── pyproject.toml              # Python 项目配置
└── README.md
```

## 注意事项

- Telegram Bot 无法拉取历史消息，缓冲区在 MCP Server 启动后开始积累
- Bot 必须被添加到群组中才能接收群消息（且需关闭 Privacy Mode 或使用 /command）
- Telegram 群消息限制约每分钟 20 条，私聊约每秒 1 条

## License

MIT
