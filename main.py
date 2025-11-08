import os
import logging
import asyncio
from typing import List, Tuple, Callable, Awaitable
from fastapi import FastAPI
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# --- 1. 配置日志记录 (Logging Setup) ---
# 设置 Python 日志格式，确保日志信息清晰
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- 2. FastAPI 应用实例 ---
# Gunicorn worker 将加载此应用实例
app = FastAPI(title="Multi-Bot Telegram Handler")

# --- 3. 全局状态和数据结构 ---
# 存储所有 Bot Application 实例
BOT_APPLICATIONS: List[Application] = []

# --- 4. Bot 核心命令处理函数 (Handlers) ---

# /start 命令
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """回复 /start 命令，并显示当前 Bot ID。"""
    # 从 context.application.bot.token 获取当前 Bot 的 Token
    bot_token_end = context.application.bot.token[-4:]
    
    # 尝试查找 BOT_APPLICATIONS 列表，看它是第几个 Bot
    bot_index = -1
    for idx, app_instance in enumerate(BOT_APPLICATIONS, 1):
        if app_instance.bot.token == context.application.bot.token:
            bot_index = idx
            break

    message = (
        f"🤖 你好！我是 Bot **#{bot_index}**。"
        f"\n(我的 Token 尾号是: `{bot_token_end}`)"
        "\n\n请发送消息给我，我会复读你的内容！"
        "\n你可以使用 /help 查看可用命令。"
    )
    # 使用 reply_html 发送消息
    await update.message.reply_html(message)

# /help 命令
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """回复 /help 命令。"""
    message = (
        "📚 **可用命令:**\n"
        "/start - 启动 Bot 并获取 Bot ID\n"
        "/help - 显示此帮助信息\n"
        "\n任何其他消息将作为文本复读。"
    )
    # 使用 reply_html 发送消息
    await update.message.reply_html(message)

# 消息处理函数（复读功能）
async def echo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """复读用户发送的文本消息。"""
    if update.message and update.message.text:
        text = update.message.text
        # 记录 Bot Token 的末尾四位进行诊断
        logger.info(f"Bot {context.application.bot.token[-4:]} 收到消息: {text[:50]}...")
        await update.message.reply_text(f"你说了: \n\n{text}")

# --- 5. Bot 启动与停止逻辑 ---

def setup_bot(app_instance: Application, bot_index: int) -> None:
    """配置 Bot 的所有处理器 (Handlers)。"""
    
    # 打印 Bot 正在配置的诊断信息
    token_end = app_instance.bot.token[-4:]
    logger.info(f"Bot Application 实例 (#{bot_index}, 尾号: {token_end}) 正在配置 Handlers。")

    # 添加 Handlers
    app_instance.add_handler(CommandHandler("start", start_command))
    app_instance.add_handler(CommandHandler("help", help_command))
    
    # 过滤掉命令，只处理普通文本消息
    app_instance.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo_message))

    
async def start_bots():
    """初始化所有 Bot 应用并启动它们。"""
    
    # 1. 查找环境变量中的 Bot Token
    token_list = []
    # 检查 BOT_TOKEN_1 到 BOT_TOKEN_9
    for i in range(1, 10): 
        token_name = f"BOT_TOKEN_{i}"
        token_value = os.getenv(token_name)
        if token_value:
            # 记录诊断信息
            logger.info(f"DIAGNOSTIC: 发现环境变量 {token_name}。Token 尾号: {token_value[-4:]}")
            token_list.append(token_value)
        else:
            logger.info(f"DIAGNOSTIC: 环境变量 {token_name} 未设置。")

    if not token_list:
        logger.error("❌ 未找到任何有效的 Bot Token。请检查环境变量 BOT_TOKEN_N 的设置。")
        return

    logger.info(f"✅ 成功找到 {len(token_list)} 个 Bot Token。开始初始化...")
    
    # 2. 创建并配置 Application 实例
    for idx, token in enumerate(token_list, 1):
        try:
            # 创建 Application 实例
            application = Application.builder().token(token).build()
            
            # 配置 Handlers (使用通用的 setup_bot 函数)
            setup_bot(application, idx)
            
            # 将实例添加到全局列表
            BOT_APPLICATIONS.append(application)
            
            logger.info(f"Bot Application 实例已为 Token (尾号: {token[-4:]}) 创建。分配 Bot ID: #{idx}")
            
        except Exception as e:
            logger.error(f"初始化 Bot Application 失败 (Token 尾号: {token[-4:]})：{e}")


# --- 6. FastAPI 生命周期钩子 (Lifespan Hooks) ---

@app.on_event("startup")
async def on_startup():
    """FastAPI 启动时执行 Bot 逻辑。"""
    logger.info("应用启动中... 正在启动 Bot Applications 的后台任务。")
    # 启动所有 Bot
    await start_bots()
    
    # 启动所有 Bot 的 Long Polling
    if BOT_APPLICATIONS:
        # 在后台以非阻塞方式启动所有 Bot 的轮询
        for app_instance in BOT_APPLICATIONS:
            # 使用 asyncio.create_task 在后台启动轮询
            asyncio.create_task(app_instance.run_polling(drop_pending_updates=True, stop_on_shutdown=True))
        logger.info("🎉 核心服务启动完成。所有 Bot 已开始轮询。")
    else:
        logger.warning("服务启动完成，但没有 Bot 运行。")


@app.on_event("shutdown")
async def on_shutdown():
    """FastAPI 关闭时停止 Bot 逻辑。"""
    logger.info("应用关闭中... 正在停止 Bot Applications 的后台任务。")
    
    # 优雅地停止所有 Bot 的轮询
    for app_instance in BOT_APPLICATIONS:
        try:
            # 使用 shutdown() 优雅地停止轮询任务
            await app_instance.shutdown()
        except Exception as e:
            logger.error(f"Bot Application 关闭失败 (Token 尾号: {app_instance.bot.token[-4:]})：{e}")
            
    logger.info("应用关闭完成。")


# --- 7. 健康检查路由 ---
# 这是一个必要的路由，确保 web 容器知道应用正在运行
@app.get("/")
async def root():
    """健康检查路由，返回 Bot 状态信息。"""
    status = {
        "status": "OK",
        "message": "Telegram Multi-Bot service is running.",
        "active_bots": len(BOT_APPLICATIONS),
        "bot_tokens_found": [app.bot.token[-4:] for app in BOT_APPLICATIONS]
    }
    return status

# --- End of main.py ---
