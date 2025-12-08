import os
import logging
import asyncio
import re
import random
import string
import datetime
from urllib.parse import urlparse
from typing import List, Dict, Any

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

# 只屏蔽底层网络库的刷屏日志，保留业务日志
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

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

# RSS 源
DEFAULT_RSS_FEEDS = [
    "https://36kr.com/feed",
    "https://www.cnbeta.com.tw/backend.php",
    "https://www.ithome.com/rss/",
    "https://sspai.com/feed",
    "http://www.zhihudaily.com/#/index",
]

# 正则表达式 (严格保留)
UNIVERSAL_COMMAND_PATTERN = r"^(地址|安装地址|安装链接|下载地址|下载链接|最新地址|安卓地址|苹果地址|安卓下载地址|苹果下载地址|链接|最新链接|安卓链接|安卓下载链接|最新安卓链接|苹果链接|苹果下载链接|ios链接|最新苹果链接)$"
ANDROID_SPECIFIC_COMMAND_PATTERN = r"^(提包|安卓专用|安卓专用链接|安卓提包链接|安卓专用地址|安卓提包地址|安卓专用下载|安卓提包)$"
IOS_QUIT_PATTERN = r"^(苹果大退|苹果重启|苹果大退重启|苹果黑屏|苹果重开)$"
ANDROID_QUIT_PATTERN = r"^(安卓大退|安卓重启|安卓大退重启|安卓黑屏|安卓重开|大退|重开|闪退|卡了|黑屏)$"
ANDROID_BROWSER_PATTERN = r"^(安卓浏览器手机版|安卓桌面版|安卓浏览器|浏览器设置)$"
IOS_BROWSER_PATTERN = r"^(苹果浏览器手机版|苹果浏览器|苹果桌面版)$"
ANDROID_TAB_LIMIT_PATTERN = r"^(安卓窗口上限|窗口上限|标签上限)$"
IOS_TAB_LIMIT_PATTERN = r"^(苹果窗口上限|苹果标签上限)$"
IPO_COMMAND_PATTERN = r"^(新股|新股申购|新股上市|近期新股|申购|上市)$"
DIGEST_COMMAND_PATTERN = r"^(日报|简报|新闻|每日简报|科技新闻)$"

GLOBAL_IMAGE_MAP: Dict[str, str] = {} 
GLOBAL_IMAGE_PATTERN: str = "" 
GLOBAL_VIDEO_MAP: Dict[str, str] = {} 
GLOBAL_VIDEO_PATTERN: str = "" 

# ==============================================================================
# 3. 核心工具函数
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

# 🔥 日志记录：谁在干什么
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
    except Exception as e:
        logger.error(f"Reply unknown error: {e}")

async def call_gemini_api(prompt: str, model: str = "gemini-2.5-flash") -> str:
    MY_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_GEMINI_KEY")
    if not MY_KEY: return "❌ 未配置 API Key"
    if not GLOBAL_HTTP_CLIENT: return "❌ HTTP Client 未就绪"
    api_base = "https://generativelanguage.googleapis.com/v1beta"
    url = f"{api_base}/models/{model}:generateContent?key={MY_KEY}"
    payload = { "contents": [{ "parts": [{"text": prompt}] }] }
    try:
        response = await GLOBAL_HTTP_CLIENT.post(url, json=payload, timeout=90.0)
        if response.status_code == 200:
            data = response.json()
            if 'candidates' in data and data['candidates']:
                return data['candidates'][0]['content']['parts'][0]['text']
        return f"❌ AI 响应错误 ({response.status_code})"
    except Exception as e:
        logger.error(f"Gemini API Error: {e}")
        return f"❌ 网络请求异常: {str(e)}"

# ==============================================================================
# 4. 高级功能 (计算器 Bot 独享)
# ==============================================================================

