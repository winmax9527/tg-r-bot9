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
# 存储 Bot 实例 (按 webhook 路径索引)
BOT_APPLICATIONS: Dict[str, Application] = {}
# 存储 Bot 专属的 API URL (按 webhook 路径索引)
BOT_API_URLS: Dict[str, str] = {}

# --- 3. 核心功能：获取动态链接 (这就是您要的功能) ---

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
        domain_parts = parsed.netloc.split('.')
        if len(domain_parts) < 2: return url_str
        domain_parts[0] = new_sub
        new_netloc = '.'.join(domain_parts)
        new_parsed = parsed._replace(netloc=new_netloc)
        return new_parsed.geturl()
    except Exception as e:
        logger.error(f"修改子域名失败: {e} - URL: {url_str}")
        return url_str

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

    # --- 
    # 关键修改：根据当前 Bot 实例查找其对应的 Webhook 路径和 API URL
    # ---
    current_app = context.application
    webhook_path = None
    api_url_for_this_bot = None
    
    for path, app in BOT_APPLICATIONS.items():
        if app is current_app:
            webhook_path = path
            api_url_for_this_bot = BOT_API_URLS.get(path) # 从我们的新字典中查找 API URL
            break
    
    if not api_url_for_this_bot:
        logger.error(f"Bot (尾号: {bot_token_end}) 无法找到其配置的 API URL！(Webhook 路径: {webhook_path})")
        await update.message.reply_text("❌ 服务配置错误：未找到此 Bot 的 API 地址。")
        return

    # 1. 发送“处理中”提示
    try:
        await update.message.reply_text("正在为您获取专属动态链接，请稍候...")
    except Exception as e:
        logger.warning(f"发送“处理中”消息失败: {e}")

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }

    try:
        # --- 步骤 1: 访问 API 获取 域名 A ---
        logger.info(f"步骤 1: Bot {bot_token_end} 正在从其专属 API [{api_url_for_this_bot}] 获取 域名 A...")
        response_api = requests.get(api_url_for_this_bot, headers=headers, timeout=10)
        response_api.raise_for_status() 
        
        domain_a = response_api.text.strip()
        if not domain_a.startswith(('http://', 'https://')):
            domain_a = 'http://' + domain_a
            
        logger.info(f"步骤 1 成功: 获取到 域名 A -> {domain_a}")

        # --- 步骤 2: 访问 域名 A 获取 域名 B (跟踪重定向) ---
        logger.info(f"步骤 2: 正在访问 {domain_a} 以获取 域名 B...")
        response_redirect = requests.get(domain_a, headers=headers, allow_redirects=True, timeout=15)
        response_redirect.raise_for_status()
        
        domain_b = response_redirect.url
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

    app_instance.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex(COMMAND_PATTERN), 
            get_final_url
        )
    )
    
    async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_html(f"🤖 Bot #{bot_index} (尾号: {token_end}) 已准备就绪。\n请发送关键字 (如: 苹果链接) 来获取动态链接。")
    
    app_instance.add_handler(CommandHandler("start", start_command))
    

# --- 5. FastAPI 应用实例 ---
app = FastAPI(title="Multi-Bot Dynamic Link Service")

# --- 6. 应用启动时，初始化所有 Bot ---
@app.on_event("startup")
async def startup_event():
    """在 FastAPI 启动时初始化所有 Bot Application 实例。"""
    
    global BOT_APPLICATIONS, BOT_API_URLS
    BOT_APPLICATIONS = {}
    BOT_API_URLS = {}

    logger.info("应用启动中... 正在查找 Bot Token 和 专属 API URL。")

    for i in range(1, 10): # 检查 1 到 9
        token_name = f"BOT_TOKEN_{i}"
        api_url_name = f"BOT_{i}_API_URL" # 匹配您截图中的 Key
        
        token_value = os.getenv(token_name)
        api_url_value = os.getenv(api_url_name) # 获取专属 API URL
        
        # 必须同时找到 Token 和 专属 API URL，这个 Bot 才算配置完整
        if token_value and api_url_value:
            logger.info(f"DIAGNOSTIC: 发现 Bot #{i}: Token (尾号: {token_value[-4:]}) 及其专属 API (值: {api_url_value})")
            
            application = Application.builder().token(token_value).build()
            
            await application.initialize()
            
            setup_bot(application, i)
            
            webhook_path = f"bot{i}_webhook"
            BOT_APPLICATIONS[webhook_path] = application
            BOT_API_URLS[webhook_path] = api_url_value # 关键：存储这个 Bot 的专属 API URL
            
            logger.info(f"Bot #{i} (尾号: {token_value[-4:]}) 已创建并初始化。监听路径: /{webhook_path}")
            
        elif token_value and not api_url_value:
            logger.warning(f"DIAGNOSTIC: 发现 Bot #{i} 的 Token，但未找到 {api_url_name}。此 Bot 将无法工作。")

    if not BOT_APPLICATIONS:
        logger.error("❌ 未找到任何配置完整的 Bot (必须同时有 Token 和 专属 API URL)。")
    else:
        logger.info(f"✅ 成功初始化 {len(BOT_APPLICATIONS)} 个 Bot 实例。")
        logger.info("🎉 核心服务启动完成。等待 Telegram 的 Webhook 消息...")

# --- 7. 动态 Webhook 路由 ---
@app.post("/{webhook_path}")
async def handle_webhook(webhook_path: str, request: Request):
    
    if webhook_path not in BOT_APPLICATIONS:
        logger.warning(f"收到未知路径的请求: /{webhook_path}")
        return Response(status_code=404) 

    application = BOT_APPLICATIONS[webhook_path]
    
    try:
        update_data = await request.json()
        update = Update.de_json(update_data, application.bot)
        
        await application.process_update(update)
        
        return Response(status_code=200) # OK
        
    except Exception as e:
        logger.error(f"处理 Webhook 请求失败 (路径: /{webhook_path})：{e}")
        return Response(status_code=500) 

# --- 8. 健康检查路由 ---
@app.get("/")
async def root():
    """健康检查路由，返回 Bot 状态信息。"""
    active_bots_info = {}
    for path, app in BOT_APPLICATIONS.items():
        active_bots_info[path] = {
            "token_end": app.bot.token[-4:],
            "api_url": BOT_API_URLS.get(path, "未设置!")
        }
        
    status = {
        "status": "OK",
        "message": "Telegram Multi-Bot (Per-Bot API URL) service is running.",
        "active_bots_count": len(BOT_APPLICATIONS),
        "active_bots_info": active_bots_info
    }
    return status
