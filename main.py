import uvicorn
from fastapi import FastAPI
import logging
import os

# 配置日志系统，确保可以看到哪个 Bot 正在工作
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- 1. 导入四个 Bot 路由文件 ---
# 假设这四个文件在当前目录下
try:
    from bot1_app import router as bot1_router
    from bot4_app import router as bot4_router
    from bot6_app import router as bot6_router
    from bot9_app import router as bot9_router
except ImportError as e:
    logger.error(f"无法导入 Bot 路由文件: {e}")
    logger.error("请确认 bot1_app.py, bot4_app.py, bot6_app.py, bot9_app.py 文件存在且没有语法错误。")
    # 如果导入失败，我们可以选择退出或继续（这里选择继续，但会在运行时失败）
    pass 


# --- 2. 创建 FastAPI 应用程序实例 ---
app = FastAPI(title="多机器人短链接服务", version="1.0.0")

# --- 3. 挂载 Bot 路由 ---
# 每个 Bot 挂载到不同的基础路径，例如：
# Bot 1 接收 /bot1/webhook 的请求
# Bot 4 接收 /bot4/webhook 的请求
# ...等等

# 确保 Bot 令牌已配置后再挂载路由
if os.getenv("TELEGRAM_BOT_TOKEN_1"):
    app.include_router(bot1_router, prefix="/bot1", tags=["Bot 1"])
    logger.info("Bot 1 路由已挂载到 /bot1")
else:
    logger.warning("Bot 1 令牌未设置，路由 /bot1 未激活。")

if os.getenv("TELEGRAM_BOT_TOKEN_4"):
    app.include_router(bot4_router, prefix="/bot4", tags=["Bot 4"])
    logger.info("Bot 4 路由已挂载到 /bot4")
else:
    logger.warning("Bot 4 令牌未设置，路由 /bot4 未激活。")

if os.getenv("TELEGRAM_BOT_TOKEN_6"):
    app.include_router(bot6_router, prefix="/bot6", tags=["Bot 6"])
    logger.info("Bot 6 路由已挂载到 /bot6")
else:
    logger.warning("Bot 6 令牌未设置，路由 /bot6 未激活。")

if os.getenv("TELEGRAM_BOT_TOKEN_9"):
    app.include_router(bot9_router, prefix="/bot9", tags=["Bot 9"])
    logger.info("Bot 9 路由已挂载到 /bot9")
else:
    logger.warning("Bot 9 令牌未设置，路由 /bot9 未激活。")


# --- 4. 根路径健康检查 ---
@app.get("/")
async def root():
    """根路径，用于健康检查"""
    # 检查哪些 Bot 令牌已配置
    status = {
        "status": "online",
        "message": "FastAPI Service is running.",
        "bots_status": {
            "Bot 1": "Active" if os.getenv("TELEGRAM_BOT_TOKEN_1") else "Inactive (Missing Token)",
            "Bot 4": "Active" if os.getenv("TELEGRAM_BOT_TOKEN_4") else "Inactive (Missing Token)",
            "Bot 6": "Active" if os.getenv("TELEGRAM_BOT_TOKEN_6") else "Inactive (Missing Token)",
            "Bot 9": "Active" if os.getenv("TELEGRAM_BOT_TOKEN_9") else "Inactive (Missing Token)",
        }
    }
    return status

# --- 5. 启动服务器的逻辑（通常用于本地运行，在部署时由 gunicorn 或 uvicorn 自身处理）---
if __name__ == "__main__":
    # 从环境变量获取端口，默认使用 8080
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"应用将在端口 {port} 上启动")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)