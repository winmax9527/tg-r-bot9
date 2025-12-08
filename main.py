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
import feedparser
from simpleeval import simple_eval
from fastapi import FastAPI, Request, Response
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import BadRequest
from playwright.async_api import async_playwright, Playwright, Browser, TimeoutError as PlaywrightTimeoutError

# ==============================================================================
# 1. 日志配置 (保留详细记录)
# ==============================================================================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger("BotLogic")

# 只屏蔽底层噪音，保留业务日志
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

# ==============================================================================
# 2. 全局变量
# ==============================================================================
BOT_APPLICATIONS: Dict[str, Application] = {}
BOT_API_URLS: Dict[str, str] = {}
BOT_APK_URLS: Dict[str, str] = {}
BOT_SCHEDULES: Dict[str, Dict[str, Any]] = {} 
BOT_ALLOWED_CHATS: Dict[str, List[str]] = {} 
PLAYWRIGHT_INSTANCE: Playwright | None = None
BROWSER_INSTANCE: Browser | None = None
GLOBAL_HTTP_CLIENT: httpx.AsyncClient | None = None
BROWSER_LOCK = asyncio.Semaphore(1)

# RSS 源 (日报用)
DEFAULT_RSS_FEEDS = [
    "https://36kr.com/feed",
    "https://www.cnbeta.com.tw/backend.php",
    "https://www.ithome.com/rss/",
    "https://sspai.com/feed",
    "http://www.zhihudaily.com/#/index",
]

# 正则 (你原本的)
UNIVERSAL_COMMAND_PATTERN = r"^(地址|安装地址|安装链接|下载地址|下载链接|最新地址|安卓地址|苹果地址|安卓下载地址|苹果下载地址|链接|最新链接|安卓链接|安卓下载链接|最新安卓链接|苹果链接|苹果下载链接|ios链接|最新苹果链接)$"
ANDROID_SPECIFIC_COMMAND_PATTERN = r"^(提包|安卓专用|安卓专用链接|安卓提包链接|安卓专用地址|安卓提包地址|安卓专用下载|安卓提包)$"
IOS_QUIT_PATTERN = r"^(苹果大退|苹果重启|苹果大退重启|苹果黑屏|苹果重开)$"
ANDROID_QUIT_PATTERN = r"^(安卓大退|安卓重启|安卓大退重启|安卓黑屏|安卓重开|大退|重开|闪退|卡了|黑屏)$"
ANDROID_BROWSER_PATTERN = r"^(安卓浏览器手机版|安卓桌面版|安卓浏览器|浏览器设置)$"
IOS_BROWSER_PATTERN = r"^(苹果浏览器手机版|苹果浏览器|苹果桌面版)$"
ANDROID_TAB_LIMIT_PATTERN = r"^(安卓窗口上限|窗口上限|标签上限)$"
IOS_TAB_LIMIT_PATTERN = r"^(苹果窗口上限|苹果标签上限)$"
# 新增正则 (给计算器用)
IPO_COMMAND_PATTERN = r"^(新股|新股申购|新股上市|近期新股|申购|上市)$"
DIGEST_COMMAND_PATTERN = r"^(日报|简报|新闻|每日简报|科技新闻)$"

GLOBAL_IMAGE_MAP: Dict[str, str] = {} 
GLOBAL_IMAGE_PATTERN: str = "" 
GLOBAL_VIDEO_MAP: Dict[str, str] = {} 
GLOBAL_VIDEO_PATTERN: str = "" 

# ==============================================================================
# 3. 辅助函数
# ==============================================================================