async def handle_book_recommend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_user_action(update, "CalcBot", "Book Recommend")
    user_input = " ".join(context.args)
    if not user_input:
        await safe_reply(update, "📚 **请告诉我您想看什么类型的书？**\n例如：<code>/book 科幻小说</code>", parse_mode='HTML')
        return
    await safe_reply(update, f"🤔 正在为您检索【{user_input}】相关的书籍...")
    prompt = f"你是一位资深图书编辑。用户想找【{user_input}】类型的书。请推荐3本高质量书籍，包含书名、作者、推荐理由和一句经典摘抄。"
    result = await call_gemini_api(prompt)
    await safe_reply(update, result, parse_mode='Markdown')

async def handle_novel_quote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_user_action(update, "CalcBot", "Quote Analysis")
    user_input = " ".join(context.args)
    target = f"《{user_input}》" if user_input else "世界经典文学名著"
    await safe_reply(update, "📜 正在翻阅藏书...")
    prompt = f"请从{target}中挑选一段经典原文摘抄（100-200字），并进行深度赏析（背景、美感、哲理）。"
    result = await call_gemini_api(prompt)
    await safe_reply(update, result, parse_mode='Markdown')

async def handle_deep_dive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_user_action(update, "CalcBot", "Deep Dive")
    user_input = " ".join(context.args)
    if not user_input:
        await safe_reply(update, "📰 **请输入话题**\n例如：<code>/deep Sora模型</code>", parse_mode='HTML')
        return
    await safe_reply(update, "🧐 正在进行深度分析...")
    prompt = f"用户想深入了解话题：【{user_input}】。请提供一份深度分析报告，包含本质、争议、影响和延伸阅读。"
    result = await call_gemini_api(prompt)
    await safe_reply(update, result, parse_mode='Markdown')

async def handle_daily_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_user_action(update, "CalcBot", "Daily Digest")
    await safe_reply(update, "☕️ 正在全网搜集新闻，并召唤 Gemini 2.5 分析...")
    all_entries = []
    try:
        tasks = [asyncio.to_thread(feedparser.parse, url) for url in DEFAULT_RSS_FEEDS]
        feeds = await asyncio.gather(*tasks)
        for feed in feeds:
            if feed.entries: all_entries.extend(feed.entries[:5]) 
    except Exception as e:
        logger.error(f"RSS Fetch Error: {e}")

    if not all_entries:
        await safe_reply(update, "📭 今日全网暂无更新。")
        return

    news_content = ""
    for entry in all_entries:
        title = entry.get('title', '无标题').replace("\n", " ")
        link = entry.get('link', '')
        news_content += f"- {title} ({link})\n"

    prompt_text = f"你是一名资深的科技新闻主编。请根据以下素材，撰写一份《今日科技内参》。要求：筛选8-12条价值新闻，分类为AI、数码、商业、深度，并用中文一句话解读。素材流：\n{news_content}"

    result = await call_gemini_api(prompt_text, model="gemini-2.5-flash")
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    final_msg = f"📅 <b>今日科技内参</b> ({today})\nFrom: Gemini 2.5 Flash\n\n{result}"
    await safe_reply(update, final_msg, parse_mode='HTML')

# --- IPO 查询 (截图版) ---
async def get_stock_ipo_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_user_action(update, "CalcBot", "IPO Query")
    global BROWSER_INSTANCE
    if not BROWSER_INSTANCE:
        await safe_reply(update, "❌ 浏览器服务未就绪。")
        return
    
    user_text = update.message.text.strip()
    is_asking_listing = "上市" in user_text
    await safe_reply(update, f"🔍 正在检索【{'上市' if is_asking_listing else '申购'}】新股...")
    
    target_url = "https://vip.stock.finance.sina.com.cn/corp/go.php/vRPD_NewStockIssue/page/1.phtml"
    page = None
    async with BROWSER_LOCK:
        try:
            page = await BROWSER_INSTANCE.new_page()
            await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(1)
            screenshot_bytes = await page.screenshot(full_page=False)
            await update.message.reply_photo(photo=screenshot_bytes, caption="📸 新浪财经实时数据")
        except Exception as e:
            logger.error(f"IPO Query Error: {e}")
            await safe_reply(update, f"❌ 查询出错: {e}")
        finally:
            if page: await page.close()

