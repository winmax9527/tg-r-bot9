import os
import logging
import asyncio
import re
import requests # 用于快速获取域名 A
import random
import string
from urllib.parse import urlparse, urlunparse
from typing import List, Dict
from fastapi import FastAPI, Request, Response
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# 引入 Playwright
from playwright.async_api import async_playwright, Playwright, Browser

# --- 1. 配置日志记录 (Logging Setup) ---
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- 2. 全局状态和数据结构 ---
BOT_APPLICATIONS: Dict[str, Application] = {}
BOT_API_URLS: Dict[str, str] = {}
# 这两个将在 startup/shutdown 时被管理
PLAYWRIGHT_INSTANCE: Playwright | None = None
BROWSER_INSTANCE: Browser | None = None

# --- 3. 核心功能：获取动态链接 ---

COMMAND_PATTERN = r"^(苹果链接|ios链接|最新苹果链接|/start_check)$"

# --- 辅助函数 ---
def generate_random_subdomain(k: int = 3) -> str:
    """生成一个 k 位的随机字母和数字组合的字符串"""
    return ''.join(random.choices(string.ascii_letters + string.digits, k=k))

def modify_url_subdomain(url_str: str, new_sub: str) -> str:
    """替换 URL 的二级域名"""
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

# --- 核心处理器 (使用 Playwright) ---
async def get_final_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    完整的多步骤获取链接流程：
    1. [Requests] 访问 API 获取 域名 A
    2. [Playwright] 访问 域名 A 获取 域名 B
    3. 修改 域名 B 的二级域名
    4. 发送最终 URL
    """
    bot_token_end = context.application.bot.token[-4:]
    logger.info(f"Bot {bot_token_end} 收到关键字，开始执行 [Playwright] 链接获取...")

    # 检查全局浏览器是否已启动
    if not BROWSER_INSTANCE or not BROWSER_INSTANCE.is_connected():
        logger.error("全局浏览器实例 BROWSER_INSTANCE 未运行！Playwright 无法工作。")
        await update.message.reply_text("❌ 服务内部错误：浏览器未启动。")
        return

    # 1. 查找此 Bot 专属的 API URL
    current_app = context.application
    api_url_for_this_bot = None
    for path, app in BOT_APPLICATIONS.items():
        if app is current_app:
            api_url_for_this_bot = BOT_API_URLS.get(path)
            break
    
    if not api_url_for_this_bot:
        logger.error(f"Bot (尾号: {bot_token_end}) 无法找到其配置的 API URL！")
        await update.message.reply_text("❌ 服务配置错误：未找到此 Bot 的 API 地址。")
        return

    # 2. 发送“处理中”提示
    try:
        await update.message.reply_text("正在为您获取专属动态链接 (JS模式)，请稍候 (约 10-15 秒)...")
    except Exception as e:
        logger.warning(f"发送“处理中”消息失败: {e}")

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    
    page = None # 确保 page 在 finally 中可被访问
    
    try:
        # --- 步骤 1: [Requests] 访问 API 获取 域名 A (这步很快) ---
        logger.info(f"步骤 1: (Requests) 正在从 API [{api_url_for_this_bot}] 获取 域名 A...")
        response_api = requests.get(api_url_for_this_bot, headers=headers, timeout=10)
        response_api.raise_for_status() 
        domain_a = response_api.text.strip()
        if not domain_a.startswith(('http://', 'https://')):
            domain_a = 'http://' + domain_a
        logger.info(f"步骤 1 成功: 获取到 域名 A -> {domain_a}")

        # --- 步骤 2: [Playwright] 访问 域名 A 获取 域名 B (这步处理 JS) ---
        logger.info(f"步骤 2: (Playwright) 正在启动新页面访问 {domain_a}...")
        
        # 从全局浏览器实例创建新页面
        page = await BROWSER_INSTANCE.new_page()
        page.set_default_timeout(25000) # 25 秒超时

        await page.goto(domain_a, wait_until="networkidle") # 等待网络空闲，确保 JS 执行完毕
        
        domain_b = page.url # 获取浏览器当前的最终 URL
        logger.info(f"步骤 2 成功: 获取到 域名 B -> {domain_b}")

        # --- 步骤 3: 修改 域名 B 的二级域名 ---
        logger.info(f"步骤 3: 正在为 {domain_b} 生成 3 位随机二级域名...")
        random_sub = generate_random_subdomain(3)
        final_modified_url = modify_url_subdomain(domain_b, random_sub)
        logger.info(f"步骤 3 成功: 最终 URL -> {final_modified_url}")

        # --- 步骤 4: 发送最终 URL ---
        await update.message.reply_text(f"✅ 您的专属链接已生成：\n{final_modified_url}")

    except Exception as e:
        logger.error(f"处理 get_final_url (Playwright) 时发生错误: {e}")
        await update.message.reply_text(f"❌ 链接获取失败：{type(e).__name__}。")
    finally:
        if page:
            await page.close() # 关键：一定要关闭页面，否则内存会泄漏！
            logger.info("Playwright 页面已关闭。")


# --- 4. Bot 启动与停止逻辑 (与之前相同) ---
def setup_bot(app_instance: Application, bot_index: int) -> None:
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
app = FastAPI(title="Multi-Bot Playwright Service")

# --- 6. 应用启动/关闭事件 (关键：管理全局浏览器) ---
@app.on_event("startup")
async def startup_event():
    """在 FastAPI 启动时：1. 初始化 Bot 2. 启动全局 Playwright 浏览器"""
    
    global BOT_APPLICATIONS, BOT_API_URLS, PLAYWRIGHT_INSTANCE, BROWSER_INSTANCE
    BOT_APPLICATIONS = {}
    BOT_API_URLS = {}

    logger.info("应用启动中... 正在查找 Bot Token 和 专属 API URL。")

    # 6.1 初始化所有 Bot (和之前一样)
    for i in range(1, 10): 
        token_name = f"BOT_TOKEN_{i}"
        api_url_name = f"BOT_{i}_API_URL"
        token_value = os.getenv(token_name)
        api_url_value = os.getenv(api_url_name)
        
        if token_value and api_url_value:
            logger.info(f"DIAGNOSTIC: 发现 Bot #{i}: Token (尾号: {token_value[-4:]}) 及其专属 API (值: {api_url_value})")
            
            application = Application.builder().token(token_value).build()
            
            # 关键：将 app 实例存入 context，以便 handler 能访问 app.state
            application.state = app 
            
            await application.initialize()
            
            setup_bot(application, i)
            
            webhook_path = f"bot{i}_webhook"
            BOT_APPLICATIONS[webhook_path] = application
            BOT_API_URLS[webhook_path] = api_url_value 
            
            logger.info(f"Bot #{i} (尾号: {token_value[-4:]}) 已创建并初始化。监听路径: /{webhook_path}")
            
        elif token_value and not api_url_value:
            logger.warning(f"DIAGNOSTIC: 发现 Bot #{i} 的 Token，但未找到 {api_url_name}。此 Bot 将无法工作。")

    if not BOT_APPLICATIONS:
        logger.error("❌ 未找到任何配置完整的 Bot (必须同时有 Token 和 专属 API URL)。")
    else:
        logger.info(f"✅ 成功初始化 {len(BOT_APPLICATIONS)} 个 Bot 实例。")

    # 6.2 启动 Playwright
    logger.info("正在启动全局 Playwright 实例...")
    try:
        PLAYWRIGHT_INSTANCE = await async_playwright().start()
        # 启动 Chromium。我们使用 --no-sandbox 标志，这在 Render 的 Docker 环境中是必需的
        BROWSER_INSTANCE = await PLAYWRIGHT_INSTANCE.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        app.state.browser = BROWSER_INSTANCE # 将浏览器实例存入 FastAPI state
        logger.info("🎉 全局 Playwright Chromium 浏览器启动成功！")
        logger.info("🎉 核心服务启动完成。等待 Telegram 的 Webhook 消息...")
    except Exception as e:
        logger.error(f"❌ 启动 Playwright 失败: {e}")
        logger.error("服务将启动，但 Playwright 功能将无法工作！")

@app.on_event("shutdown")
async def shutdown_event():
    """在 FastAPI 关闭时，优雅地关闭浏览器和 Playwright"""
    logger.info("应用关闭中...")
    if BROWSER_INSTANCE:
        await BROWSER_INSTANCE.close()
        logger.info("全局浏览器已关闭。")
    if PLAYWRIGHT_INSTANCE:
        await PLAYWRIGHT_INSTANCE.stop()
        logger.info("Playwright 实例已停止。")
    logger.info("应用关闭完成。")

# --- 7. 动态 Webhook 路由 (与之前相同) ---
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

# --- 8. 健康检查路由 (与之前相同) ---
@app.get("/")
async def root():
    browser_status = "未运行"
    if BROWSER_INSTANCE and BROWSER_INSTANCE.is_connected():
        browser_status = f"运行中 (Version: {BROWSER_INSTANCE.version})"

    active_bots_info = {}
    for path, app in BOT_APPLICATIONS.items():
        active_bots_info[path] = {
            "token_end": app.bot.token[-4:],
            "api_url": BOT_API_URLS.get(path, "未设置!")
        }
    status = {
        "status": "OK",
        "message": "Telegram Multi-Bot (Playwright JS) service is running.",
        "browser_status": browser_status,
        "active_bots_count": len(BOT_APPLICATIONS),
        "active_bots_info": active_bots_info
    }
    return status
