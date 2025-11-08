import os
import logging
import asyncio
from typing import List, Dict
from fastapi import FastAPI, Request, Response
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# --- 1. 配置日志记录 (Logging Setup) ---
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- 2. 全局状态和数据结构 ---
BOT_APPLICATIONS: Dict[str, Application] = {}
BOT_WEBHOOK_PATHS: Dict[str, str] = {}

# --- 3. Bot 核心命令处理函数 (Handlers) ---

# /start 命令
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """回复 /start 命令，并显示当前 Bot ID。"""
    bot_token_end = context.application.bot.token[-4:]
    bot_index = "N/A"
    for path, app in BOT_APPLICATIONS.items():
        if app.bot.token == context.application.bot.token:
            bot_index = path.replace("bot", "").replace("_webhook", "") # e.g., "1", "4"
            break

    message = (
        f"🤖 你好！我是 Bot **#{bot_index}**。\n"
        f"(我的 Token 尾号是: `{bot_token_end}`)\n\n"
        "请发送消息给我，我会复读你的内容！\n"
        "你可以使用 /help 查看可用命令。"
    )
    await update.message.reply_html(message)

# /help 命令
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """回复 /help 命令。"""
    message = (
        "📚 **可用命令:**\n"
        "/start - 启动 Bot 并获取 Bot ID\n"
        "/help - 显示此帮助信息\n\n"
        "任何其他消息将作为文本复读。"
    )
    await update.message.reply_html(message)

# 消息处理函数（复读功能）
async def echo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """复读用户发送的文本消息。"""
    if update.message and update.message.text:
        text = update.message.text
        logger.info(f"Bot {context.application.bot.token[-4:]} 收到消息: {text[:50]}...")
        await update.message.reply_text(f"你说了: \n\n{text}")

def setup_bot(app_instance: Application, bot_index: int) -> None:
    """配置 Bot 的所有处理器 (Handlers)。"""
    token_end = app_instance.bot.token[-4:]
    logger.info(f"Bot Application 实例 (#{bot_index}, 尾号: {token_end}) 正在配置 Handlers。")

    app_instance.add_handler(CommandHandler("start", start_command))
    app_instance.add_handler(CommandHandler("help", help_command))
    app_instance.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo_message))

# --- 4. FastAPI 应用实例 ---
app = FastAPI(title="Multi-Bot Telegram Webhook Handler")

# --- 5. 应用启动时，初始化所有 Bot ---
@app.on_event("startup")
async def startup_event():
    """在 FastAPI 启动时初始化所有 Bot Application 实例。"""
    
    global BOT_APPLICATIONS, BOT_WEBHOOK_PATHS
    BOT_APPLICATIONS = {}
    BOT_WEBHOOK_PATHS = {}

    logger.info("应用启动中... 正在查找 Bot Token 并创建 Application 实例。")

    for i in range(1, 10): # 检查 BOT_TOKEN_1 到 BOT_TOKEN_9
        token_name = f"BOT_TOKEN_{i}"
        token_value = os.getenv(token_name)
        
        if token_value:
            logger.info(f"DIAGNOSTIC: 发现环境变量 {token_name}。Token 尾号: {token_value[-4:]}")
            
            # 创建 Application 实例
            application = Application.builder().token(token_value).build()
            
            # --- 
            # --- ⬇️ 关键修复：就是这一行！⬇️ ---
            #
            # 必须在添加 Handlers 之前，异步初始化 Application
            await application.initialize()
            #
            # --- ⬆️ 关键修复：就是这一行！⬆️ ---
            # --- 
            
            # 配置 Handlers (复读机功能)
            setup_bot(application, i)
            
            webhook_path = f"bot{i}_webhook"
            BOT_APPLICATIONS[webhook_path] = application
            BOT_WEBHOOK_PATHS[token_value] = webhook_path
            
            logger.info(f"Bot #{i} (尾号: {token_value[-4:]}) 已创建并初始化。监听路径: /{webhook_path}")
            
        else:
            logger.info(f"DIAGNOSTIC: 环境变量 {token_name} 未设置。")

    if not BOT_APPLICATIONS:
        logger.error("❌ 未找到任何有效的 Bot Token。请检查环境变量 BOT_TOKEN_N 的设置。")
    else:
        logger.info(f"✅ 成功初始化 {len(BOT_APPLICATIONS)} 个 Bot 实例。")
        logger.info("🎉 核心服务启动完成。等待 Telegram 的 Webhook 消息...")

# --- 6. 动态 Webhook 路由 ---
@app.post("/{webhook_path}")
async def handle_webhook(webhook_path: str, request: Request):
    """
    这是一个统一的入口点，用于处理所有 Bot 的 Webhook 消息。
    """
    
    if webhook_path not in BOT_APPLICATIONS:
        logger.warning(f"收到未知路径的请求: /{webhook_path}")
        return Response(status_code=404) # Not Found

    application = BOT_APPLICATIONS[webhook_path]
    token_end = application.bot.token[-4:]
    
    try:
        update_data = await request.json()
        update = Update.de_json(update_data, application.bot)
        
        logger.info(f"Bot (尾号: {token_end}) 正在处理 Webhook 请求 (路径: /{webhook_path})")
        
        await application.process_update(update)
        
        return Response(status_code=200) # OK
        
    except Exception as e:
        logger.error(f"处理 Webhook 请求失败 (路径: /{webhook_path})：{e}")
        return Response(status_code=500) # Internal Server Error

# --- 7. 健康检查路由 ---
@app.get("/")
async def root():
    """健康检查路由，返回 Bot 状态信息。"""
    active_bots_info = {}
    for path, app in BOT_APPLICATIONS.items():
        active_bots_info[path] = f"Token 尾号: {app.bot.token[-4:]}"
        
    status = {
        "status": "OK",
        "message": "Telegram Multi-Bot Webhook service is running.",
        "active_bots_count": len(BOT_APPLICATIONS),
        "active_bots_info": active_bots_info
    }
    return status
