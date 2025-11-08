import os
import logging
from fastapi import FastAPI, Request
from telegram import Update
# 修正：所有组件都应该从 telegram.ext 导入，以确保兼容性
from telegram.ext import (
    Application, 
    ApplicationBuilder, 
    CommandHandler, 
    MessageHandler, 
    filters
)
from typing import Dict, Optional

# --- 配置 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 存储所有 Bot 的 token 和对应的 Application 实例
BOT_TOKENS: Dict[str, Optional[str]] = {
    "1": os.environ.get("BOT_TOKEN_1"),
    "4": os.environ.get("BOT_TOKEN_4"),
    "6": os.environ.get("BOT_TOKEN_6"),
    "9": os.environ.get("BOT_TOKEN_9"),
}

# 过滤掉未设置 token 的 Bot
ACTIVE_BOTS: Dict[str, str] = {bot_id: token for bot_id, token in BOT_TOKENS.items() if token}

# 存储 Bot Token 到 Application 实例的映射
bot_applications: Dict[str, Application] = {}

# --- 处理器函数 ---
async def start(update: Update, context):
    """处理 /start 命令"""
    chat_id = update.effective_chat.id
    bot_token = context.bot.token
    
    # 根据 token 查找 Bot ID，用于日志和回复
    current_bot_id = next((bot_id for bot_id, token in ACTIVE_BOTS.items() if token == bot_token), "未知")
    
    await context.bot.send_message(
        chat_id=chat_id, 
        text=f"你好！我是 Bot {current_bot_id} (Token 尾号: {bot_token[-4:]})。\n"
             f"我的 Webhook 正在运行中！请给我发送一条消息。"
    )
    logger.info(f"Bot {current_bot_id} 收到 /start 命令 from {chat_id}")

async def echo(update: Update, context):
    """回显用户发送的文本消息"""
    chat_id = update.effective_chat.id
    text = update.message.text
    # 查找 Bot ID
    bot_token = context.bot.token
    current_bot_id = next((bot_id for bot_id, token in ACTIVE_BOTS.items() if token == bot_token), "未知")
    
    await context.bot.send_message(chat_id=chat_id, text=f"我是 Bot {current_bot_id}，你说了: {text}")
    logger.info(f"Bot {current_bot_id} 收到消息: {text} from {chat_id}")

# --- 初始化 Bots 和 Applications ---
def initialize_bots_and_applications():
    """初始化所有活跃的 Application 实例"""
    global bot_applications
    
    if not ACTIVE_BOTS:
        logger.error("❌ 未找到任何有效的 Bot Token。")
        return

    for bot_id, token in ACTIVE_BOTS.items():
        try:
            # 使用 ApplicationBuilder 构建 Application 实例
            application = (
                ApplicationBuilder()
                .token(token)
                .updater(None) # Webhook 模式不需要内置 Updater
                .arbitrary_callback_data(True)
                .build()
            )

            # 注册处理器
            application.add_handler(CommandHandler("start", start))
            application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), echo))
            
            bot_applications[token] = application
            logger.info(f"✅ Bot {bot_id} (Token 尾号: {token[-4:]}) Application 初始化完成。")

        except Exception as e:
            logger.error(f"❌ 初始化 Bot {bot_id} 失败: {e}")

# 在应用启动前初始化
initialize_bots_and_applications()

# --- FastAPI 应用实例 ---
app = FastAPI(title="Multi-Bot Telegram Webhook Handler")

@app.on_event("startup")
async def startup_event():
    """应用启动时启动 Bot Application 的后台任务"""
    logger.info("应用启动中... 正在启动 Bot Applications 的后台任务。")
    for token, app_instance in bot_applications.items():
        # 必须先 initialize 再 start
        await app_instance.initialize()
        # 启动 Application 的后台任务（如处理器和队列）
        await app_instance.start()
        logger.info(f"✅ Bot {token[-4:]} Application 后台任务启动。")
    logger.info("🎉 核心服务启动完成。")

@app.on_event("shutdown")
async def shutdown_event():
    """应用关闭时停止 Bot Application 的后台任务"""
    logger.info("应用关闭中... 正在停止 Bot Applications 的后台任务。")
    for app_instance in bot_applications.values():
        await app_instance.stop()
    logger.info("应用关闭完成。")

@app.get("/")
async def home():
    """根路径健康检查"""
    return {"status": "ok", "message": "Multi-Bot Handler is running."}


# 动态创建和处理 Webhook 路由
@app.post("/bot/{token}/webhook")
async def process_webhook(token: str, request: Request):
    """处理来自 Telegram 的 Webhook 更新"""
    if token not in bot_applications:
        logger.warning(f"❌ 收到未知 Token 的请求: {token[:4]}...{token[-4:]}")
        return {"status": "error", "message": "Unknown bot token"}

    application = bot_applications[token]
    
    try:
        # 获取请求体
        body = await request.json()
        
        # 将 JSON 转换为 Telegram Update 对象
        update = Update.de_json(body, application.bot)
        
        # 将更新放入 Application 队列，让后台任务处理
        await application.update_queue.put(update)
        
        logger.info(f"✅ Bot {token[-4:]} 成功接收更新并放入队列。")
        return {"status": "ok"}

    except Exception as e:
        logger.error(f"❌ Bot {token[-4:]} Webhook 处理失败: {e}")
        return {"status": "error", "message": f"Processing failed: {e}"}

# 兜底路由：捕获旧的或错误的 Webhook 路径
@app.post("/bot/{token}")
async def catch_old_webhook(token: str):
    """捕获旧的或错误的 Webhook 路径，并给出提示"""
    logger.warning(f"❌ Webhook 路径未找到 (404): POST /bot/{token} - (请检查 set_webhooks.py 中设置的路径是否包含 /webhook 后缀)")
    return {"status": "error", "message": "Webhook route not found. Did you forget /webhook suffix in the route definition?"}
