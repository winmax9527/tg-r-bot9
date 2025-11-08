import os
import logging
from fastapi import FastAPI, Request
from telegram import Bot, Update
from telegram.ext import Dispatcher, CommandHandler, MessageHandler, filters

# --- 配置 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 存储所有 Bot 的 token 和对应的 Dispatcher
BOT_TOKENS = {
    "1": os.environ.get("BOT_TOKEN_1"),
    "4": os.environ.get("BOT_TOKEN_4"),
    "6": os.environ.get("BOT_TOKEN_6"),
    "9": os.environ.get("BOT_TOKEN_9"),
}

# 过滤掉未设置 token 的 Bot
ACTIVE_BOTS = {bot_id: token for bot_id, token in BOT_TOKENS.items() if token}

bot_dispatchers = {}

# --- Bot 处理器函数 ---
def start(update: Update, context):
    """处理 /start 命令"""
    chat_id = update.effective_chat.id
    bot_token = context.bot.token
    
    # 查找当前的 Bot ID
    current_bot_id = next((bot_id for bot_id, token in ACTIVE_BOTS.items() if token == bot_token), "未知")
    
    context.bot.send_message(
        chat_id=chat_id, 
        text=f"你好！我是 Bot {current_bot_id} (Token: {bot_token[:4]}...{bot_token[-4:]})。\n"
             f"我的 Webhook 正在运行中！"
    )
    logger.info(f"Bot {current_bot_id} 收到 /start 命令 from {chat_id}")

def echo(update: Update, context):
    """回显用户发送的文本消息"""
    chat_id = update.effective_chat.id
    text = update.message.text
    context.bot.send_message(chat_id=chat_id, text=f"你说了: {text}")
    logger.info(f"Bot 收到消息: {text} from {chat_id}")

# --- 初始化 Bots 和 Dispatchers ---
def initialize_bots_and_dispatchers():
    """初始化所有活跃的 Bot 对象和 Dispatcher"""
    global bot_dispatchers
    
    if not ACTIVE_BOTS:
        logger.error("❌ 未找到任何有效的 Bot Token，应用将无法处理 Webhook。")
        return

    for bot_id, token in ACTIVE_BOTS.items():
        try:
            bot = Bot(token=token)
            dispatcher = Dispatcher(bot, None, workers=0, use_context=True)
            
            # 注册处理器
            dispatcher.add_handler(CommandHandler("start", start))
            dispatcher.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), echo))
            
            bot_dispatchers[token] = dispatcher
            logger.info(f"✅ Bot {bot_id} 初始化完成，Token 尾号: {token[-4:]}")

        except Exception as e:
            logger.error(f"❌ 初始化 Bot {bot_id} 失败: {e}")

# 在应用启动时调用初始化函数
initialize_bots_and_dispatchers()

# --- FastAPI 应用实例 ---
app = FastAPI(title="Multi-Bot Telegram Webhook Handler")

@app.on_event("startup")
async def startup_event():
    """应用启动时打印信息"""
    logger.info("应用启动中... 动态挂载 Bot 路由。")

@app.get("/")
def home():
    """根路径健康检查"""
    return {"status": "ok", "message": "Multi-Bot Handler is running."}


# 动态创建和处理 Webhook 路由
# ⚠️ 关键修改：路由路径现在包含 /webhook 后缀，以匹配 set_webhooks.py 中设置的 URL
@app.post("/bot/{token}/webhook")
async def process_webhook(token: str, request: Request):
    """处理来自 Telegram 的 Webhook 更新"""
    if token not in bot_dispatchers:
        logger.warning(f"❌ 收到未知 Token 的请求: {token[:4]}...{token[-4:]}")
        return {"status": "error", "message": "Unknown bot token"}

    dispatcher = bot_dispatchers[token]
    
    try:
        # 获取请求体 (Update 对象)
        body = await request.json()
        
        # 将 JSON 转换为 Telegram Update 对象
        update = Update.de_json(body, dispatcher.bot)
        
        # 将更新放入 Dispatcher 队列
        dispatcher.process_update(update)
        
        logger.info(f"✅ Bot {token[-4:]} 成功处理了一个更新。")
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