def is_chat_allowed(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    current_app = context.application
    allowed_list: List[str] = []
    for path, app_instance in BOT_APPLICATIONS.items():
        if app_instance is current_app:
            allowed_list = BOT_ALLOWED_CHATS.get(path, [])
            break
    if not allowed_list: return True 
    chat_id_str = str(chat_id)
    possible_ids = {chat_id_str}
    if chat_id_str.startswith("-100"): possible_ids.add(f"-{chat_id_str[4:]}")
    elif chat_id_str.startswith("-"): possible_ids.add(f"-100{chat_id_str[1:]}")
    for cid in possible_ids:
        if cid in allowed_list: return True
    return False

# 🔥 日志记录器
def log_user_action(update: Update, bot_name: str, action: str):
    if not update.effective_user: return
    user = update.effective_user
    chat = update.effective_chat
    u_name = user.username or user.first_name or user.id
    c_id = chat.id if chat else "Unknown"
    logger.info(f"[{bot_name}] User:{u_name} Chat:{c_id} -> Cmd: {action}")

async def safe_reply(update: Update, text: str, parse_mode=None):
    try:
        if parse_mode: await update.message.reply_text(text, parse_mode=parse_mode)
        else: await update.message.reply_text(text)
    except BadRequest as e:
        logger.warning(f"Reply failed: {e}")
    except Exception: pass

# ==============================================================================
# 4. 🔥 原汁原味的工兵逻辑 (保留所有文字和跳转)
# ==============================================================================

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
        return parsed._replace(netloc=new_netloc).geturl()
    except Exception: return url_str

# 核心链接逻辑 (API -> Playwright -> B域名)
async def get_universal_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_chat_allowed(context, update.message.chat_id): return
    bot_id = context.bot_data.get("bot_index", "?")
    log_user_action(update, f"Worker-{bot_id}", "Get Link")

    current_app = context.application
    api_url = None
    for path, app_instance in BOT_APPLICATIONS.items():
        if app_instance is current_app:
            api_url = BOT_API_URLS.get(path)
            break
    if not api_url:
        await safe_reply(update, "❌ 配置错误：未找到 API。")
        return

    try: await safe_reply(update, "正在为您获取专属通用下载链接，请稍候 ...")
    except: pass
    
    user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    page = None 
    async with BROWSER_LOCK:
        try:
            if GLOBAL_HTTP_CLIENT is None: raise RuntimeError("HTTP Client error")
            resp = await GLOBAL_HTTP_CLIENT.get(api_url, headers={'User-Agent': user_agent})
            api_data = resp.json()
            domain_a = api_data.get("data", "").strip()
            if not domain_a.startswith(('http://', 'https://')): domain_a = 'http://' + domain_a
            
            # 使用浏览器跳转
            context_p = await context.bot_data["fastapi_app"].state.browser.new_context(user_agent=user_agent)
            page = await context_p.new_page()
            await page.goto(domain_a, wait_until="domcontentloaded", timeout=25000)
            try: await page.wait_for_timeout(1500)
            except: pass
            
            final_url_b = page.url
            if "chrome-error" in final_url_b: raise Exception("Chrome Error")

            rand_sub = generate_universal_subdomain()
            final_url = modify_url_subdomain(final_url_b, rand_sub)
            
            msg = f"✅ <b>您的专属通用下载链接已生成！</b>\n👇 <b>点击下方链接即可复制：</b>\n<code>{final_url}</code>\n💡 <i>请务必在手机自带浏览器中打开</i>"
            await safe_reply(update, msg, parse_mode='HTML')
            await context_p.close()
        except Exception as e:
            logger.error(f"Link Error: {e}")
            await safe_reply(update, "❌ 获取失败，请重试。")
        finally:
            if page: await page.close()

async def get_android_specific_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_chat_allowed(context, update.message.chat_id): return
    current_app = context.application
    apk_template = None
    for path, app_instance in BOT_APPLICATIONS.items():
        if app_instance is current_app:
            apk_template = BOT_APK_URLS.get(path)
            break
    if not apk_template: return
    try:
        random_sub = generate_android_specific_subdomain()
        final_url = apk_template.replace("*", random_sub, 1)
        msg = f"✅ <b>您的专属安卓专用链接已生成！</b>\n👇 <b>点击下方链接即可复制：</b>\n<code>{final_url}</code>\n💡 <i>请务必在手机自带浏览器中打开</i>"
        await safe_reply(update, msg, parse_mode='HTML')
    except Exception: pass

# 静态回复 (Wrapper)
async def send_static_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, html_msg: str):
    if not update.message or not is_chat_allowed(context, update.message.chat_id): return
    await safe_reply(update, html_msg, parse_mode='HTML')

