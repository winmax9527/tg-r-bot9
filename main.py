import os
import asyncio
from typing import Dict, List, Any

from telegram import Update
from telegram.ext import Application, MessageHandler, filters, CommandHandler, ApplicationBuilder
from fastapi import FastAPI, Request
import uvicorn
import logging

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- FastAPI 和 Telegram 应用初始化 ---
# FastAPI 主应用，用于处理 Webhook 请求
app = FastAPI()

# 存储所有 Telegram Application 实例
applications: Dict[str, Application] = {}
# 存储所有 Bot 的 URL 路径映射 (例如: "1" -> "/webhook/bot1")
bot_url_paths: Dict[str, str] = {}
# 存储所有 Bot 的启动后台任务
bot_tasks: List[asyncio.Task] = []


# --- 配置文件路径（请确保这些文件存在于您的项目根目录）---
# 导入各个 Bot 的逻辑函数 (假设它们都在各自的文件中)
# 
# 确保您的项目根目录存在以下文件:
# bot1_app.py, bot4_app.py, bot6_app.py, bot9_app.py
from bot1_app import setup_bot_1
from bot4_app import setup_bot_4
from bot6_app import setup_bot_6
from bot9_app import setup_bot_9

# 将所有 Bot 的设置函数集中到一个字典中
BOT_SETUPS = {
    "1": setup_bot_1,
    "4": setup_bot_4,
    "6": setup_bot_6,
    "9": setup_bot_9,
}

# --- 核心逻辑：加载配置并初始化 Bots ---

def load_config():
    """从环境变量中加载 Bot Token 并构建配置。"""
    logger.info("应用启动中... 正在启动 Bot Applications 的后台任务。")
    config = {}
    tokens_found = 0
    
    # --------------------------------------------------------------------
    # 核心：寻找 BOT_TOKEN_N 变量，与 Render 仪表板配置匹配
    # --------------------------------------------------------------------
    for bot_id in BOT_SETUPS.keys():
        token_key = f"BOT_TOKEN_{bot_id}" # 查找 BOT_TOKEN_1, BOT_TOKEN_4, etc.
        token = os.environ.get(token_key)
        
        if token:
            config[bot_id] = {
                "token": token,
                "url_path": f"/webhook/bot{bot_id}",
                # 从环境变量加载 API URL
                "api_url": os.environ.get(f"BOT_{bot_id}_API_URL")
            }
            # 诊断信息显示成功找到 Token
            logger.info(f"DIAGNOSTIC: 环境变量 {token_key} 已设置。")
            tokens_found += 1
        else:
            # 诊断信息显示未找到 Token (这应该只发生在未设置的 BOT_TOKEN_2, BOT_TOKEN_3, BOT_TOKEN_5, etc.)
            logger.info(f"DIAGNOSTIC: 环境变量 {token_key} 未设置。")

    if tokens_found == 0:
        logger.error("❌ 未找到任何有效的 Bot Token。")
    else:
        logger.info(f"✅ 成功加载 {tokens_found} 个 Bot Token。")
        
    return config

async def init_telegram_applications(bot_configs: Dict[str, Any]):
    """初始化并启动所有 Telegram 应用。"""
    if not bot_configs:
        return

    # 从 Render 环境变量获取服务的外部 URL
    external_url = os.environ.get("EXTERNAL_URL") 
    
    # 如果 Render 没有自动设置 EXTERNAL_URL，则假定它在运行时提供
    if not external_url:
        logger.warning("EXTERNAL_URL 环境变量未设置，可能无法正确设置 Webhook。")
        # 尝试使用 Render 的 SERVICE_URL 变量 (如果存在)
        external_url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("RENDER_SERVICE_URL")

    for bot_id, cfg in bot_configs.items():
        token = cfg["token"]
        url_path = cfg["url_path"]
        api_url = cfg.get("api_url")
        
        # 1. 创建 Application
        application = ApplicationBuilder().token(token).build()
        applications[bot_id] = application
        bot_url_paths[bot_id] = url_path

        # 2. 设置 Bot 的逻辑 (Handlers)
        setup_function = BOT_SETUPS.get(bot_id)
        if setup_function:
            setup_function(application)
        
        # 3. 配置 Webhook
        if external_url:
            full_webhook_url = f"{external_url.rstrip('/')}{url_path}"
            logger.info(f"Bot {bot_id} (Token {token[:5]}...): 正在设置 Webhook 到 {full_webhook_url}")
            
            # 使用 set_webhook 设置 Webhook URL
            try:
                await application.bot.set_webhook(url=full_webhook_url)
                logger.info(f"Bot {bot_id}: Webhook 设置成功。")
            except Exception as e:
                logger.error(f"Bot {bot_id}: 设置 Webhook 失败: {e}")

            if api_url:
                 # 这是一个可选步骤，用于设置自定义 API URL，以防万一
                 await application.bot.set_api_url(api_url)


# --- 生命周期事件处理 (FastAPI) ---

@app.on_event("startup")
async def startup_event():
    """应用启动时，初始化所有 Bot 并启动后台轮询。"""
    
    # 1. 加载配置
    bot_configs = load_config()
    
    # 2. 初始化 Telegram 应用 (设置 Webhook URL)
    await init_telegram_applications(bot_configs)
    
    logger.info("🎉 核心服务启动完成。")

@app.on_event("shutdown")
async def shutdown_event():
    """应用关闭时，停止所有后台任务。"""
    logger.info("应用关闭中... 正在停止 Bot Applications 的后台任务。")
    # 清理所有 Applications 的 Webhook
    for bot_id, application in applications.items():
        try:
            await application.bot.delete_webhook()
            logger.info(f"Bot {bot_id}: Webhook 已删除。")
        except Exception as e:
            logger.error(f"Bot {bot_id}: 删除 Webhook 失败: {e}")

    logger.info("应用关闭完成。")


# --- FastAPI Webhook 路由 ---

@app.get("/")
def home():
    """根路径，用于健康检查和显示服务信息。"""
    return {"status": "ok", "message": f"Telegram Bot Webhook Service Running with {len(applications)} Bots."}

# 动态创建 Webhook 路由
for bot_id in BOT_SETUPS.keys():
    path = f"/webhook/bot{bot_id}"
    
    # 使用函数工厂模式来捕获 bot_id
    def create_webhook_handler(current_bot_id):
        async def webhook_handler(request: Request):
            try:
                # 获取对应的 Application 实例
                application = applications.get(current_bot_id)
                if not application:
                    logger.warning(f"Webhook received for unknown bot ID: {current_bot_id}")
                    return {"status": "error", "message": "Unknown bot ID"}

                # 从请求中解析 JSON 数据
                update_data = await request.json()
                update = Update.de_json(update_data, application.bot)

                # 将 Update 放入处理队列并异步处理
                await application.process_update(update)

                return {"status": "ok"}
            except Exception as e:
                logger.error(f"Error handling webhook for bot {current_bot_id}: {e}")
                return {"status": "error", "message": str(e)}
        
        # 给函数指定一个唯一的名称，避免 FastAPI 路由冲突
        webhook_handler.__name__ = f"webhook_handler_bot{current_bot_id}"
        return webhook_handler

    # 将动态生成的处理器添加到 FastAPI 路由
    app.post(path)(create_webhook_handler(bot_id))
    logger.info(f"Registered FastAPI route: POST {path}")


if __name__ == "__main__":
    # 仅用于本地测试
    try:
        uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
    except KeyboardInterrupt:
        pass
