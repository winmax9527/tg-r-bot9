from fastapi import APIRouter, HTTPException
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler
import logging
import os

# 导入核心逻辑文件中的函数
from link_generation_logic import generate_short_link

# 配置日志，用于区分是哪个 Bot 实例在运行
logger = logging.getLogger(__name__)

# --- 1. 创建 APIRouter 实例并定义 Bot ---
router = APIRouter()
# Bot 9 的令牌环境变量
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN_9")

# 初始化 Bot 和 Application
if BOT_TOKEN:
    bot = Bot(token=BOT_TOKEN)
    # 使用并发更新来处理多个 Bot 的请求
    application = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
else:
    logger.warning("Bot 9 (短链接生成) Token 未配置 (TELEGRAM_BOT_TOKEN_9)。请设置环境变量。")


# --- Bot Command Handlers ---

async def start_handler(update: Update, context):
    """处理 /start 命令，提供帮助信息。"""
    help_text = "欢迎使用 9 号短链接生成 Bot！\n"
    help_text += "功能：从 API 获取域名 A，并在域名 B 上生成一个短链接。\n"
    help_text += "使用方法：\n"
    help_text += "1. 自动生成代码：`/link`\n"
    help_text += "2. 自定义代码 (3-8 位字母数字)：`/link <自定义代码>`\n"
    help_text += "示例：`/link MyCode123`"
    await update.message.reply_text(help_text)

async def link_command_handler(update: Update, context):
    """处理 /link 命令，调用核心短链接生成逻辑。"""
    
    # 提取自定义代码，如果没有则为 None
    custom_code = context.args[0] if context.args else None
    
    await update.message.reply_text("Bot 9 正在为您生成短链接，请稍候...")
    
    # 调用核心逻辑 (来自 link_generation_logic.py)
    result = await generate_short_link(custom_code)
    
    await update.message.reply_text(result)


# --- 添加 Handler 到 Application ---
if BOT_TOKEN:
    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("link", link_command_handler))


# --- 2. Webhook 路由函数 ---

async def handle_webhook(update_data: dict):
    """处理传入的 Telegram Webhook JSON 数据。"""
    if not BOT_TOKEN:
        return {"status": "error", "message": "Bot token not configured."}
        
    try:
        update = Update.de_json(update_data, bot)
        await application.process_update(update)
        return {"status": "ok"}
        
    except Exception as e:
        logger.error(f"Bot 9 处理 Webhook 错误: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


# --- 3. 挂载到 APIRouter ---
@router.post("/webhook")
async def webhook_handler(update_data: dict):
    """接收 Telegram Webhook POST 请求。"""
    if not BOT_TOKEN:
        raise HTTPException(status_code=500, detail="Bot 9 Token 未配置。")
        
    response = await handle_webhook(update_data)
    
    # 始终返回 200 OK，通知 Telegram 接收成功
    return response