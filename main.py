import os
import uvicorn
import asyncio
import logging
from fastapi import FastAPI, Request
from contextlib import asynccontextmanager
from bot1_app import bot1_app
from bot4_app import bot4_app
from bot6_app import bot6_app
from bot9_app import bot9_app

# --- 基础配置 ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

# --- 应用生命周期管理 ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 应用启动时执行
    logger.info("应用启动中...")
    # 这里可以添加一些初始化任务，如检查 Webhook 状态等
    yield
    # 应用关闭时执行
    logger.info("应用关闭中...")

app = FastAPI(lifespan=lifespan)

# --- 根路径测试路由 ---
# 这解释了为什么 35.197.118.178:0 - "GET / HTTP/1.1" 200 是成功的
@app.get("/")
def read_root():
    return {"message": "Telegram Bot Service is Running"}

# --- 核心：挂载 Bot 路由 (确保路径正确) ---
# 必须使用 prefix=/botX 来匹配 Telegram Webhook URL
# Webhook URL 示例: https://tg-r-bot9.onrender.com/bot1/webhook
app.include_router(bot1_app, prefix="/bot1")
app.include_router(bot4_app, prefix="/bot4")
app.include_router(bot6_app, prefix="/bot6")
app.include_router(bot9_app, prefix="/bot9")

# --- 启动配置 (Render 部署标准) ---
if __name__ == "__main__":
    # 使用 Render 提供的 PORT 环境变量
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
