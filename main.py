import os
import logging
import asyncio
import re
import random
import string
import datetime
from urllib.parse import urlparse
from typing import List, Dict, Any

# 🔥 核心依赖
import httpx 
from fastapi import FastAPI, Request, Response
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import BadRequest  # 引入异常类型

# 引入 Playwright
from playwright.async_api import async_playwright, Playwright, Browser, TimeoutError as PlaywrightTimeoutError

# --- 1. 配置日志记录 ---
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- 2. 全局状态 ---
BOT_APPLICATIONS: Dict[str, Application] = {}
BOT_API_URLS: Dict[str, str] = {}
BOT_APK_URLS: Dict[str, str] = {}
BOT_SCHEDULES: Dict[str, Dict[str, Any]] = {} 
BOT_ALLOWED_CHATS: Dict[str, List[str]] = {} 
PLAYWRIGHT_INSTANCE: Playwright | None = None
BROWSER_INSTANCE: Browser | None = None

# 全局 HTTP 客户端
GLOBAL_HTTP_CLIENT: httpx.AsyncClient | None = None

# 全局图片/视频
GLOBAL_IMAGE_MAP: Dict[str, str] = {} 
GLOBAL_IMAGE_PATTERN: str = "" 
GLOBAL_VIDEO_MAP: Dict[str, str] = {} 
GLOBAL_VIDEO_PATTERN: str = "" 

# --- 3. 核心正则 ---
UNIVERSAL_COMMAND_PATTERN = r"^(地址|安装地址|安装链接|下载地址|下载链接|最新地址|安卓地址|苹果地址|安卓下载地址|苹果下载地址|链接|最新链接|安卓链接|安卓下载链接|最新安卓链接|苹果链接|苹果下载链接|ios链接|最新苹果链接)$"
ANDROID_SPECIFIC_COMMAND_PATTERN = r"^(提包|安卓专用|安卓专用链接|安卓提包链接|安卓专用地址|安卓提包地址|安卓专用下载|安卓提包)$"
IOS_QUIT_PATTERN = r"^(苹果大退|苹果重启|苹果大退重启|苹果黑屏|苹果重开)$"
ANDROID_QUIT_PATTERN = r"^(安卓大退|安卓重启|安卓大退重启|安卓黑屏|安卓重开|大退|重开|闪退|卡了|黑屏)$"
ANDROID_BROWSER_PATTERN = r"^(安卓浏览器手机版|安卓桌面版|安卓浏览器|浏览器设置)$"
IOS_BROWSER_PATTERN = r"^(苹果浏览器手机版|苹果浏览器|苹果桌面版)$"
ANDROID_TAB_LIMIT_PATTERN = r"^(安卓窗口上限|窗口上限|标签上限)$"
IOS_TAB_LIMIT_PATTERN = r"^(苹果窗口上限|苹果标签上限)$"


# --- 辅助函数 ---

def is_chat_allowed(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    """智能安全检查"""
    current_app = context.application
    allowed_list: List[str] = []
    
    for path, app_instance in BOT_APPLICATIONS.items():
        if app_instance is current_app:
            allowed_list = BOT_ALLOWED_CHATS.get(path, [])
            break
            
    chat_id_str = str(chat_id)
    possible_ids_to_check = {chat_id_str} 
    if chat_id_str.startswith("-100"):
        short_id = f"-{chat_id_str[4:]}"
        possible_ids_to_check.add(short_id)
    elif chat_id_str.startswith("-"):
        long_id = f"-100{chat_id_str[1:]}"
        possible_ids_to_check.add(long_id)

    for check_id in possible_ids_to_check:
        if check_id in allowed_list:
            return True 

    # 降低日志级别，防止刷屏，或者保留 warning
    logger.warning(f"Bot {current_app.bot.token[-4:]} 拒绝了未授权 Chat ID: {chat_id_str}")
    return False

def generate_universal_subdomain(min_len: int = 4, max_len: int = 7) -> str:
    length = random.randint(min_len, max_len)
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))

def generate_android_specific_subdomain(min_len: int = 5, max_len: int = 9) -> str:
    length = random.randint(min_len, max_len)
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))

