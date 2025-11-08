import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from telegram.ext import Application
import asyncio

# --- 1. 日志配置 ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

# --- 2. 路由导入 ---
# 使用 try-except 导入所有 Bot 路由，以防止单个 Bot 文件的错误影响整个应用启动
try:
    from bot1_app import router as bot1_router, initialize_bot as initialize_bot1
    from bot4_app import router as bot4_router, initialize_bot as initialize_bot4
    from bot6_app import router as bot6_router, initialize_bot as initialize_bot6
    from bot9_app import router as bot9_router, initialize_bot as initialize_bot9
    
    BOT_ROUTERS = [
        (bot1_router, initialize_bot1, "/bot1", "Bot 1"),
        (bot4_router, initialize_bot4, "/bot4", "Bot 4"),
        (bot6_router, initialize_bot6, "/bot6", "Bot 6"),
        (bot9_router, initialize_bot9, "/bot9", "Bot 9"),
    ]
    logger.info("所有 Bot 路由文件导入成功。")

except ImportError as e:
    logger.error(f"无法导入 Bot 路由文件: {e}")
    logger.error("请确认 bot1_app.py, bot4_app.py, bot6_app.py, bot9_app.py 文件存在且没有语法错误。")
    BOT_ROUTERS = []
except Exception as e:
    logger.error(f"导入 Bot 路由时发生未知错误: {e}")
    BOT_ROUTERS = []

# --- 3. 生命周期管理 (Lifespan Context Manager) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI 生命周期管理：启动时初始化 Bot，关闭时停止 Bot。
    """
    logger.info("应用启动中...")
    
    initialization_tasks = []
    
    # 启动所有 Bot 的初始化任务
    for _, init_func, _, _ in BOT_ROUTERS:
        # initialize_bot() 函数内部现在处理 Application.initialize() 和 Application.start()
        # 我们只需要调用它
        task = asyncio.create_task(init_func())
        initialization_tasks.append(task)
    
    # 等待所有初始化任务启动（无需等待它们内部的 application.start() 结束）
    await asyncio.sleep(1) 
    
    logger.info("所有 Bot 初始化任务已启动。")
    
    yield # 应用开始接受请求
    
    # --- 关闭阶段 ---
    logger.info("应用关闭中...")
    
    for _, _, _, _ in BOT_ROUTERS:
        # 由于 application.start() 是在 botX_app.py 中作为后台任务启动的，
        # 停止逻辑应在 botX_app.py 中自行管理，或者在 application 实例上调用 stop()
        pass # 暂时省略显式停止逻辑，依赖进程关闭

# --- 4. FastAPI 应用创建 ---
app = FastAPI(lifespan=lifespan)

# --- 5. 路由注册 ---
for router, _, prefix, tag in BOT_ROUTERS:
    # 只有当路由对象存在时才注册
    if router:
        app.include_router(router, prefix=prefix, tags=[tag])
        logger.info(f"成功注册路由: {prefix}")
    else:
        logger.warning(f"跳过注册 {tag} 路由，因为它未成功导入。")

# --- 6. 根路由 (健康检查) ---
@app.get("/")
def read_root():
    return {"status": "ok", "message": "FastAPI Webhook Server is running"}
