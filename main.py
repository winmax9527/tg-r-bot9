import os
import logging
import asyncio
from typing import Dict, Optional

# 使用 telegram.ext 而不是 python-telegram-bot
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

# FastAPI 框架
from fastapi import FastAPI, Request, HTTPException

# 配置日志
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# 全局字典用于存储所有 Bot Application 实例
bot_applications: Dict[str, Application] = {}

# --- Bot 逻辑 ---

async def start_command(update: Update, context):
    """处理 /start 命令，回复欢迎消息。"""
    user = update.effective_user
    await update.message.reply_html(
        f"你好 {user.mention_html()}! 我是您的 Telegram Bot。",
        # reply_markup=ForceReply(selective=True),
    )

async def echo(update: Update, context):
    """回应用户的文本消息。"""
    await update.message.reply_text(f"我收到了您的消息: {update.message.text}")

async def post_init(application: Application):
    """Bot 初始化后的回调函数，用于记录启动成功。"""
    bot_info = await application.bot.get_me()
    logger.info(f"✅ Bot '{bot_info.username}' Application 初始化完成。")

def initialize_bots_and_applications():
    """从环境变量加载所有 Bot Token 并初始化 Application 实例。"""
    
    found_tokens = {}
    for i in range(1, 10):  # 检查 BOT_TOKEN_1 到 BOT_TOKEN_9
        token_key = f"BOT_TOKEN_{i}"
        token = os.environ.get(token_key)
        
        # --- 诊断性日志：检查环境变量是否被正确加载 ---
        if token:
            # 打印部分 token 以确认存在，但隐藏完整 token
            logger.info(f"DIAGNOSTIC: 发现环境变量 {token_key}。Token 尾号: {token[-4:]}")
            found_tokens[token_key] = token
        else:
            logger.info(f"DIAGNOSTIC: 环境变量 {token_key} 未设置。")
        # -------------------------------------------------

    if not found_tokens:
        logger.error("❌ 未找到任何有效的 Bot Token。")
        return

    for token_key, token in found_tokens.items():
        try:
            # 1. 创建 Application
            application = (
                Application.builder()
                .token(token)
                .post_init(post_init) # 启动后执行 post_init
                .build()
            )
            
            # 2. 注册处理程序
            application.add_handler(CommandHandler("start", start_command))
            application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
            
            # 3. 存储实例，使用完整的 token 作为键，用于 webhook 查找
            bot_applications[token] = application
            logger.info(f"Bot Application 实例已为 Token (尾号: {token[-4:]}) 创建。")
            
        except Exception as e:
            logger.error(f"初始化 Bot (Token 尾号: {token[-4:]}) 失败: {e}")


# --- FastAPI 主应用 ---

app = FastAPI(title="Telegram Multi-Bot Webhook Server")

@app.on_event("startup")
async def startup_event():
    """应用启动时调用，初始化所有 Bot Application。"""
    logger.info("应用启动中... 正在启动 Bot Applications 的后台任务。")
    initialize_bots_and_applications()
    
    # 启动所有 Bot 的后台任务
    # 注意：我们使用 http_version="1.1" 的轮询方式（webhook）
    # 这里不需要 run_polling() 或 run_webhook()，因为我们将使用手动处理 update
    
    logger.info("🎉 核心服务启动完成。")


@app.on_event("shutdown")
async def shutdown_event():
    """应用关闭时调用。"""
    logger.info("应用关闭中... 正在停止 Bot Applications 的后台任务。")
    
    # 清理 any long-running tasks if necessary (currently none defined)
    
    logger.info("应用关闭完成。")


# Health Check 路由
@app.get("/")
async def root():
    """用于 Render 健康检查的根路由。"""
    return {"status": "ok", "message": f"Server running with {len(bot_applications)} active bot(s)."}


@app.post("/bot/{token}/webhook")
async def telegram_webhook(token: str, request: Request):
    """主 Webhook 路由，处理来自 Telegram 的所有更新。"""
    
    application = bot_applications.get(token)
    
    if not application:
        # 如果 token 在我们初始化的 Bot 字典中不存在
        logger.warning(f"❌ 收到未知 Token 的请求: {token[:4]}...{token[-4:]}")
        # 返回 200 以避免 Telegram 反复重试
        return {"status": "error", "message": "Unknown bot token."}

    # 1. 读取 Telegram 发送的 JSON 数据
    try:
        update_json = await request.json()
    except Exception as e:
        logger.error(f"无法解析 JSON 更新: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON format.")

    # 2. 将 JSON 数据转换为 Telegram Update 对象
    update = Update.de_json(update_json, application.bot)

    # 3. 处理更新
    # 使用 application.process_update 在后台处理更新
    await application.process_update(update)

    # 4. 立即返回 200 OK，表示接收成功
    return {"status": "ok", "message": "Update processed."}
