import os
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
# 从环境中获取 Bot 1 的 Token
BOT_TOKEN = os.environ.get("BOT_TOKEN_1")

# --- Application 实例创建 ---
# 仅创建实例，不调用 initialize()，initialize() 由 main.py 的 lifespan 负责调用
application = Application.builder().token(BOT_TOKEN).build()

# --- 路由 ---
router = APIRouter()

# --- Handlers ---
async def start(update: Update, context):
    """处理 /start 命令"""
    await update.message.reply_text('Hello! I am Bot 1. I am running successfully!')

async def echo(update: Update, context):
    """回应用户发送的消息"""
    if update.message:
        await update.message.reply_text(f"Bot 1 收到消息: {update.message.text}")

# 注册 Handlers
application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))


# --- Webhook 路由定义 ---
@router.post("/webhook")
async def telegram_webhook(request: Request):
    """
    处理传入的 Telegram Webhook 更新。
    路径 '/webhook' 会被 main.py 挂载在 /bot/{TOKEN}/ 之下。
    """
    try:
        # 获取 JSON 数据
        data = await request.json()
        
        # 将 JSON 数据转换为 Telegram Update 对象
        update = Update.de_json(data, application.bot)
        
        # 将 Update 放入 Application 的更新队列中
        await application.update_queue.put(update)
        
        # 立即返回 200 OK，表示接收成功
        return JSONResponse(content={"status": "ok"}, status_code=status.HTTP_200_OK)

    except Exception as e:
        logger.error(f"Bot 1 处理 Webhook 更新时发生错误: {e}")
        # 即使出错也返回 200，防止 Telegram 频繁重试
        return JSONResponse(content={"status": "error", "message": str(e)}, status_code=status.HTTP_200_OK)
