import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BOT_ROUTERS = []
BOT_KEYS = ["1", "4", "6", "9"]

for key in BOT_KEYS:
    try:
        module_name = f"bot{key}_app"
        module = __import__(module_name)
        router = getattr(module, "router")
        
        BOT_ROUTERS.append({
            "router": router,
            "token_key": key,
            "tags": [f"bot{key}_service"]
        })
        logger.info(f"成功导入 {module_name}.router")

    except Exception as e:
        logger.warning(f"导入 Bot {key} 失败: {e}")

BOT_TOKENS = {
    "1": os.getenv("TELEGRAM_BOT_TOKEN_1"),
    "4": os.getenv("TELEGRAM_BOT_TOKEN_4"),
    "6": os.getenv("TELEGRAM_BOT_TOKEN_6"),
    "9": os.getenv("TELEGRAM_BOT_TOKEN_9"),
}

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("应用启动中... 动态挂载 Bot 路由。")
    
    for config in BOT_ROUTERS:
        token_key = config["token_key"]
        token = BOT_TOKENS.get(token_key)
        
        if not token:
            logger.warning(f"TELEGRAM_BOT_TOKEN_{token_key} 环境变量未找到，Bot {token_key} 未挂载。")
            continue
            
        try:
            prefix = f"/bot/{token}"
            app.include_router(
                config["router"],
                prefix=prefix,
                tags=config["tags"]
            )
            logger.info(f"✅ 成功挂载 Bot {token_key} 至路由: {prefix}")
        except Exception as e:
            logger.error(f"❌ 挂载 Bot {token_key} 时出错: {e}")
    
    yield
    logger.info("应用关闭中...")

app = FastAPI(title="统一 Telegram Bot Webhook 系统", lifespan=lifespan)

@app.get("/")
def read_root():
    return {"status": "ok", "message": "Unified Bot System is running"}