# --- 计算器 ---
def safe_calculate(expression: str):
    try:
        cleaned_expr = re.sub(r'[^\d\+\-\*\/\(\)\.\%\^]', '', expression)
        if not cleaned_expr: return None
        final_expr = cleaned_expr.replace('^', '**')
        if len(final_expr) > 100: return "❌ 算式太长"
        result = simple_eval(final_expr)
        if isinstance(result, float) and result.is_integer(): result = int(result)
        return f"🔢 结果: {result}"
    except: return None

# ==============================================================================
# 5. 基础功能 (工兵 Bot 专用：链接与教程)
# ==============================================================================
def modify_url_subdomain(url_str: str, new_sub: str) -> str:
    try:
        parsed = urlparse(url_str)
        domain_parts = parsed.netloc.split('.')
        if len(domain_parts) < 2: return url_str
        domain_parts[0] = new_sub
        new_netloc = '.'.join(domain_parts)
        return parsed._replace(netloc=new_netloc).geturl()
    except Exception: return url_str

# 🔥 核心：链接获取逻辑 (API -> A -> Playwright跳转 -> B -> 修改二级域名)
async def get_universal_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_chat_allowed(context, update.message.chat_id): return
    bot_index = context.bot_data.get("bot_index", "?")
    log_user_action(update, f"WorkerBot-{bot_index}", "Get Link")
    
    current_app = context.application
    api_url = None
    for path, app_instance in BOT_APPLICATIONS.items():
        if app_instance is current_app:
            api_url = BOT_API_URLS.get(path)
            break
            
    if not api_url:
        await safe_reply(update, "❌ 配置错误：此机器人未配置 API。")
        return

    try: await safe_reply(update, "正在获取链接...")
    except: pass
    
    user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    page = None 
    async with BROWSER_LOCK:
        try:
            # 1. 从 API 获取入口域名 A
            if GLOBAL_HTTP_CLIENT is None: raise RuntimeError("HTTP Client not init")
            resp = await GLOBAL_HTTP_CLIENT.get(api_url, headers={'User-Agent': user_agent})
            api_data = resp.json()
            domain_a = api_data.get("data", "").strip()
            if not domain_a.startswith(('http://', 'https://')): domain_a = 'http://' + domain_a
            
            # 2. 使用 Playwright 访问 A，等待跳转到 B
            # 🔥 这是您强调的“先从api取得 A域名再从指向B”的关键步骤
            context_p = await context.bot_data["fastapi_app"].state.browser.new_context(user_agent=user_agent)
            page = await context_p.new_page()
            await page.goto(domain_a, wait_until="domcontentloaded", timeout=25000)
            
            # 等待潜在的 JS 跳转
            try: await page.wait_for_timeout(2000)
            except: pass
            
            # 3. 获取最终域名 B
            final_url_b = page.url
            
            # 4. 修改二级域名 (生成专属链接)
            rand_sub = ''.join(random.choices(string.ascii_lowercase + string.digits, k=5))
            final_url = modify_url_subdomain(final_url_b, rand_sub)
            
            msg = f"✅ <b>专属链接：</b>\n<code>{final_url}</code>"
            await safe_reply(update, msg, parse_mode='HTML')
            await context_p.close()
        except Exception as e:
            logger.error(f"Link Fetch Error: {e}")
            await safe_reply(update, "❌ 获取失败，请重试。")
        finally:
            if page: await page.close()

# 辅助函数：发送静态回复
async def send_static_reply_wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, html_msg: str, action_name: str):
    if not update.message or not is_chat_allowed(context, update.message.chat_id): return
    bot_index = context.bot_data.get("bot_index", "?")
    log_user_action(update, f"WorkerBot-{bot_index}", action_name)
    await safe_reply(update, html_msg, parse_mode='HTML')

