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
BOT_APK_URLS: Dict[str, str] = {}
PLAYWRIGHT_INSTANCE: Playwright | None = None
BROWSER_INSTANCE: Browser | None = None

# --- 3. 核心功能：获取动态链接 ---

# 需求 1: 通用链接 (iOS/安卓) 关键字
UNIVERSAL_COMMAND_PATTERN = r"^(地址|下载地址|下载链接|最新地址|安卓地址|苹果地址|安卓下载地址|苹果下载地址|链接|最新链接|安卓链接|安卓下载链接|最新安卓链接|苹果链接|苹果下载链接|ios链接|最新苹果链接)$"

# 需求 2: 安卓专用链接 关键字
ANDROID_SPECIFIC_COMMAND_PATTERN = r"^(安卓直接下载|安卓专用|安卓专用链接|安卓提包链接|安卓专用地址|安卓提包地址|安卓专用下载|安卓提包)$"

# --- 辅助函数 ---
def generate_universal_subdomain(min_len: int = 4, max_len: int = 7) -> str:
    """(需求 1) 生成一个 4-7 位随机长度的字符串"""
    length = random.randint(min_len, max_len)
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))

def generate_android_specific_subdomain(min_len: int = 5, max_len: int = 9) -> str:
    """(需求 2) 生成一个 5-9 位随机长度的字符串"""
    length = random.randint(min_len, max_len)
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))

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