async def send_global_media(update: Update, context: ContextTypes.DEFAULT_TYPE, is_video=False):
    if not update.message: return
    key = update.message.text
    url = GLOBAL_VIDEO_MAP.get(key) if is_video else GLOBAL_IMAGE_MAP.get(key)
    if url:
        try:
            if is_video: await update.message.reply_video(video=url)
            else: await update.message.reply_photo(photo=url)
        except: pass

# ==============================================================================
# 5. 🔥 计算器/AI 逻辑 (独立封装，互不干扰)
# ==============================================================================

async def call_gemini(prompt: str, model: str = "gemini-2.5-flash") -> str:
    MY_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_GEMINI_KEY")
    if not MY_KEY: return "❌ 未配置 API Key"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={MY_KEY}"
    payload = { "contents": [{ "parts": [{"text": prompt}] }] }
    try:
        resp = await GLOBAL_HTTP_CLIENT.post(url, json=payload, timeout=90.0)
        if resp.status_code == 200:
            return resp.json()['candidates'][0]['content']['parts'][0]['text']
        return f"AI Error: {resp.status_code}"
    except Exception as e: return f"Net Error: {e}"

# 新股查询 (截图版)
async def get_ipo_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_user_action(update, "CalcBot", "IPO Query")
    if not BROWSER_INSTANCE: return await safe_reply(update, "❌ 浏览器未就绪")
    await safe_reply(update, "🔍 正在检索新股数据...")
    async with BROWSER_LOCK:
        try:
            page = await BROWSER_INSTANCE.new_page()
            await page.goto("https://vip.stock.finance.sina.com.cn/corp/go.php/vRPD_NewStockIssue/page/1.phtml", timeout=30000)
            await page.wait_for_load_state("domcontentloaded")
            img = await page.screenshot(full_page=False)
            await update.message.reply_photo(photo=img, caption="📸 新浪财经实时数据")
            await page.close()
        except Exception as e:
            await safe_reply(update, f"查询失败: {e}")

# 日报
async def get_daily_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_user_action(update, "CalcBot", "Daily Digest")
    await safe_reply(update, "☕️ 正在搜集新闻并生成简报...")
    entries = []
    try:
        tasks = [asyncio.to_thread(feedparser.parse, u) for u in DEFAULT_RSS_FEEDS]
        results = await asyncio.gather(*tasks)
        for f in results: entries.extend(f.entries[:5])
    except: pass
    
    if not entries: return await safe_reply(update, "📭 无新闻更新")
    
    content = "\n".join([f"- {e.title} ({e.link})" for e in entries])
    prompt = f"你是一名资深科技主编。请从以下素材中筛选10条重要新闻，分类为AI、数码、商业、深度。用中文一句话解读。素材：\n{content}"
    res = await call_gemini(prompt)
    await safe_reply(update, f"📅 <b>今日科技内参</b>\n\n{res}", parse_mode='HTML')

# 纯文本计算
def do_calc(text):
    try:
        clean = re.sub(r'[^\d\+\-\*\/\(\)\.\%]', '', text)
        if not clean: return None
        res = simple_eval(clean)
        return int(res) if float(res).is_integer() else res
    except: return None

# ==============================================================================
# 6. Bot Setup (两个配置函数)
# ==============================================================================