def modify_url_subdomain(url_str: str, new_sub: str) -> str:
    try:
        parsed = urlparse(url_str)
        domain_parts = parsed.netloc.split('.')
        if len(domain_parts) < 2: return url_str
        domain_parts[0] = new_sub
        new_netloc = '.'.join(domain_parts)
        new_parsed = parsed._replace(netloc=new_netloc)
        return new_parsed.geturl()
    except Exception:
        return url_str

# 🔥 新增：安全回复函数 (防崩核心)
async def safe_reply(update: Update, text: str, parse_mode=None):
    """
    尝试回复消息。如果原消息被删 (BadRequest)，则尝试直接发送消息。
    """
    try:
        if parse_mode:
            await update.message.reply_text(text, parse_mode=parse_mode)
        else:
            await update.message.reply_text(text)
    except BadRequest as e:
        if "Message to be replied not found" in str(e):
            # 原消息没了，改用 send_message 直接发到群里，不引用原消息
            try:
                await update.message.chat.send_message(text, parse_mode=parse_mode)
            except Exception as e2:
                logger.error(f"无法发送备用消息: {e2}")
        else:
            logger.error(f"回复时发生其他 BadRequest: {e}")
    except Exception as e:
        logger.error(f"回复未知错误: {e}")


# --- 核心处理器 1 (Playwright - 通用链接) ---
async def get_universal_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_chat_allowed(context, update.message.chat_id):
        return

    bot_token_end = context.application.bot.token[-4:]
    logger.info(f"Bot {bot_token_end} 收到 [通用链接]...")

    fastapi_app = context.bot_data.get("fastapi_app")
    if not fastapi_app or not hasattr(fastapi_app.state, 'browser'):
        # 使用安全回复
        await safe_reply(update, "❌ 服务内部错误：浏览器未启动。")
        return

    current_app = context.application
    api_url = None
    for path, app_instance in BOT_APPLICATIONS.items():
        if app_instance is current_app:
            api_url = BOT_API_URLS.get(path)
            break
            
    if not api_url:
        await safe_reply(update, "❌ 配置错误：未找到此 Bot 的 API 地址。")
        return

    # 尝试发送“请稍候”提示
    try:
        await safe_reply(update, "正在为您获取专属通用下载链接，请稍候 ...")
    except Exception:
        pass

    user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    browser_context = None 
    page = None 
    
    try:
        # --- 步骤 1: [httpx] 使用全局 Client ---
        logger.info(f"步骤 1: (Async) 正在访问 API...")
        
        if GLOBAL_HTTP_CLIENT is None:
             raise RuntimeError("Global HTTP Client not initialized")

        resp = await GLOBAL_HTTP_CLIENT.get(api_url, headers={'User-Agent': user_agent})
        resp.raise_for_status()
        api_data = resp.json()

        if api_data.get("code") != 0 or "data" not in api_data:
            logger.error(f"API 数据无效: {api_data}")
            await safe_reply(update, "❌ API 未返回有效链接。")
            return

        domain_a = api_data["data"].strip()
        if not domain_a.startswith(('http://', 'https://')):
            domain_a = 'http://' + domain_a
            
        logger.info(f"步骤 1 成功: A -> {domain_a}") 

        # --- 步骤 2: [Playwright] 上下文隔离 ---
        logger.info(f"步骤 2: Playwright 访问...")
        
        browser_context = await fastapi_app.state.browser.new_context(
            user_agent=user_agent,
            viewport={'width': 1280, 'height': 800}
        )
        
        page = await browser_context.new_page()
        page.set_default_timeout(35000)

        try:
            await page.goto(domain_a, wait_until="domcontentloaded")
            try:
                await page.wait_for_timeout(1500)
            except:
                pass

        except PlaywrightTimeoutError:
            logger.error(f"Playwright 导航超时")
            await safe_reply(update, "❌ 源站响应太慢，请重试。")
            return 
        except Exception as nav_err:
            logger.error(f"Playwright 导航错误: {nav_err}")
            await safe_reply(update, "❌ 无法连接到源站。")
            return 
        
        domain_b = page.url 
        
        if "chrome-error://" in domain_b or "chromewebdata" in domain_b:
            logger.error(f"Chrome 错误页: {domain_b}")
            await safe_reply(update, "⚠️ 线路维护中，请稍后再试。")
            return

        logger.info(f"步骤 2 成功: B -> {domain_b}")

        # --- 步骤 3: 修改域名 ---
        random_sub = generate_universal_subdomain()
        final_url = modify_url_subdomain(domain_b, random_sub)

        msg = (
            "✅ <b>您的专属通用下载链接已生成！</b>\n"
            "👇 <b>点击下方链接即可复制：</b>\n"
            f"<code>{final_url}</code>" 
            "\n💡 <i>请务必在手机自带浏览器中打开</i>"
        )
        # 🔥 使用 safe_reply 发送结果
        await safe_reply(update, msg, parse_mode='HTML')

    except httpx.TimeoutException:
        logger.error(f"❌ API 请求超时")
        await safe_reply(update, "❌ 获取链接超时，对方服务器响应太慢，请重试。")
    except Exception as e:
        logger.error(f"系统错误 ({type(e).__name__}): {e}")
        await safe_reply(update, "❌ 系统繁忙，请重试。")
        
    finally:
        if page:
            try: await page.close()
            except: pass
        if browser_context:
            try: await browser_context.close()
            except: pass