# --- 核心处理器 1 (Playwright - 通用链接) ---
async def get_universal_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    (需求 1)
    1. [Requests] 访问 API 获取 域名 A
    2. [Playwright] 访问 域名 A 获取 域名 B
    3. 修改 域名 B 的二级域名 (3-7位)
    4. 发送最终 URL
    """
    bot_token_end = context.application.bot.token[-4:]
    logger.info(f"Bot {bot_token_end} 收到 [通用链接] 关键字，开始执行 [Playwright] 链接获取...")

    # 1. 检查浏览器
    fastapi_app = context.bot_data.get("fastapi_app")
    if not fastapi_app or not hasattr(fastapi_app.state, 'browser') or not fastapi_app.state.browser or not fastapi_app.state.browser.is_connected():
        logger.error("全局浏览器实例未运行或未连接！Playwright 无法工作。")
        await update.message.reply_text("❌ 服务内部错误：浏览器未启动。")
        return

    # 2. 查找此 Bot 专属的 API URL
    current_app = context.application
    api_url_for_this_bot = None
    for path, app_instance in BOT_APPLICATIONS.items():
        if app_instance is current_app:
            api_url_for_this_bot = BOT_API_URLS.get(path)
            break
    
    if not api_url_for_this_bot:
        logger.error(f"Bot (尾号: {bot_token_end}) 无法找到其配置的 API URL！")
        await update.message.reply_text("❌ 服务配置错误：未找到此 Bot 的 API 地址。")
        return

    # 3. 发送“处理中”提示
    try:
        await update.message.reply_text("正在为您获取专属通用下载链接，请稍候 ...")
    except Exception as e:
        logger.warning(f"发送“处理中”消息失败: {e}")

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    
    page = None 
    
    try:
        # --- 步骤 1: [Requests] 访问 API 获取 域名 A ---
        logger.info(f"步骤 1: (Requests) 正在从 API [{api_url_for_this_bot}] 获取 域名 A...")
        response_api = requests.get(api_url_for_this_bot, headers=headers, timeout=10)
        response_api.raise_for_status() 

        api_data = response_api.json() 
        
        if api_data.get("code") != 0 or "data" not in api_data or not api_data["data"]:
            logger.error(f"API 返回了错误或无效的数据: {api_data}")
            await update.message.reply_text("❌ 链接获取失败：API 未返回有效链接。")
            return

        domain_a = api_data["data"].strip() 

        if not domain_a.startswith(('http://', 'https://')):
            domain_a = 'http://' + domain_a
            
        logger.info(f"步骤 1 成功: 获取到 域名 A -> {domain_a}") 

        # --- 步骤 2: [Playwright] 访问 域名 A 获取 域名 B ---
        logger.info(f"步骤 2: (Playwright) 正在启动新页面访问 {domain_a}...")
        
        page = await fastapi_app.state.browser.new_page()
        
        # --- 
        # --- ⬇️ 关键修复：把“耐心”从 25 秒提高到 40 秒 ⬇️ ---
        #
        page.set_default_timeout(40000) # 40 秒超时 (原为 25000)
        #
        # --- ⬆️ 关键修复 ⬆️ ---
        # --- 

        await page.goto(domain_a, wait_until="networkidle") 
        
        domain_b = page.url 
        logger.info(f"步骤 2 成功: 获取到 域名 B -> {domain_b}")

        # --- 步骤 3: 修改 域名 B 的二级域名 (4-7位) ---
        logger.info(f"步骤 3: 正在为 {domain_b} 生成 4-7 位随机二级域名...")
        random_sub = generate_universal_subdomain() # 4-7 位
        final_modified_url = modify_url_subdomain(domain_b, random_sub)
        logger.info(f"步骤 3 成功: 最终 URL -> {final_modified_url}")

        # --- 步骤 4: 发送最终 URL ---
        await update.message.reply_text(f"✅ 您的专属通用链接已生成：\n{final_modified_url}")

    except Exception as e:
        logger.error(f"处理 get_universal_link (Playwright) 时发生错误: {e}")
        # --- ⬇️ 改进：向用户报告超时错误 ⬇️ ---
        if "Timeout" in str(e):
            await update.message.reply_text("❌ 链接获取失败：目标网页加载超时（超过 40 秒）。")
        else:
            await update.message.reply_text(f"❌ 链接获取失败：{type(e).__name__}。")
        # --- ⬆️ 改进 ⬆️ ---
    finally:
        if page:
            await page.close() 
            logger.info("Playwright 页面已关闭。")

# --- 核心处理器 2 (安卓专用链接) ---
async def get_android_specific_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    (需求 2 - 动态模板)
    1. 收到 "安卓专用" 关键字
    2. 查找此 Bot 专属的 APK_URL 模板
    3. 生成 5-9 位随机字符串
    4. 替换模板中的 *
    5. 发送
    """
    bot_token_end = context.application.bot.token[-4:]
    logger.info(f"Bot {bot_token_end} 收到 [安卓专用] 关键字，开始生成 APK 链接...")
    
    # 1. 查找此 Bot 专属的 APK URL 模板
    current_app = context.application
    apk_template = None
    for path, app_instance in BOT_APPLICATIONS.items():
        if app_instance is current_app:
            apk_template = BOT_APK_URLS.get(path) # 从新字典中查找
            break
            
    if not apk_template:
        logger.error(f"Bot (尾号: {bot_token_end}) 无法找到其配置的 BOT_..._APK_URL！")
        await update.message.reply_text("❌ 服务配置错误：未找到此 Bot 的 APK 链接模板。")
        return
        
    try:
        # 2. 生成 5-9 位随机二级域名
        random_sub = generate_android_specific_subdomain()
        
        # 3. 格式化 URL (替换模板中的第一个 *)
        final_url = apk_template.replace("*", random_sub, 1)
        
        # 4. 发送
        await update.message.reply_text(f"✅ 您的专属安卓专用链接已生成：\n{final_url}")
        
    except Exception as e:
        logger.error(f"处理 get_android_specific_link 时发生错误: {e}")
        await update.message.reply_text(f"❌ 处理安卓链接时发生内部错误。")


# --- 4. Bot 启动与停止逻辑 ---
def setup_bot(app_instance: Application, bot_index: int) -> None:
    """配置 Bot 的所有处理器 (Handlers)。"""
    token_end = app_instance.bot.token[-4:]
    logger.info(f"Bot Application 实例 (#{bot_index}, 尾号: {token_end}) 正在配置 Handlers。")

    # (需求 1) 处理器
    app_instance.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex(UNIVERSAL_COMMAND_PATTERN), 
            get_universal_link # 调用 Playwright 函数
        )
    )
    
    # (需求 2) 处理器
    app_instance.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex(ANDROID_SPECIFIC_COMMAND_PATTERN),
            get_android_specific_link # 调用新的安卓函数
        )
    )
    
    async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_html(f"🤖 Bot #{bot_index} (尾号: {token_end}) 已准备就绪。\n- 发送 `链接`、`地址` 等获取通用链接。\n- 发送 `安卓专用` 等获取 APK 链接。")
    
    app_instance.add_handler(CommandHandler("start", start_command))
    

