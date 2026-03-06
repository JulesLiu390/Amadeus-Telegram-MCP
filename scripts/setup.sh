#!/bin/sh
# ============================================================
# Telegram MCP Server 配置脚本
# 交互式设置 Bot Token，自动生成 mcp.json 配置
# ============================================================
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

# 小写转换 (POSIX 兼容)
to_lower() {
    echo "$1" | tr '[:upper:]' '[:lower:]'
}

echo "=== Telegram MCP Server 配置 ==="
echo ""

# ── 1. 检查 / 安装 uv ────────────────────────────────────
if command -v uv >/dev/null 2>&1; then
    echo "✅ uv 已安装: $(uv --version)"
else
    echo "📦 正在安装 uv ..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
    if command -v uv >/dev/null 2>&1; then
        echo "✅ uv 安装完成: $(uv --version)"
    else
        echo "❌ uv 安装失败，请手动安装: https://docs.astral.sh/uv/"
        exit 1
    fi
fi

# ── 2. 安装 Python 依赖 ──────────────────────────────────
echo ""
echo "📦 安装 Python 依赖 ..."
uv sync
echo "✅ Python 依赖安装完成"

# ── 3. 交互式输入 Bot Token ──────────────────────────────
echo ""
echo "📝 配置 Telegram Bot"
echo "   在 Telegram 中找 @BotFather，发送 /newbot 创建机器人，获取 token"
echo ""

# 尝试从已有 .env 读取
EXISTING_TOKEN=""
if [ -f .env ]; then
    EXISTING_TOKEN=$(sed -n 's/^TELEGRAM_BOT_TOKEN=//p' .env 2>/dev/null || true)
fi

if [ -n "$EXISTING_TOKEN" ]; then
    DISPLAY_TOKEN=$(echo "$EXISTING_TOKEN" | cut -c1-10)
    echo "   已检测到 .env 中的 token: ${DISPLAY_TOKEN}..."
    printf "使用已有 token？(Y/n): "
    read USE_EXISTING
    USE_EXISTING=$(to_lower "${USE_EXISTING:-y}")
    if [ "$USE_EXISTING" != "n" ]; then
        BOT_TOKEN="$EXISTING_TOKEN"
    else
        printf "请输入 Bot Token: "
        read BOT_TOKEN
    fi
else
    printf "请输入 Bot Token: "
    read BOT_TOKEN
fi

if [ -z "$BOT_TOKEN" ]; then
    echo "❌ Token 不能为空"
    exit 1
fi

# ── 4. 验证 Token 是否有效 ────────────────────────────────
echo ""
echo "🔍 验证 Bot Token ..."
API_RESP=$(curl -s "https://api.telegram.org/bot${BOT_TOKEN}/getMe" 2>/dev/null || echo '{"ok":false}')

BOT_USERNAME=""
BOT_NAME=""
if echo "$API_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d['ok']" 2>/dev/null; then
    BOT_USERNAME=$(echo "$API_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['result'].get('username',''))")
    BOT_NAME=$(echo "$API_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['result'].get('first_name',''))")
    BOT_ID=$(echo "$API_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['result'].get('id',''))")
    echo "✅ Token 有效"
    echo "   Bot: ${BOT_NAME} (@${BOT_USERNAME})"
    echo "   ID:  ${BOT_ID}"
else
    echo "⚠️  Token 验证失败（可能是网络问题），将继续配置"
fi

# ── 5. 可选: 监控过滤 ────────────────────────────────────
echo ""
echo "📋 监控配置 (可选，直接回车表示监控全部)"

printf "监控的 Chat ID（逗号分隔，留空=全部）: "
read CHAT_IDS
printf "监控的 User ID（逗号分隔，留空=全部）: "
read USER_IDS

# ── 6. 保存 .env ─────────────────────────────────────────
cat > .env <<EOF
TELEGRAM_BOT_TOKEN=${BOT_TOKEN}
EOF
echo ""
echo "✅ Token 已保存到 .env"

# ── 7. 生成 MCP 客户端配置 ────────────────────────────────
UV_PATH="$(which uv)"
PROJECT_ABS="$(pwd)"

# 构建 args 字符串
ARGS_STR="run --directory ${PROJECT_ABS} telegram-agent-mcp --token ${BOT_TOKEN}"

if [ -n "$CHAT_IDS" ]; then
    ARGS_STR="${ARGS_STR} --chat-ids ${CHAT_IDS}"
fi
if [ -n "$USER_IDS" ]; then
    ARGS_STR="${ARGS_STR} --user-ids ${USER_IDS}"
fi

# 生成 mcp.json
MCP_CONF="mcp.json"
SKIP_MCP=0
if [ -f "$MCP_CONF" ]; then
    printf "⚠️  mcp.json 已存在，是否覆盖？(y/N): "
    read OVERWRITE
    OVERWRITE=$(to_lower "${OVERWRITE:-n}")
    if [ "$OVERWRITE" != "y" ]; then
        echo "   保留现有 mcp.json"
        SKIP_MCP=1
    fi
fi

if [ "$SKIP_MCP" != "1" ]; then
    python3 -c "
import json
config = {
    'mcpServers': {
        'telegram-agent': {
            'command': '${UV_PATH}',
            'args': '${ARGS_STR}'
        }
    }
}
print(json.dumps(config, indent=2, ensure_ascii=False))
" > "$MCP_CONF"
    echo "✅ MCP 配置已生成: $MCP_CONF"
fi

# ── 8. 汇总 ──────────────────────────────────────────────
echo ""
echo "=== 配置完成 ==="
echo ""
echo "当前配置："
if [ -n "$BOT_NAME" ]; then
    echo "  Bot:      ${BOT_NAME} (@${BOT_USERNAME})"
else
    echo "  Bot:      未验证"
fi
echo "  Chat IDs: ${CHAT_IDS:-全部}"
echo "  User IDs: ${USER_IDS:-全部}"
echo ""
echo "生成的文件："
echo "  .env      — Bot Token（请勿提交到 git）"
echo "  mcp.json  — MCP 客户端配置"
echo ""
echo "下一步："
echo "  1. 将 mcp.json 的内容复制到你的 MCP 客户端配置中"
echo "     - Claude Desktop: ~/Library/Application Support/Claude/claude_desktop_config.json"
echo "     - Cursor: .cursor/mcp.json"
echo "  2. 测试连接: python3 scripts/test-mcp.py"
echo ""

# 显示生成的 mcp.json 内容
if [ -f "$MCP_CONF" ]; then
    echo "mcp.json 内容："
    echo "─────────────────────────────────────"
    cat "$MCP_CONF"
    echo ""
    echo "─────────────────────────────────────"
fi