# 配置工兵 (只做原来的事，文案一个字不少)
def setup_worker_bot(app_instance: Application, bot_index: int) -> None:
    token_end = app_instance.bot.token[-4:]
    
    async def start(u, c):
        await safe_reply(u, f"🤖 工兵 #{bot_index} ({token_end}) 就绪。")

    app_instance.add_handler(CommandHandler("start", start))
    
    # 链接
    app_instance.add_handler(MessageHandler(filters.Regex(UNIVERSAL_COMMAND_PATTERN), get_universal_link))
    app_instance.add_handler(MessageHandler(filters.Regex(ANDROID_SPECIFIC_COMMAND_PATTERN), get_android_specific_link))

    # 教程 (全文本恢复)
    t_ios_quit = "📱 <b>苹果手机APP大退步骤</b>\n\n1. 上滑停留调出后台。\n2. 上滑关闭App卡片。\n3. 重新点击图标打开。"
    t_and_quit = "🤖 <b>安卓手机APP大退步骤</b>\n\n1. 上滑或点击多任务键进入后台。\n2. 上滑关闭App卡片。\n3. 重新打开App。"
    t_and_browser = "🤖 <b>安卓浏览器设置手机版</b>\n\n1. 打开浏览器菜单(≡或⋮)。\n2. 找到“桌面版”或“电脑模式”。\n3. <b>取消勾选</b>它。"
    t_ios_browser = "📱 <b>苹果浏览器设置手机版</b>\n\n1. 点击地址栏左侧(大小/AA)。\n2. 选择“请求移动网站”。\n(如果显示“请求桌面网站”则无需操作)"
    t_and_tab = "🤖 <b>安卓窗口上限解决</b>\n\n1. 点击浏览器标签页图标(数字框)。\n2. 选择“关闭所有标签页”或手动关闭旧标签。"
    t_ios_tab = "📱 <b>苹果窗口上限解决</b>\n\n1. 长按右下角标签图标。\n2. 选择“关闭所有标签页”。"

    app_instance.add_handler(MessageHandler(filters.Regex(IOS_QUIT_PATTERN), lambda u,c: send_static_reply(u,c,t_ios_quit)))
    app_instance.add_handler(MessageHandler(filters.Regex(ANDROID_QUIT_PATTERN), lambda u,c: send_static_reply(u,c,t_and_quit)))
    app_instance.add_handler(MessageHandler(filters.Regex(ANDROID_BROWSER_PATTERN), lambda u,c: send_static_reply(u,c,t_and_browser)))
    app_instance.add_handler(MessageHandler(filters.Regex(IOS_BROWSER_PATTERN), lambda u,c: send_static_reply(u,c,t_ios_browser)))
    app_instance.add_handler(MessageHandler(filters.Regex(ANDROID_TAB_LIMIT_PATTERN), lambda u,c: send_static_reply(u,c,t_and_tab)))
    app_instance.add_handler(MessageHandler(filters.Regex(IOS_TAB_LIMIT_PATTERN), lambda u,c: send_static_reply(u,c,t_ios_tab)))

    if GLOBAL_IMAGE_PATTERN:
        app_instance.add_handler(MessageHandler(filters.Regex(GLOBAL_IMAGE_PATTERN), lambda u,c: send_global_media(u,c,False)))
    if GLOBAL_VIDEO_PATTERN:
        app_instance.add_handler(MessageHandler(filters.Regex(GLOBAL_VIDEO_PATTERN), lambda u,c: send_global_media(u,c,True)))

# 配置计算器 (接管高级功能)
def setup_calculator_bot(app_instance: Application) -> None:
    async def start(u, c):
        await safe_reply(u, "👋 我是智能计算器。\n功能：计算、新股、日报、/book、/quote")
    
    app_instance.add_handler(CommandHandler("start", start))
    
    # AI 功能
    async def book(u,c): 
        q = " ".join(c.args); 
        if not q: return await safe_reply(u, "请加关键词"); 
        await safe_reply(u, await call_gemini(f"推荐3本关于{q}的书，带理由和摘抄", parse_mode='Markdown'))
    
    async def quote(u,c): 
        q = " ".join(c.args); 
        t = f"《{q}》" if q else "名著"; 
        await safe_reply(u, await call_gemini(f"从{t}找一段经典摘抄并赏析", parse_mode='Markdown'))
    
    async def deep(u,c): 
        q = " ".join(c.args); 
        if not q: return await safe_reply(u, "请加话题"); 
        await safe_reply(u, await call_gemini(f"深度解析话题：{q}", parse_mode='Markdown'))

    app_instance.add_handler(CommandHandler("book", book))
    app_instance.add_handler(CommandHandler("quote", quote))
    app_instance.add_handler(CommandHandler("deep", deep))

    # 新股 & 日报 (优先匹配)
    app_instance.add_handler(MessageHandler(filters.Regex(IPO_COMMAND_PATTERN), get_ipo_info))
    app_instance.add_handler(MessageHandler(filters.Regex(DIGEST_COMMAND_PATTERN), get_daily_digest))

    # 计算器 (兜底)
    async def calc(u,c):
        if not u.message.text or u.message.text.startswith("/"): return
        res = do_calc(u.message.text)
        if res: 
            log_user_action(u, "CalcBot", f"Calc: {u.message.text}")
            await safe_reply(u, f"🔢 {res}")

    app_instance.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, calc))