# ==============================================================================
# 6. Bot Setup (严格职责划分)
# ==============================================================================

# 🔥 核心：计算器 Bot 配置 (全能王)
def setup_calculator_bot(app_instance: Application) -> None:
    async def calc_start(update, context):
        log_user_action(update, "CalcBot", "/start")
        await safe_reply(update, "👋 我是智能计算器。\n支持：算式、新股、日报、/book、/quote、/deep")

    # 1. 优先注册命令 Handlers
    app_instance.add_handler(CommandHandler("start", calc_start))
    app_instance.add_handler(CommandHandler("book", handle_book_recommend))
    app_instance.add_handler(CommandHandler("quote", handle_novel_quote))
    app_instance.add_handler(CommandHandler("deep", handle_deep_dive))

    # 2. 注册 Regex Handlers (新股、日报) - 必须在文本计算之前！
    app_instance.add_handler(MessageHandler(filters.Regex(IPO_COMMAND_PATTERN), get_stock_ipo_info))
    app_instance.add_handler(MessageHandler(filters.Regex(DIGEST_COMMAND_PATTERN), handle_daily_digest))

    # 3. 最后注册文本计算 (作为兜底)
    async def calc_catch_all(update, context):
        if not update.message or not update.message.text: return
        text = update.message.text.strip()
        # 如果不是命令，尝试计算
        if not text.startswith("/"):
            res = safe_calculate(text)
            if res:
                log_user_action(update, "CalcBot", f"Calculate: {text}")
                await safe_reply(update, res)

    app_instance.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, calc_catch_all))

# 🔥 核心：工兵 Bot 配置 (只干苦力，文案 100% 恢复)
def setup_worker_bot(app_instance: Application, bot_index: int) -> None:
    token_end = app_instance.bot.token[-4:]
    
    async def worker_start(update, context):
        log_user_action(update, f"Worker-{bot_index}", "/start")
        await safe_reply(update, f"🤖 工兵 #{bot_index} ({token_end}) 就绪。\n请发送关键词获取链接或教程。")
    
    app_instance.add_handler(CommandHandler("start", worker_start))
    
    # 链接获取
    app_instance.add_handler(MessageHandler(filters.Regex(UNIVERSAL_COMMAND_PATTERN), get_universal_link))
    
    # 🔥🔥🔥 教程全家桶 (文字完全恢复) 🔥🔥🔥
    
    # 1. 苹果大退
    msg_ios_quit = "📱 <b>苹果APP大退重新打开步骤</b>\n\n1. 上滑停留调出后台。\n2. 上滑关闭App卡片。\n3. 重新点击图标打开。"
    app_instance.add_handler(MessageHandler(filters.Regex(IOS_QUIT_PATTERN), lambda u,c: send_static_reply_wrapper(u,c, msg_ios_quit, "IOS Quit Guide")))
    
    # 2. 安卓大退
    msg_android_quit = "🤖 <b>安卓APP大退重新打开步骤</b>\n\n1. 上滑或点击多任务键进入后台。\n2. 上滑关闭App卡片。\n3. 重新打开App。"
    app_instance.add_handler(MessageHandler(filters.Regex(ANDROID_QUIT_PATTERN), lambda u,c: send_static_reply_wrapper(u,c, msg_android_quit, "Android Quit Guide")))

    # 3. 安卓浏览器设置
    msg_android_browser = "🤖 <b>安卓浏览器设置手机版</b>\n\n1. 打开浏览器菜单(≡或⋮)。\n2. 找到“桌面版”或“电脑模式”。\n3. <b>取消勾选</b>它。"
    app_instance.add_handler(MessageHandler(filters.Regex(ANDROID_BROWSER_PATTERN), lambda u,c: send_static_reply_wrapper(u,c, msg_android_browser, "Android Browser Guide")))

    # 4. 苹果浏览器设置
    msg_ios_browser = "📱 <b>苹果浏览器设置手机版</b>\n\n1. 点击地址栏左侧(大小/AA)。\n2. 选择“请求移动网站”。\n(如果显示“请求桌面网站”则无需操作)"
    app_instance.add_handler(MessageHandler(filters.Regex(IOS_BROWSER_PATTERN), lambda u,c: send_static_reply_wrapper(u,c, msg_ios_browser, "IOS Browser Guide")))

    # 5. 安卓标签上限
    msg_android_tab = "🤖 <b>安卓窗口上限解决</b>\n\n1. 点击浏览器标签页图标(数字框)。\n2. 选择“关闭所有标签页”或手动关闭旧标签。"
    app_instance.add_handler(MessageHandler(filters.Regex(ANDROID_TAB_LIMIT_PATTERN), lambda u,c: send_static_reply_wrapper(u,c, msg_android_tab, "Android Tab Limit")))

    # 6. 苹果标签上限
    msg_ios_tab = "📱 <b>苹果窗口上限解决</b>\n\n1. 长按右下角标签图标。\n2. 选择“关闭所有标签页”。"
    app_instance.add_handler(MessageHandler(filters.Regex(IOS_TAB_LIMIT_PATTERN), lambda u,c: send_static_reply_wrapper(u,c, msg_ios_tab, "IOS Tab Limit")))

