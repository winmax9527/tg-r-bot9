import os
import logging
import asyncio
import re
import requests # 引入 requests 库用于发起 HTTP 请求
from typing import List, Dict
from fastapi import FastAPI, Request, Response
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# --- 1. 配置日志记录 (Logging Setup) ---
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- 2. 全局状态和数据结构 ---
BOT_APPLICATIONS: Dict[str, Application] = {}
BOT_WEBHOOK_PATHS: Dict[str, str] = {}

# --- 3. 核心功能：获取动态链接 (这就是您要的功能) ---

# 定义您要追踪的域名 A
# ！！！重要：请确保这里的值是正确的 ！！！
DOMAIN_A = "http://your-dynamic-domain-a.com" # 替换成您要追踪的域名 A

# 定义触发关键字 (正则表达式)
COMMAND_PATTERN = r"^(苹果链接|ios链接|最新苹果链接|/start_check)$"

async def get_final_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    核心功能：当收到关键字时，请求域名 A 并返回重定向后的最终域名 B。
    """
    bot_token_end = context.application.bot.token[-4:]
    logger.info(f"Bot {bot_token_end} 收到关键字，开始获取动态链接...")
    
    # 1. 发送“处理中”提示
    try:
        await update.message.reply_text("正在为您获取最终动态链接，请稍候...")
    except Exception as e:
        logger.warning(f"发送“处理中”消息失败: {e}")

    # 2. 执行 HTTP 请求
    try:
        # 使用 requests 库发起 GET 请求
        # allow_redirects=True (默认) 确保库会自动跟随重定向
        # timeout=15 防止请求等待时间过长
        # 添加 User-Agent 模拟浏览器
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        response = requests.get(DOMAIN_A, headers=headers, allow_redirects=True, timeout=15)
        
        # 检查 HTTP 状态码是否成功
        if response.status_code == 200:
            # response.url 就是最终重定向后的 URL (域名 B)
            final_url_b = response.url
            
            # 3. 将最终 URL 发送给用户
            logger.info(f"Bot {bot_token_end} 成功获取链接: {final_url_b}")
            await update.message.reply_text(f"✅ 最终动态域名 B 是：\n{final_url_b}")
        else:
            logger.error(f"链接获取失败，域名 A 返回了错误状态码: {response.status_code}")
            await update.message.reply_text(f"❌ 链接获取失败，目标服务器返回: {response.status_code}")
            
    except requests.exceptions.Timeout:
        logger.error(f"链接获取超时 (Timeout) 访问 {DOMAIN_A}")
        await update.message.reply_text("❌ 链接获取失败：请求超时。")
    except requests.exceptions.RequestException as e:
        # 捕获所有其他网络异常
        logger.error(f"链接获取出现网络错误: {e}")
        await update.message.reply_text(f"❌ 链接获取失败，出现网络错误。")
    except Exception as e:
        logger.error(f"处理 get_final_url 时发生未知错误: {e}")
        await update.message.reply_text(f"❌ 处理请求时发生内部错误。")


# --- 4. Bot 启动与停止逻辑 ---

def setup_bot(app_instance: Application, bot_index: int) -> None:
    """配置 Bot 的所有处理器 (Handlers)。"""
    token_end = app_instance.bot.token[-4:]
    logger.info(f"Bot Application 实例 (#{bot_index}, 尾号: {token_end}) 正在配置 Handlers。")

    # 关键：使用 MessageHandler 捕获所有匹配 COMMAND_PATTERN 的文本
    app_instance.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex(COMMAND_PATTERN), 
            get_final_url
        )
    )
    
    # 您也可以保留 /start
    async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_html(f"🤖 Bot #{bot_index} (尾号: {token_end}) 已准备就绪。\n请发送关键字 (如: 苹果链接) 来获取动态链接。")
    
    app_instance.add_handler(CommandHandler("start", start_command))
    

# --- 5. FastAPI 应用实例 ---
app = FastAPI(title="Multi-Bot Telegram Webhook Handler")

# --- 6. 应用启动时，初始化所有 Bot ---
@app.on_event("startup")
async def startup_event():
    """在 FastAPI 启动时初始化所有 Bot Application 实例。"""
    
    global BOT_APPLICATIONS, BOT_WEBHOOK_PATHS
    BOT_APPLICATIONS = {}
    BOT_WEBHOOK_PATHS = {}

    logger.info("应用启动中... 正在查找 Bot Token 并创建 Application 实例。")

    for i in range(1, 10): # 检查 BOT_TOKEN_1 到 BOT_TOKEN_9
        token_name = f"BOT_TOKEN_{i}"
        token_value = os.getenv(token_name)
        
        if token_value:
            logger.info(f"DIAGNOSTIC: 发现环境变量 {token_name}。Token 尾号: {token_value[-4:]}")
            
            application = Application.builder().token(token_value).build()
            
            # 关键修复：异步初始化
            await application.initialize()
            
            # 配置 Handlers (配置为获取链接功能)
            setup_bot(application, i)
            
            webhook_path = f"bot{i}_webhook"
            BOT_APPLICATIONS[webhook_path] = application
            BOT_WEBHOOK_PATHS[token_value] = webhook_path
            
            logger.info(f"Bot #{i} (尾号: {token_value[-4:]}) 已创建并初始化。监听路径: /{webhook_path}")
            
        else:
            # 仅在调试时取消注释
            # logger.info(f"DIAGNOSTIC: 环境变量 {token_name} 未设置。")
            pass

    if not BOT_APPLICATIONS:
        logger.error("❌ 未找到任何有效的 Bot Token。请检查环境变量 BOT_TOKEN_N 的设置。")
    else:
        logger.info(f"✅ 成功初始化 {len(BOT_APPLICATIONS)} 个 Bot 实例。")
        logger.info("🎉 核心服务启动完成。等待 Telegram 的 Webhook 消息...")

# --- 7. 动态 Webhook 路由 ---
@app.post("/{webhook_path}")
async def handle_webhook(webhook_path: str, request: Request):
    
    if webhook_path not in BOT_APPLICATIONS:
        logger.warning(f"收到未知路径的请求: /{webhook_path}")
        return Response(status_code=404) # Not Found

    application = BOT_APPLICATIONS[webhook_path]
    token_end = application.bot.token[-4:]
    
    try:
        update_data = await request.json()
        update = Update.de_json(update_data, application.bot)
        
        # logger.info(f"Bot (尾号: {token_end}) 正在处理 Webhook 请求 (路径: /{webhook_path})")
        
        await application.process_update(update)
        
        return Response(status_code=200) # OK
        
    except Exception as e:
        logger.error(f"处理 Webhook 请求失败 (路径: /{webhook_path})：{e}")
        return Response(status_code=500) # Internal Server Error

# --- 8. 健康检查路由 ---
@app.get("/")
async def root():
    """健康检查路由，返回 Bot 状态信息。"""
    active_bots_info = {}
    for path, app in BOT_APPLICATIONS.items():
        active_bots_info[path] = f"Token 尾号: {app.bot.token[-4:]}"
        
    status = {
        "status": "OK",
        "message": "Telegram Multi-Bot (Dynamic Link Fetcher) service is running.",
        "active_bots_count": len(BOT_APPLICATIONS),
        "active_bots_info": active_bots_info
    }
    return status

# --- 9. 别忘了更新 requirements.txt ---
# 确保您的 requirements.txt 文件中包含:
# fastapi
# uvicorn[standard]
# python-telegram-bot
# requests