# ==============================================================================
# 7. 启动
# ==============================================================================
app = FastAPI()

@app.on_event("startup")
async def startup_event():
    global GLOBAL_HTTP_CLIENT, PLAYWRIGHT_INSTANCE, BROWSER_INSTANCE
    GLOBAL_HTTP_CLIENT = httpx.AsyncClient(timeout=30.0, verify=False)
    try:
        PLAYWRIGHT_INSTANCE = await async_playwright().start()
        BROWSER_INSTANCE = await PLAYWRIGHT_INSTANCE.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        app.state.browser = BROWSER_INSTANCE
        logger.info("✅ System Ready")
    except Exception as e: logger.error(e)

    # 加载全局媒体资源
    global GLOBAL_IMAGE_MAP, GLOBAL_IMAGE_PATTERN, GLOBAL_VIDEO_MAP, GLOBAL_VIDEO_PATTERN
    for i in range(1, 11):
        k, v = os.getenv(f"IMAGE_{i}_KEYS"), os.getenv(f"IMAGE_{i}_URL")
        if k and v: 
            for key in k.split(','): GLOBAL_IMAGE_MAP[key.strip()] = v
        k, v = os.getenv(f"VIDEO_{i}_KEYS"), os.getenv(f"VIDEO_{i}_URL")
        if k and v: 
            for key in k.split(','): GLOBAL_VIDEO_MAP[key.strip()] = v
    if GLOBAL_IMAGE_MAP: GLOBAL_IMAGE_PATTERN = r"^(" + "|".join([re.escape(k) for k in GLOBAL_IMAGE_MAP.keys()]) + r")$"
    if GLOBAL_VIDEO_MAP: GLOBAL_VIDEO_PATTERN = r"^(" + "|".join([re.escape(k) for k in GLOBAL_VIDEO_MAP.keys()]) + r")$"

    # 启动工兵 1-10
    for i in range(1, 10):
        token = os.getenv(f"BOT_TOKEN_{i}")
        if token:
            bot = Application.builder().token(token).build()
            bot.bot_data["fastapi_app"] = app
            bot.bot_data["bot_index"] = i
            await bot.initialize()
            setup_worker_bot(bot, i) # 工兵配置
            
            path = f"bot{i}_webhook"
            BOT_APPLICATIONS[path] = bot
            if url := os.getenv(f"BOT_{i}_API_URL"): BOT_API_URLS[path] = url
            if url := os.getenv(f"BOT_{i}_APK_URL"): BOT_APK_URLS[path] = url
            if al := os.getenv(f"BOT_{i}_ALLOWED_CHAT_IDS"): BOT_ALLOWED_CHATS[path] = [c.strip() for c in al.split(',')]
            
            await bot.start()
            await bot.updater.start_polling()
            logger.info(f"Worker {i} Started")

    # 启动计算器
    calc_token = os.getenv("CALC_BOT_TOKEN")
    if calc_token:
        c_bot = Application.builder().token(calc_token).build()
        await c_bot.initialize()
        setup_calculator_bot(c_bot) # 计算器配置
        await c_bot.start()
        await c_bot.updater.start_polling()
        BOT_APPLICATIONS["calc"] = c_bot
        logger.info("Calc Bot Started")

@app.on_event("shutdown")
async def shutdown():
    if GLOBAL_HTTP_CLIENT: await GLOBAL_HTTP_CLIENT.aclose()
    if BROWSER_INSTANCE: await BROWSER_INSTANCE.close()
    if PLAYWRIGHT_INSTANCE: await PLAYWRIGHT_INSTANCE.stop()
    for b in BOT_APPLICATIONS.values(): await b.stop(); await b.shutdown()

@app.get("/")
async def root(): return {"status": "OK", "bots": len(BOT_APPLICATIONS)}
