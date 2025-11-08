import logging
import os
import asyncio
from fastapi import APIRouter, Request, HTTPException
from telegram import Update
from telegram.ext import Application, MessageHandler, filters
from link_generation_logic import generate_short_link

# --- 1. Bot 配置和初始化 ---
BOT_ID = 1
# 环境变量名称，例如 TELEGRAM_BOT_TOKEN_1, BOT_1_API_URL
BOT_TOKEN = os.getenv(f"TELEGRAM_BOT_TOKEN_{BOT_ID}") 
API_URL = os.getenv(f"BOT_{BOT_ID}_API_URL")

logger = logging.getLogger(__name__)

# 初始化 Bot 核心实例 (但不启动它，由 FastAPI 进程托管)
application = None
if BOT_TOKEN and API_URL:
    try:
        application = Application.builder().token(BOT_TOKEN).build()
        # 将 API URL 存储在 bot_data 中，供 handler 使用
        application.bot_data['API_URL'] = API_URL
        application.bot_data['BOT_ID'] = BOT_ID
        
        # 定义路由
        router = APIRouter()

        # --- 2. Handler 逻辑 ---
        COMMAND_PATTERN = r"^(地址|最新地址|安卓地址|苹果地址|安卓下载地址|苹果下载地址|链接|最新链接|安卓链接|安卓下载链接|最新安卓链接|苹果链接|苹果下载链接|ios链接|最新苹果链接|/start_check|/link)$"
        application.add_handler(
            MessageHandler(
                filters.TEXT & filters.Regex(COMMAND_PATTERN), 
                generate_short_link
            )
        )
        
        # 必须在主进程初始化前完成初始化工作
        # 注意: 这里只初始化 Application，不调用 application.run_polling() 或 application.start()
        # 异步初始化 Bot
        asyncio.create_task(application.initialize()) 
        logger.info(f"Bot {BOT_ID} 实例已初始化，但尚未挂载 Webhook 路由。")

    except Exception as e:
        logger.error(f"Bot {BOT_ID} 初始化失败: {e}")
        application = None # 确保失败时 application 为 None


# --- 3. Webhook 路由函数 ---
@router.post("/webhook")
async def telegram_webhook(request: Request):
    """处理 Telegram 发送到 /bot/<TOKEN>/webhook 的更新"""
    if not application:
        logger.error(f"Bot {BOT_ID} 实例未初始化或配置错误。")
        raise HTTPException(status_code=503, detail="Service Unavailable: Bot not initialized.")

    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        
        # 将更新放入 queue，由 telegram.ext 库在后台处理
        await application.process_update(update)
        
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Bot {BOT_ID} 处理更新时出错: {e}")
        # 返回 200 OK，防止 Telegram 重复发送更新
        return {"status": "error", "message": str(e)}

@router.get("/")
async def bot_root_check():
    """Bot 服务的健康检查，应被 main.py 挂载到 /bot/<TOKEN>/"""
    if application:
        return {"status": "ok", "message": f"Bot {BOT_ID} Router is active."}
    else:
        return {"status": "error", "message": f"Bot {BOT_ID} is configured incorrectly or token missing."}

# --- 4. 导出 APIRouter 实例 ---
# 必须命名为 'router'，供 main.py 动态导入
if 'router' not in locals():
    # 如果初始化失败，创建一个空的 APIRouter 确保 main.py 不会崩溃
    router = APIRouter()
    logger.warning(f"Bot {BOT_ID} 路由未成功创建，使用空路由。")

# 必须显式写出 'router'，防止 IDE 误判
router = router