# --- 核心处理器 2 (安卓专用) ---
async def get_android_specific_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_chat_allowed(context, update.message.chat_id): return
    
    current_app = context.application
    apk_template = None
    for path, app_instance in BOT_APPLICATIONS.items():
        if app_instance is current_app:
            apk_template = BOT_APK_URLS.get(path)
            break
            
    if not apk_template:
        await safe_reply(update, "❌ 配置错误：未找到 APK 模板。")
        return
        
    try:
        random_sub = generate_android_specific_subdomain()
        final_url = apk_template.replace("*", random_sub, 1)
        msg = (
            "✅ <b>您的专属安卓专用链接已生成！</b>\n"
            "👇 <b>点击下方链接即可复制：</b>\n"
            f"<code>{final_url}</code>"
            "\n💡 <i>请务必在手机自带浏览器中打开</i>"
        )
        await safe_reply(update, msg, parse_mode='HTML')
    except Exception as e:
        logger.error(f"APK 生成错误: {e}")

# --- 其他静态回复处理器 ---
async def send_static_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, log_msg: str, html_msg: str):
    if not update.message or not is_chat_allowed(context, update.message.chat_id): return
    logger.info(f"Bot {context.application.bot.token[-4:]} {log_msg}")
    try: 
        # 🔥 使用 safe_reply
        await safe_reply(update, html_msg, parse_mode='HTML')
    except Exception as e: 
        logger.error(f"发送消息失败: {e}")

async def send_ios_quit_guide(u, c): await send_static_reply(u, c, "发送苹果大退", "📱 <b>苹果手机APP大退步骤</b>\n\n1. 上滑停留调出后台。\n2. 上滑关闭App卡片。\n3. 重新点击图标打开。")
async def send_android_quit_guide(u, c): await send_static_reply(u, c, "发送安卓大退", "🤖 <b>安卓手机APP大退步骤</b>\n\n1. 上滑或点击多任务键进入后台。\n2. 上滑关闭App卡片。\n3. 重新打开App。")
async def send_android_browser_guide(u, c): await send_static_reply(u, c, "发送安卓浏览器", "🤖 <b>安卓浏览器设置手机版</b>\n\n1. 打开浏览器菜单(≡或⋮)。\n2. 找到“桌面版”或“电脑模式”。\n3. <b>取消勾选</b>它。")
async def send_ios_browser_guide(u, c): await send_static_reply(u, c, "发送苹果浏览器", "📱 <b>苹果浏览器设置手机版</b>\n\n1. 点击地址栏左侧(大小/AA)。\n2. 选择“请求移动网站”。\n(如果显示“请求桌面网站”则无需操作)")
async def send_android_tab_limit_guide(u, c): await send_static_reply(u, c, "发送安卓窗口上限", "🤖 <b>安卓窗口上限解决</b>\n\n1. 点击浏览器标签页图标(数字框)。\n2. 选择“关闭所有标签页”或手动关闭旧标签。")
async def send_ios_tab_limit_guide(u, c): await send_static_reply(u, c, "发送苹果窗口上限", "📱 <b>苹果窗口上限解决</b>\n\n1. 长按右下角标签图标。\n2. 选择“关闭所有标签页”。")

# --- 图片/视频处理器 (不做复杂处理，简单 try-except) ---
async def send_global_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_chat_allowed(context, update.message.chat_id): return
    keyword = update.message.text
    url = GLOBAL_IMAGE_MAP.get(keyword)
    if url: 
        try: await update.message.reply_photo(photo=url)
        except Exception: pass

