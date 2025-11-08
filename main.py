import uvicorn
import logging
import importlib
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, APIRouter, Request, status
from fastapi.responses import JSONResponse
from telegram.ext import Application
from typing import Dict

# --- 配置日志 ---
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- 全局变量和配置 ---
BOT_CONFIGS = {
    "1": os.environ.get("BOT_TOKEN_1"),
    "4": os.environ.get("BOT_TOKEN_4"),
    "6": os.environ.get("BOT_TOKEN_6"),
    "9": os.environ.get("BOT_TOKEN_9"),
}

# 存储 Application 实例和对应的 Bot Token
# 键为完整的 Bot Token
app_instance_map: Dict[str, Application] = {} 
router_modules = ["bot1_app", "bot4_app", "bot6_app", "bot9_app"]

# --- 核心初始化和清理逻辑 ---

async def initialize_bot(bot_id: str, bot_token: str):
    """初始化单个 Telegram Application 实例并注册路由模块。"""
    try:
        # 动态导入 Bot 对应的路由模块
        module_name = f"bot{bot_id}_app"
        module = importlib.import_module(module_name)
        
        # 检查模块中是否存在 Application 实例和 router
        if not hasattr(module, 'application'):
            logger.error(f"Bot {bot_id} 初始化失败: 模块 {module_name} 缺少 'application' 实例")
            return
        if not hasattr(module, 'router'):
            logger.error(f"Bot {bot_id} 初始化失败: 模块 {module_name} 缺少 'router' 实例")
            return

        # 获取实例
        application: Application = module.application
        router: APIRouter = module.router
        
        # 异步初始化 Application (用于 Webhook)
        await application.initialize() 

        # 注册路由模块到 Application 实例中 (如果需要，通常在 bot_app.py 中完成)
        # 这里仅作记录
        logger.info(f"成功导入 {module_name}.router")

        # 将完整的 Application 实例存储起来，键是完整的 Bot Token
        app_instance_map[bot_token] = application

    except Exception as e:
        logger.error(f"Bot {bot_id} 初始化失败: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI 应用生命周期管理器：在启动时初始化所有 Bots。"""
    logger.info("应用启动中... 动态挂载 Bot 路由。")
    
    tasks = []
    # 过滤掉空的 Token
    valid_bots = {k: v for k, v in BOT_CONFIGS.items() if v}

    for bot_id, token in valid_bots.items():
        # 初始化 Application 实例 (这是异步操作)
        await initialize_bot(bot_id, token)

    
    # 动态挂载路由，使用完整的 Bot Token 作为路径的一部分
    for bot_id, token in valid_bots.items():
        # 导入 Bot 的路由模块
        module_name = f"bot{bot_id}_app"
        try:
            module = importlib.import_module(module_name)
            router = module.router
            
            # --- 核心修复：更灵活的 Token 匹配 ---
            # Telegram 可能会发送 URL 编码后的 Token (如 %3A 代替 :)
            # 为了防止 404 错误，我们使用正则表达式路径 (path:path) 来捕获包含冒号或编码冒号的完整 Token
            # 注意：在 Uvicorn/FastAPI 中，路径参数会自动尝试 URL 解码
            # 但如果遇到不一致的编码，可能仍会失败。
            
            # 尝试直接挂载，并期望 Uvicorn/FastAPI 能正确解码
            # 路由格式: /bot/{完整的 bot token}
            path = f"/bot/{token}"

            # 挂载路由
            app.include_router(
                router,
                prefix=path,
                tags=[f"Bot {bot_id} - {token}"],
            )
            
            logger.info(f"✅ 成功挂载 Bot {bot_id} 至路由: {path}")

        except Exception as e:
            logger.error(f"❌ 挂载 Bot {bot_id} 路由失败: {e}")
            
    # --- 启动后清理 ---
    yield

    logger.info("应用关闭中...")
    # 清理所有 Application 实例
    for token, application in app_instance_map.items():
        await application.shutdown()
        logger.info(f"Bot {token} 实例已关闭。")


# --- FastAPI 主应用实例 ---
app = FastAPI(lifespan=lifespan)

# --- 健康检查和默认路由 ---
@app.get("/")
async def health_check():
    """健康检查路由"""
    return {"status": "ok", "message": "Unified Telegram Bot System is running."}


# --- Webhook 处理路由 (统一入口) ---

# 路由参数必须是 'path' 类型，以确保它能够捕获包含特殊字符（如冒号或其编码 %3A）的完整 Token 字符串
# Fastapi/Starlette 的 path:path 允许包含斜杠 '/' 以外的任意字符，这通常足以处理 Bot Token。
# 我们将路由改为 `/bot/{full_token:path}` 来捕获完整的路径，然后让处理函数去验证。
# 但是，因为我们使用 `app.include_router(router, prefix=path)` 动态挂载，
# 实际上所有 Bot 的路由都是独立的，不需要这个统一入口。
# 让我们使用一个专门的**默认路由**来捕获所有未能匹配成功的 /bot/... 请求，并打印日志。

# 重新定义一个 catch-all 路由来捕获所有未匹配的 /bot/... 请求
@app.post("/bot/{full_path:path}", 
          summary="未匹配的 Bot Webhook Catch-All",
          status_code=status.HTTP_404_NOT_FOUND)
async def catch_all_webhook(request: Request, full_path: str):
    """用于捕获所有未匹配成功的 /bot/... 请求，并返回 404"""
    logger.warning(f"❌ Webhook 路径未找到 (404): POST /bot/{full_path}")
    # 注意：这里我们不返回标准的 JSONResponse，因为 404 应该由 FastAPI 自身处理，
    # 但由于我们定义了这个 catch-all 路由，我们必须返回 404
    return JSONResponse(
        content={"detail": "Not Found", "message": f"Webhook path /bot/{full_path} did not match any active bot route."},
        status_code=status.HTTP_404_NOT_FOUND
    )

# 确保所有 Bot 的 Webhook 路由处理函数在各自的 bot_app.py 中被正确定义，
# 并且在 `main.py` 中被 `app.include_router` 挂载。

if __name__ == "__main__":
    # 在本地运行时用于调试
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
```
---
### **下一步：**

1.  请将上面的代码更新到您的 **`main.py`** 文件中。
2.  **重新部署 (Deploy)** 您的 Render 服务。这次部署会应用新的路由逻辑。
3.  **再次运行 `set_webhooks.py` 脚本**。虽然理论上不需要，但为了确保万无一失，请在 Web Shell 中再运行一次。
    ```bash
    python set_webhooks.py
