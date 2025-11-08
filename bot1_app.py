import os
import logging
from telegram import Update
from telegram.ext import Application, MessageHandler, filters
from fastapi import APIRouter, Request
import asyncio
from link_generation_logic import resolve_url_logic # 导入核心逻辑

# --- 1. Bot 配置 ---
BOT_ID = "1"
BOT_TOKEN = os.environ.get(f"TELEGRAM_BOT_TOKEN_{BOT_ID}") 
API_URL = os.environ.get(f"BOT_{BOT_ID}_API_URL") # 使用 BOT_1_API_URL 环境变量
WEBHOOK_PATH = f"/bot{BOT_ID}/webhook"

# --- 2. 日志配置 ---
logger = logging.getLogger(__name__)

# --- 3. Bot Core Logic ---
async def get_final_url(update: Update, context) -> None:
    """处理用户消息，调用核心逻辑并返回结果。"""
    
    # 立即发送回复，这是防止 Telegram 超时的关键
    await update.message.reply_text("正在为您获取最新下载链接，请稍候...")
    
    if not API_URL:
        await update.message.reply_text("❌ 机器人配置错误，未找到 API URL。")
        logger.error(f"Bot {BOT_ID}: API_URL not found.")
        return

    # 调用核心逻辑
    final_url, reply_message = await resolve_url_logic(API_URL, BOT_ID)
    
    # 发送最终结果
    await update.message.reply_text(reply_message)


# --- 4. 初始化 Telegram Application 实例 ---
application = None
# ⭐️ 新增：状态标志，用于确保 Bot 完全初始化
bot_ready = asyncio.Event()

async def initialize_bot(): 
    """初始化 Bot 实例"""
    global application
    
    if BOT_TOKEN and API_URL:
        # 必须使用 Application.builder().token().build() 来创建实例
        application = Application.builder().token(BOT_TOKEN).build()
        
        # 定义需要响应的命令/关键词
        COMMAND_PATTERN = r"^(地址|最新地址|安卓地址|苹果地址|安卓下载地址|苹果下载地址|链接|最新链接|安卓链接|安卓下载链接|最新安卓链接|苹果链接|苹果下载链接|ios链接|最新苹果链接|/start_check|/start)$"
        application.add_handler(
            MessageHandler(
                filters.TEXT & filters.Regex(COMMAND_PATTERN), 
                get_final_url
            )
        )
        
        # 1. 启动 Bot 内部的异步任务，包括 initialize() 和 start()
        await application.initialize()
        asyncio.create_task(application.start()) 
        
        # 2. 标记 Bot 已准备就绪
        bot_ready.set()
        
        logger.info(f"Initialized Bot {BOT_ID} on path {WEBHOOK_PATH}")
    else:
        logger.error(f"Bot {BOT_ID}: TOKEN or API_URL not set.")

# --- 5. FastAPI 路由设置 ---
# 必须使用 APIRouter
router = APIRouter()

@router.on_event("startup")
async def startup_event():
    """Bot 1 专属启动事件"""
    await initialize_bot()

@router.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    """处理来自 Telegram 的 Webhook 请求"""
    # ⭐️ 关键修复：等待 Bot 准备就绪
    await asyncio.wait_for(bot_ready.wait(), timeout=5) # 最多等待 5 秒
    
    if not application:
        logger.error(f"Bot {BOT_ID}: Application not initialized.")
        return {"status": "error", "message": "Application not initialized"}

    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        # 将 Update 对象放入 Bot 内部的异步队列中等待处理
        await application.update_queue.put(update)
        return {"status": "ok"}
    except asyncio.TimeoutError:
        # 如果等待超时，返回一个失败状态
        logger.error(f"Bot {BOT_ID}: Initialization timeout. Cannot process webhook.")
        return {"status": "error", "message": "Bot initialization timeout"}
    except Exception as e:
        logger.error(f"Bot {BOT_ID}: Error processing update: {e}")
        return {"status": "error", "message": str(e)}

# --- 6. 额外：健康检查 (可选) ---
@router.get("/")
def health_check():
    # 也可以检查 bot_ready.is_set() 来提供更详细的状态
    status = "ready" if bot_ready.is_set() else "initializing"
    return {"status": status, "message": f"Bot {BOT_ID} Router is {status}."}