async def send_global_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_chat_allowed(context, update.message.chat_id): return
    keyword = update.message.text
    url = GLOBAL_VIDEO_MAP.get(keyword)
    if url: 
        try: await update.message.reply_video(video=url)
        except Exception: pass


# --- Setup Bot ---
def setup_bot(app_instance: Application, bot_index: int) -> None:
    token_end = app_instance.bot.token[-4:]
    
    app_instance.add_handler(MessageHandler(filters.TEXT & filters.Regex(UNIVERSAL_COMMAND_PATTERN), get_universal_link))
    app_instance.add_handler(MessageHandler(filters.TEXT & filters.Regex(ANDROID_SPECIFIC_COMMAND_PATTERN), get_android_specific_link))
    app_instance.add_handler(MessageHandler(filters.TEXT & filters.Regex(IOS_QUIT_PATTERN), send_ios_quit_guide))
    app_instance.add_handler(MessageHandler(filters.TEXT & filters.Regex(ANDROID_QUIT_PATTERN), send_android_quit_guide))
    app_instance.add_handler(MessageHandler(filters.TEXT & filters.Regex(ANDROID_BROWSER_PATTERN), send_android_browser_guide))
    app_instance.add_handler(MessageHandler(filters.TEXT & filters.Regex(IOS_BROWSER_PATTERN), send_ios_browser_guide))
    app_instance.add_handler(MessageHandler(filters.TEXT & filters.Regex(ANDROID_TAB_LIMIT_PATTERN), send_android_tab_limit_guide))
    app_instance.add_handler(MessageHandler(filters.TEXT & filters.Regex(IOS_TAB_LIMIT_PATTERN), send_ios_tab_limit_guide))

    if GLOBAL_IMAGE_PATTERN:
        app_instance.add_handler(MessageHandler(filters.TEXT & filters.Regex(GLOBAL_IMAGE_PATTERN), send_global_image))
    if GLOBAL_VIDEO_PATTERN:
        app_instance.add_handler(MessageHandler(filters.TEXT & filters.Regex(GLOBAL_VIDEO_PATTERN), send_global_video))
    
    async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not is_chat_allowed(context, update.message.chat_id): return
        msg = f"🤖 Bot #{bot_index} ({token_end}) 就绪。\n发送 链接、安卓专用、大退 等指令使用。"
        if GLOBAL_IMAGE_MAP:
            msg += "\n\n快捷图片: " + ", ".join(list(GLOBAL_IMAGE_MAP.keys())[:3])
        await safe_reply(update, msg, parse_mode='HTML')
    
    app_instance.add_handler(CommandHandler("start", start_command))

# --- FastAPI & Startup (保持不变) ---
app = FastAPI(title="Multi-Bot Service")

async def background_scheduler():
    logger.info("后台调度器启动...")
    await asyncio.sleep(10)
    while True:
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            curr_hm = now.strftime("%H:%M")
            for path, sched in BOT_SCHEDULES.items():
                if curr_hm in sched["times"]:
                    last = sched.get("last_sent")
                    if last is None or (now - last).total_seconds() > 3500:
                        app_inst = BOT_APPLICATIONS.get(path)
                        if app_inst:
                            msg = sched["message"].replace("<br>", "\n").replace("<br/>", "\n")
                            for cid in sched["chat_ids"]:
                                try: await app_inst.bot.send_message(chat_id=cid, text=msg, parse_mode='HTML')
                                except: pass
                            sched["last_sent"] = now
        except Exception as e:
            logger.error(f"调度器单次循环错误 (不影响服务): {e}")
            
        await asyncio.sleep(60)