# --- 5. FastAPI 应用实例 ---
app = FastAPI(title="Multi-Bot Playwright Service")

# --- 6. 应用启动/关闭事件 (与之前相同, 100% 正确) ---
@app.on_event("startup")
async def startup_event():
    """在 FastAPI 启动时：1. 初始化 Bot 2. 启动全局 Playwright 浏览器"""
    
    global BOT_APPLICATIONS, BOT_API_URLS, BOT_APK_URLS, PLAYWRIGHT_INSTANCE, BROWSER_INSTANCE
    BOT_APPLICATIONS = {}
    BOT_API_URLS = {}
    BOT_APK_URLS = {} # 初始化新字典

    logger.info("应用启动中... 正在查找 Bot Token 和 专属 API/APK URL。")

    for i in range(1, 10): 
        token_name = f"BOT_TOKEN_{i}"
        api_url_name = f"BOT_{i}_API_URL"
        apk_url_name = f"BOT_{i}_APK_URL"
        
        token_value = os.getenv(token_name)
        
        # 只要有 Token，就加载 Bot
        if token_value:
            logger.info(f"DIAGNOSTIC: 发现 Bot #{i}: Token (尾号: {token_value[-4:]})")
            
            application = Application.builder().token(token_value).build()
            application.bot_data["fastapi_app"] = app
            
            await application.initialize()
            
            setup_bot(application, i)
            
            webhook_path = f"bot{i}_webhook"
            BOT_APPLICATIONS[webhook_path] = application
            
            # 加载 API URL (用于通用链接)
            api_url_value = os.getenv(api_url_name)
            if api_url_value:
                BOT_API_URLS[webhook_path] = api_url_value 
                logger.info(f"Bot #{i} (尾号: {token_value[-4:]}) 已加载 [通用链接 API]: {api_url_value}")
            else:
                 logger.warning(f"DIAGNOSTIC: Bot #{i} 未找到 {api_url_name}。[通用链接] 功能将无法工作。")

            # 加载 APK URL (用于安卓专用链接)
            apk_url_value = os.getenv(apk_url_name)
            if apk_url_value:
                BOT_APK_URLS[webhook_path] = apk_url_value
                logger.info(f"Bot #{i} (尾号: {token_value[-4:]}) 已加载 [安卓专用模板]: {apk_url_value}")
            else:
                logger.warning(f"DIAGNOSTIC: Bot #{i} 未找到 {apk_url_name}。[安卓专用链接] 功能将无法工作。")
                
            logger.info(f"Bot #{i} (尾号: {token_value[-4:]}) 已创建并初始化。监听路径: /{webhook_path}")

    if not BOT_APPLICATIONS:
        logger.error("❌ 未找到任何有效的 Bot Token。")
    else:
        logger.info(f"✅ 成功初始化 {len(BOT_APPLICATIONS)} 个 Bot 实例。")

    logger.info("正在启动全局 Playwright 实例...")
    try:
        PLAYWRIGHT_INSTANCE = await async_playwright().start()
        BROWSER_INSTANCE = await PLAYWRIGHT_INSTANCE.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        app.state.browser = BROWSER_INSTANCE 
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

# --- 7. 动态 Webhook 路由 (与之前相同, 100% 正确) ---
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

# --- 8. 健康检查路由 (与之前相同, 100% 正确) ---
@app.get("/")
async def root():
    browser_status = "未运行"
    if BROWSER_INSTANCE and BROWSER_INSTANCE.is_connected():
        browser_status = f"运行中 (Version: {BROWSER_INSTANCE.version})"

    active_bots_info = {}
    for path, app in BOT_APPLICATIONS.items():
        active_bots_info[path] = {
            "token_end": app.bot.token[-4:],
            "api_url_universal": BOT_API_URLS.get(path, "未设置!"),
            "api_url_android_apk": BOT_APK_URLS.get(path, "未设置!")
        }
    status = {
        "status": "OK",
        "message": "Telegram Multi-Bot (Playwright JS) service is running.",
        "browser_status": browser_status,
        "active_bots_count": len(BOT_APPLICATIONS),
        "active_bots_info": active_bots_info
    }
    return status