# ==============================================================================
# 7. 启动逻辑
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
        logger.info("✅ Playwright & HTTP Client Ready")
    except Exception as e:
        logger.error(f"❌ Init Error: {e}")

    # 1. 初始化工兵 Bots (1-10)
    for i in range(1, 10):
        token = os.getenv(f"BOT_TOKEN_{i}")
        if token:
            application = Application.builder().token(token).build()
            application.bot_data["fastapi_app"] = app
            application.bot_data["bot_index"] = i 
            await application.initialize()
            
            # 🔥 配置工兵逻辑 (链接+教程)
            setup_worker_bot(application, i)
            
            path = f"bot{i}_webhook"
            BOT_APPLICATIONS[path] = application
            if url := os.getenv(f"BOT_{i}_API_URL"): BOT_API_URLS[path] = url
            if al := os.getenv(f"BOT_{i}_ALLOWED_CHAT_IDS"): BOT_ALLOWED_CHATS[path] = [cid.strip() for cid in al.split(',') if cid.strip()]
            
            await application.start()
            await application.updater.start_polling()
            logger.info(f"🚀 Worker Bot #{i} Started Polling")

    # 2. 初始化计算器 Bot (全能王)
    calc_token = os.getenv("CALC_BOT_TOKEN")
    if calc_token:
        try:
            calc_app = Application.builder().token(calc_token).build()
            await calc_app.initialize()
            
            # 🔥 配置计算器逻辑 (AI+IPO+计算)
            setup_calculator_bot(calc_app)
            
            await calc_app.start()
            await calc_app.updater.start_polling()
            BOT_APPLICATIONS["calc_bot"] = calc_app
            logger.info(f"👑 Calculator Bot Started (Full Features)")
        except Exception as e:
            logger.error(f"❌ Calculator Bot Failed: {e}")

@app.on_event("shutdown")
async def shutdown_event():
    if GLOBAL_HTTP_CLIENT: await GLOBAL_HTTP_CLIENT.aclose()
    if BROWSER_INSTANCE: await BROWSER_INSTANCE.close()
    if PLAYWRIGHT_INSTANCE: await PLAYWRIGHT_INSTANCE.stop()
    for app_inst in BOT_APPLICATIONS.values():
        await app_inst.stop()
        await app_inst.shutdown()

@app.get("/")
async def root(): return {"status": "Running", "bots_active": len(BOT_APPLICATIONS)}
