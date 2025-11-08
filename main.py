import os
import logging
import asyncio
import re
import requests
import random
import string
from urllib.parse import urlparse, urlunparse
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

# !!! 关键：您必须在 Render 环境变量中设置这个值 !!!
# 这是您说的“固定的api地址”
API_URL_FOR_A = os.getenv("API_URL_FOR_A")

# 定义触发关键字 (正则表达式)
COMMAND_PATTERN = r"^(苹果链接|ios链接|最新苹果链接|/start_check)$"

# --- 辅助函数 ---
def generate_random_subdomain(k: int = 3) -> str:
    """生成一个 k 位的随机字母和数字组合的字符串"""
    return ''.join(random.choices(string.ascii_letters + string.digits, k=k))

def modify_url_subdomain(url_str: str, new_sub: str) -> str:
    """
    替换 URL 的二级域名。
    例如：modify_url_subdomain("https://sub.example.com/path", "xyz")
    返回: "https://xyz.example.com/path"
    """
    try:
        parsed = urlparse(url_str)
        
        # 分割域名: e.g., 'sub.example.com' -> ['sub', 'example', 'com']
        domain_parts = parsed.netloc.split('.')
        
        if len(domain_parts) < 2:
            # 无法修改 (例如 'localhost' 或 IP 地址)
            return url_str
        
        # 替换第一部分 (二级域名)
        domain_parts[0] = new_sub
        
        # 重新组合
        new_netloc = '.'.join(domain_parts)
        
        # 使用 _replace 重新构建 URL
        new_parsed = parsed._replace(netloc=new_netloc)
        
        return new_parsed.geturl()
        
    except Exception as e:
        logger.error(f"修改子域名失败: {e} - URL: {url_str}")
        return url_str # 修改失败则返回原 URL

# --- 核心处理器 ---
async def get_final_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    完整的多步骤获取链接流程：
    1. 访问 API_URL_FOR_A 获取 域名 A
    2. 访问 域名 A 获取 域名 B
    3. 修改 域名 B 的二级域名
    4. 发送最终 URL
    """
    bot_token_end = context.application.bot.token[-4:]
    logger.info(f"Bot {bot_token_end} 收到关键字，开始执行多步链接获取...")

    # 0. 检查 API_URL 是否已配置
    if not API_URL_FOR_A:
        logger.error("环境变量 API_URL_FOR_A 未设置！机器人无法获取域名 A。")
        await update.message.reply_text("❌ 服务配置错误：未找到 API 地址。")
        return

    # 1. 发送“处理中”提示
    try:
        await update.message.reply_text("正在为您获新下载链接，请稍候...")
    except Exception as e:
        logger.warning(f"发送“处理中”消息失败: {e}")

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }

    try:
        # --- 步骤 1: 访问 API 获取 域名 A ---
        logger.info(f"步骤 1: 正在从 API [{API_URL_FOR_A}] 获取 域名 A...")
        response_api = requests.get(API_URL_FOR_A, headers=headers, timeout=10)
        response_api.raise_for_status() # 如果 API 返回 4xx/5xx，则抛出异常
        
        # 假设 API 返回的是纯文本 URL
        domain_a = response_api.text.strip()
        if not domain_a.startswith(('http://', 'https://')):
            domain_a = 'http://' + domain_a
            
        logger.info(f"步骤 1 成功: 获取到 域名 A -> {domain_a}")

        # --- 步骤 2: 访问 域名 A 获取 域名 B (跟踪重定向) ---
        logger.info(f"步骤 2: 正在访问 {domain_a} 以获取 域名 B...")
        response_redirect = requests.get(domain_a, headers=headers, allow_redirects=True, timeout=15)
        response_redirect.raise_for_status()
        
        domain_b = response_redirect.url # requests 会自动处理重定向
        logger.info(f"步骤 2 成功: 获取到 域名 B -> {domain_b}")

        # --- 步骤 3: 修改 域名 B 的二级域名 ---
        logger.info(f"步骤 3: 正在为 {domain_b} 生成 3 位随机二级域名...")
        random_sub = generate_random_subdomain(3)
        final_modified_url = modify_url_subdomain(domain_b, random_sub)
        
        logger.info(f"步骤 3 成功: 最终 URL -> {final_modified_url}")

        # --- 步骤 4: 发送最终 URL ---
        await update.message.reply_text(f"✅ 您的专属链接已生成：\n{final_modified_url}")

    except requests.exceptions.Timeout:
        logger.error("链接获取超时 (Timeout)")
        await update.message.reply_text("❌ 链接获取失败：请求超时。")
    except requests.exceptions.HTTPError as e:
        logger.error(f"HTTP 错误: {e}")
        await update.message.reply_text(f"❌ 链接获取失败：目标服务器返回错误 (HTTP {e.response.status_code})。")
    except requests.exceptions.RequestException as e:
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
app = FastAPI(title="Multi-Bot Dynamic Link Service")

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
        "message": "Telegram Multi-Bot (Custom Dynamic Link) service is running.",
        "active_bots_count": len(BOT_APPLICATIONS),
        "api_url_configured": "已设置" if API_URL_FOR_A else "未设置 - 机器人将无法工作",
        "active_bots_info": active_bots_info
    }
    return status

# --- 9. 别忘了更新 requirements.txt ---
# 确保您的 requirements.txt 文件中包含:
# fastapi
# uvicorn[standard]
# python-telegram-bot
# requests