@app.on_event("startup")
async def startup_event():
    global BOT_APPLICATIONS, BOT_API_URLS, BOT_APK_URLS, BOT_SCHEDULES, BOT_ALLOWED_CHATS, PLAYWRIGHT_INSTANCE, BROWSER_INSTANCE
    global GLOBAL_IMAGE_MAP, GLOBAL_IMAGE_PATTERN, GLOBAL_VIDEO_MAP, GLOBAL_VIDEO_PATTERN
    global GLOBAL_HTTP_CLIENT

    GLOBAL_HTTP_CLIENT = httpx.AsyncClient(timeout=30.0, verify=False, limits=httpx.Limits(max_keepalive_connections=20, max_connections=50))

    BOT_APPLICATIONS = {}
    BOT_API_URLS = {}
    BOT_APK_URLS = {}
    BOT_SCHEDULES = {} 
    BOT_ALLOWED_CHATS = {} 
    GLOBAL_IMAGE_MAP = {}
    GLOBAL_VIDEO_MAP = {}

    for i in range(1, 11):
        k, v = os.getenv(f"IMAGE_{i}_KEYS"), os.getenv(f"IMAGE_{i}_URL")
        if k and v: 
            for key in k.split(','): 
                if key.strip(): GLOBAL_IMAGE_MAP[key.strip()] = v
        
        k, v = os.getenv(f"VIDEO_{i}_KEYS"), os.getenv(f"VIDEO_{i}_URL")
        if k and v: 
            for key in k.split(','): 
                if key.strip(): GLOBAL_VIDEO_MAP[key.strip()] = v

    if GLOBAL_IMAGE_MAP:
        GLOBAL_IMAGE_PATTERN = r"^(" + "|".join([re.escape(k) for k in GLOBAL_IMAGE_MAP.keys()]) + r")$"
    if GLOBAL_VIDEO_MAP:
        GLOBAL_VIDEO_PATTERN = r"^(" + "|".join([re.escape(k) for k in GLOBAL_VIDEO_MAP.keys()]) + r")$"

    for i in range(1, 10):
        token = os.getenv(f"BOT_TOKEN_{i}")
        if token:
            application = Application.builder().token(token).build()
            application.bot_data["fastapi_app"] = app
            await application.initialize()
            setup_bot(application, i)
            
            path = f"bot{i}_webhook"
            BOT_APPLICATIONS[path] = application
            
            if url := os.getenv(f"BOT_{i}_API_URL"): BOT_API_URLS[path] = url
            if url := os.getenv(f"BOT_{i}_APK_URL"): BOT_APK_URLS[path] = url
            
            if al := os.getenv(f"BOT_{i}_ALLOWED_CHAT_IDS"):
                BOT_ALLOWED_CHATS[path] = [cid.strip() for cid in al.split(',') if cid.strip()]
            
            s_cids = os.getenv(f"BOT_{i}_SCHEDULE_CHAT_ID")
            s_times = os.getenv(f"BOT_{i}_SCHEDULE_TIMES_UTC")
            s_msg = os.getenv(f"BOT_{i}_SCHEDULE_MESSAGE")
            if s_cids and s_times and s_msg:
                BOT_SCHEDULES[path] = {
                    "chat_ids": [c.strip() for c in s_cids.split(',')],
                    "times": [t.strip() for t in s_times.split(',')],
                    "message": s_msg,
                    "last_sent": None
                }
            logger.info(f"Bot #{i} ({token[-4:]}) 加载完成")

    try:
        PLAYWRIGHT_INSTANCE = await async_playwright().start()
        launch_args = [
            "--no-sandbox", 
            "--disable-setuid-sandbox", 
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-software-rasterizer"
        ]
        BROWSER_INSTANCE = await PLAYWRIGHT_INSTANCE.chromium.launch(
            headless=True, 
            args=launch_args
        )
        app.state.browser = BROWSER_INSTANCE
        logger.info("Playwright 启动成功 (优化模式)")
    except Exception as e:
        logger.error(f"Playwright 启动失败: {e}")

    asyncio.create_task(background_scheduler())

@app.on_event("shutdown")
async def shutdown_event():
    if GLOBAL_HTTP_CLIENT: 
        await GLOBAL_HTTP_CLIENT.aclose()
    if BROWSER_INSTANCE: 
        await BROWSER_INSTANCE.close()
    if PLAYWRIGHT_INSTANCE: 
        await PLAYWRIGHT_INSTANCE.stop()

@app.post("/{webhook_path}")
async def handle_webhook(webhook_path: str, request: Request):
    if webhook_path not in BOT_APPLICATIONS: return Response(status_code=404)
    try:
        update = Update.de_json(await request.json(), BOT_APPLICATIONS[webhook_path].bot)
        await BOT_APPLICATIONS[webhook_path].process_update(update)
        return Response(status_code=200)
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return Response(status_code=500)

@app.get("/")
async def root():
    return {"status": "OK", "bots": len(BOT_APPLICATIONS)}
