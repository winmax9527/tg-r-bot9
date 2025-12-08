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
from fastapi import FastAPI, Request, Response
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import BadRequest
from playwright.async_api import async_playwright, Playwright, Browser, TimeoutError as PlaywrightTimeoutError
from simpleeval import simple_eval
import feedparser

# ==============================================================================
# 1. 日志配置
# ==============================================================================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger("BotLogic")

# 🔥 降噪
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

# --- 2. 全局状态 ---
BOT_APPLICATIONS: Dict[str, Application] = {}
BOT_API_URLS: Dict[str, str] = {}
BOT_APK_URLS: Dict[str, str] = {}
BOT_SCHEDULES: Dict[str, Dict[str, Any]] = {} 
BOT_ALLOWED_CHATS: Dict[str, List[str]] = {} 
PLAYWRIGHT_INSTANCE: Playwright | None = None
BROWSER_INSTANCE: Browser | None = None
GLOBAL_HTTP_CLIENT: httpx.AsyncClient | None = None

# 🔥 默认 RSS 源
DEFAULT_RSS_FEEDS = [
    "https://36kr.com/feed",
    "https://www.cnbeta.com.tw/backend.php",
]

BROWSER_LOCK = asyncio.Semaphore(1)
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
IPO_COMMAND_PATTERN = r"^(新股|新股申购|新股上市|近期新股|申购|上市)$"
DIGEST_COMMAND_PATTERN = r"^(日报|简报|新闻|每日简报|科技新闻)$"


# --- 辅助函数 ---
def is_chat_allowed(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    current_app = context.application
    allowed_list: List[str] = []
    for path, app_instance in BOT_APPLICATIONS.items():
        if app_instance is current_app:
            allowed_list = BOT_ALLOWED_CHATS.get(path, [])
            break
    chat_id_str = str(chat_id)
    possible_ids_to_check = {chat_id_str} 
    if chat_id_str.startswith("-100"): possible_ids_to_check.add(f"-{chat_id_str[4:]}")
    elif chat_id_str.startswith("-"): possible_ids_to_check.add(f"-100{chat_id_str[1:]}")
    for check_id in possible_ids_to_check:
        if check_id in allowed_list: return True 
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
    except Exception: return url_str

async def safe_reply(update: Update, text: str, parse_mode=None):
    try:
        if parse_mode: await update.message.reply_text(text, parse_mode=parse_mode)
        else: await update.message.reply_text(text)
    except BadRequest as e:
        if "Message to be replied not found" in str(e):
            try: await update.message.chat.send_message(text, parse_mode=parse_mode)
            except Exception: pass
    except Exception: pass

# --- Playwright Handlers ---
async def get_universal_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_chat_allowed(context, update.message.chat_id): return
    bot_id = context.bot_data.get("bot_index", "?")
    fastapi_app = context.bot_data.get("fastapi_app")
    if not fastapi_app or not hasattr(fastapi_app.state, 'browser'):
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
    try: await safe_reply(update, "正在为您获取专属通用下载链接，请稍候 ...")
    except: pass
    
    user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    page = None 
    browser_context = None
    async with BROWSER_LOCK:
        try:
            if GLOBAL_HTTP_CLIENT is None: raise RuntimeError("Global HTTP Client not initialized")
            resp = await GLOBAL_HTTP_CLIENT.get(api_url, headers={'User-Agent': user_agent})
            resp.raise_for_status()
            api_data = resp.json()
            if api_data.get("code") != 0 or "data" not in api_data:
                await safe_reply(update, "❌ API 未返回有效链接。")
                return
            domain_a = api_data["data"].strip()
            if not domain_a.startswith(('http://', 'https://')): domain_a = 'http://' + domain_a
            
            browser_context = await fastapi_app.state.browser.new_context(user_agent=user_agent, viewport={'width': 1280, 'height': 800})
            page = await browser_context.new_page()
            page.set_default_timeout(30000)
            try:
                await page.goto(domain_a, wait_until="domcontentloaded")
                try: await page.wait_for_timeout(1500)
                except: pass
            except PlaywrightTimeoutError:
                await safe_reply(update, "❌ 源站响应太慢，请重试。")
                return 
            except Exception:
                await safe_reply(update, "❌ 无法连接到源站。")
                return 
            domain_b = page.url 
            if "chrome-error://" in domain_b or "chromewebdata" in domain_b:
                await safe_reply(update, "⚠️ 线路维护中，请稍后再试。")
                return
            random_sub = generate_universal_subdomain()
            final_url = modify_url_subdomain(domain_b, random_sub)
            msg = f"✅ <b>您的专属通用下载链接已生成！</b>\n👇 <b>点击下方链接即可复制：</b>\n<code>{final_url}</code>\n💡 <i>请务必在手机自带浏览器中打开</i>"
            await safe_reply(update, msg, parse_mode='HTML')
        except httpx.TimeoutException: await safe_reply(update, "❌ 获取链接超时，对方服务器响应太慢，请重试。")
        except Exception: await safe_reply(update, "❌ 系统繁忙，请重试。")
        finally:
            if page: 
                try: await page.close()
                except: pass
            if browser_context:
                try: await browser_context.close()
                except: pass

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
        msg = f"✅ <b>安卓专用链接：</b>\n<code>{final_url}</code>"
        await safe_reply(update, msg, parse_mode='HTML')
    except Exception: pass

async def send_static_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, log_msg: str, html_msg: str):
    if not update.message or not is_chat_allowed(context, update.message.chat_id): return
    try: await safe_reply(update, html_msg, parse_mode='HTML')
    except Exception: pass

async def send_ios_quit_guide(u, c): await send_static_reply(u, c, "发送苹果大退", "📱 <b>苹果手机APP大退步骤</b>...")
async def send_android_quit_guide(u, c): await send_static_reply(u, c, "发送安卓大退", "🤖 <b>安卓手机APP大退步骤</b>...")
async def send_android_browser_guide(u, c): await send_static_reply(u, c, "发送安卓浏览器", "🤖 <b>安卓浏览器设置手机版</b>...")
async def send_ios_browser_guide(u, c): await send_static_reply(u, c, "发送苹果浏览器", "📱 <b>苹果浏览器设置手机版</b>...")
async def send_android_tab_limit_guide(u, c): await send_static_reply(u, c, "发送安卓窗口上限", "🤖 <b>安卓窗口上限解决</b>...")
async def send_ios_tab_limit_guide(u, c): await send_static_reply(u, c, "发送苹果窗口上限", "📱 <b>苹果窗口上限解决</b>...")

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

def safe_calculate(expression: str):
    try:
        cleaned_expr = re.sub(r'[^\d\+\-\*\/\(\)\.\%\^]', '', expression)
        if not cleaned_expr or (len(cleaned_expr) == 1 and cleaned_expr in '+-*/.^%'): return None
        final_expr = cleaned_expr.replace('^', '**')
        if len(final_expr) > 100: return "❌ 算式太长了。"
        result = simple_eval(final_expr)
        if isinstance(result, float) and result.is_integer(): result = int(result)
        return f"🔢 结果: {result}"
    except Exception: return None

async def get_stock_ipo_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BROWSER_INSTANCE
    if not BROWSER_INSTANCE:
        if hasattr(app.state, 'browser') and app.state.browser: BROWSER_INSTANCE = app.state.browser
        else:
            await safe_reply(update, "❌ 浏览器服务未就绪。")
            return
    user_text = update.message.text.strip()
    is_asking_listing = "上市" in user_text
    if is_asking_listing: await safe_reply(update, "🔍 正在检索【即将上市】及【排队中】新股...")
    else: await safe_reply(update, "🔍 正在检索【即将申购】新股...")
    target_url = "https://vip.stock.finance.sina.com.cn/corp/go.php/vRPD_NewStockIssue/page/1.phtml"
    page = None
    browser_context = None
    async with BROWSER_LOCK:
        try:
            browser_context = await BROWSER_INSTANCE.new_context(user_agent='Mozilla/5.0 ...', viewport={'width': 1280, 'height': 800})
            page = await browser_context.new_page()
            await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(1)
            try:
                screenshot_bytes = await page.screenshot(full_page=False)
                await update.message.reply_photo(photo=screenshot_bytes, caption="📸 数据源验证 (新浪财经)")
            except: pass
            stocks_data = await page.evaluate('''() => {
                let table = document.getElementById('NewStockIssueTable');
                if (!table) table = document.querySelector('table'); 
                if (!table) return [];
                const rows = table.querySelectorAll('tbody tr');
                const data = [];
                for (let i = 1; i < rows.length; i++) {
                    const row = rows[i];
                    const cells = row.querySelectorAll('td');
                    if (cells.length < 5) continue;
                    const getText = (idx) => cells[idx] ? cells[idx].innerText.trim() : "";
                    data.push({code: getText(0), name: getText(2), sub_date: getText(3), list_date: getText(4)});
                }
                return data;
            }''')
            today_str = datetime.datetime.now().strftime("%Y-%m-%d")
            result_lines = []
            if is_asking_listing:
                title = "🔔 <b>近期即将上市 / 待上市</b>"
                confirmed, tbd = [], []
                for item in stocks_data:
                    code, name, l_date, s_date = item['code'], item['name'], item['list_date'], item['sub_date']
                    if not code or not code.isdigit(): continue
                    if len(l_date) >= 8 and l_date[0].isdigit():
                        if l_date >= today_str:
                            display = l_date[5:] if len(l_date)>=10 else l_date
                            confirmed.append(f"• <code>{code}</code> <b>{name}</b> ({display} 上市)")
                    elif s_date and len(s_date) >= 5: tbd.append(f"• <code>{code}</code> <b>{name}</b> (待定)")
                result_lines = sorted(list(set(confirmed))) + sorted(list(set(tbd)))
            else:
                title = "📅 <b>近期即将申购</b>"
                temp_list = []
                for item in stocks_data:
                    code, name, s_date = item['code'], item['name'], item['sub_date']
                    if not code or not code.isdigit(): continue
                    if len(s_date) >= 8 and s_date[0].isdigit():
                        if s_date >= today_str:
                            display = s_date[5:] if len(s_date)>=10 else s_date
                            temp_list.append(f"• <code>{code}</code> <b>{name}</b> ({display} 申购)")
                result_lines = sorted(list(set(temp_list)))
            if not result_lines: await safe_reply(update, "📭 数据列表为空。")
            else:
                final_msg = f"{title}\n" + "\n".join(result_lines)
                await safe_reply(update, final_msg, parse_mode='HTML')
        except Exception as e:
            logger.error(f"Stock Error: {e}")
            await safe_reply(update, f"❌ 访问出错: {e}")
        finally:
            if page: await page.close()
            if browser_context: await browser_context.close()

# --- 🔥 [关键修改] 侦探版原生 HTTP 日报生成器 ---
async def handle_daily_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 1. 获取 Key
    MY_KEY = os.getenv("GOOGLE_GEMINI_KEY")
    if not MY_KEY:
        await safe_reply(update, "❌ 错误：未配置 GOOGLE_GEMINI_KEY。")
        return

    await safe_reply(update, "☕️ 正在尝试连接 AI 模型生成简报...")
    
    # 2. 抓取 RSS
    all_entries = []
    try:
        for feed_url in DEFAULT_RSS_FEEDS:
            feed = await asyncio.to_thread(feedparser.parse, feed_url)
            if feed.entries: all_entries.extend(feed.entries[:2])
    except Exception: pass

    if not all_entries:
        await safe_reply(update, "📭 今日暂无新闻更新。")
        return

    # 3. 准备提示词
    prompt_text = "请将以下科技新闻总结为一份简报。要求：\n1. 中文回答\n2. 每条新闻用一个emoji开头\n3. 语言简练\n\n内容：\n"
    for entry in all_entries[:5]:
        title = entry.get('title', '无标题')
        link = entry.get('link', '')
        prompt_text += f"标题：{title}\n链接：{link}\n---\n"

    # 4. 🔥【终极方案】定义一个模型列表，轮询尝试，直到成功
    # 这样不管 Google 怎么改名，或者你的账号支持哪个，总能撞对一个
    candidate_models = [
        "gemini-1.5-flash-latest", # 尝试1：最新版 Flash
        "gemini-1.5-flash",        # 尝试2：标准版 Flash
        "gemini-1.5-flash-001",    # 尝试3：特定版 Flash
        "gemini-pro"               # 尝试4：保底 (1.0 Pro，最稳)
    ]

    last_error = ""
    success_content = None

    if not GLOBAL_HTTP_CLIENT: 
        await safe_reply(update, "❌ 系统错误: HTTP Client 未就绪")
        return

    # 开始轮询
    for model_name in candidate_models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={MY_KEY}"
        payload = { "contents": [{ "parts": [{"text": prompt_text}] }] }
        
        try:
            # 这里的 timeout 设长一点，给 AI 思考时间
            response = await GLOBAL_HTTP_CLIENT.post(url, json=payload, timeout=60.0)
            
            if response.status_code == 200:
                # 成功！解析数据
                data = response.json()
                if 'candidates' in data and data['candidates']:
                    success_content = data['candidates'][0]['content']['parts'][0]['text']
                    # 打印一下是哪个模型成功的，方便之后查看日志
                    logger.info(f"✅ Success with model: {model_name}")
                    break # 跳出循环
            else:
                # 记录错误，尝试下一个
                last_error = f"Model {model_name} failed: {response.status_code}"
                logger.warning(last_error)
                
        except Exception as e:
            last_error = str(e)
            continue # 尝试下一个

    # 5. 发送结果
    if success_content:
        await safe_reply(update, f"📅 <b>今日 AI 简报</b>\n\n{success_content}", parse_mode='HTML')
    else:
        # 如果所有模型都试完了还不行
        await safe_reply(update, f"❌ 所有模型均尝试失败。\n最后一次报错: {last_error}\n请检查 Key 是否有效。")

def setup_calculator_bot(app_instance: Application) -> None:
    async def calc_start(update, context):
        await safe_reply(update, "👋 我是智能计算器。\n\n1️⃣ 发送算式\n2️⃣ 发送 <b>新股</b>\n3️⃣ 发送 <b>日报</b>", parse_mode='HTML')
    async def calc_handle_message(update, context):
        if not update.message or not update.message.text: return
        user_text = update.message.text.strip()
        if user_text.startswith("/start"): return
        final_expression = user_text
        if update.message.reply_to_message and update.message.reply_to_message.text:
            if re.match(r'^[\+\-\*\/\^]', user_text):
                reply_text = update.message.reply_to_message.text
                match = re.search(r"结果:\s*(-?\d+(\.\d+)?)", reply_text)
                previous_num = None
                if match: previous_num = match.group(1)
                elif re.match(r'^-?\d+(\.\d+)?$', reply_text.strip()): previous_num = reply_text.strip()
                if previous_num: final_expression = f"{previous_num}{user_text}"
        result = safe_calculate(final_expression)
        if result: await safe_reply(update, result)

    app_instance.add_handler(CommandHandler("start", calc_start))
    app_instance.add_handler(MessageHandler(filters.TEXT & filters.Regex(IPO_COMMAND_PATTERN), get_stock_ipo_info))
    app_instance.add_handler(MessageHandler(filters.TEXT & filters.Regex(DIGEST_COMMAND_PATTERN), handle_daily_digest))
    app_instance.add_handler(MessageHandler(filters.TEXT, calc_handle_message))

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
    if GLOBAL_IMAGE_PATTERN: app_instance.add_handler(MessageHandler(filters.TEXT & filters.Regex(GLOBAL_IMAGE_PATTERN), send_global_image))
    if GLOBAL_VIDEO_PATTERN: app_instance.add_handler(MessageHandler(filters.TEXT & filters.Regex(GLOBAL_VIDEO_PATTERN), send_global_video))
    async def start_command(update, context):
        if not update.message or not is_chat_allowed(context, update.message.chat_id): return
        await safe_reply(update, f"🤖 Bot #{bot_index} ({token_end}) 就绪。", parse_mode='HTML')
    app_instance.add_handler(CommandHandler("start", start_command))

# --- FastAPI & Startup ---
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
                            msg = sched["message"].replace("<br>", "\n")
                            for cid in sched["chat_ids"]:
                                try: await app_inst.bot.send_message(chat_id=cid, text=msg, parse_mode='HTML')
                                except: pass
                            sched["last_sent"] = now
        except Exception: pass
        await asyncio.sleep(60)

@app.on_event("startup")
async def startup_event():
    global BOT_APPLICATIONS, BOT_API_URLS, BOT_APK_URLS, BOT_SCHEDULES, BOT_ALLOWED_CHATS, PLAYWRIGHT_INSTANCE, BROWSER_INSTANCE
    global GLOBAL_IMAGE_MAP, GLOBAL_IMAGE_PATTERN, GLOBAL_VIDEO_MAP, GLOBAL_VIDEO_PATTERN
    global GLOBAL_HTTP_CLIENT
    
    GLOBAL_HTTP_CLIENT = httpx.AsyncClient(timeout=30.0, verify=False, limits=httpx.Limits(max_keepalive_connections=20, max_connections=50))
    logger.info("✅ HTTP Client 初始化成功")

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

    if GLOBAL_IMAGE_MAP: GLOBAL_IMAGE_PATTERN = r"^(" + "|".join([re.escape(k) for k in GLOBAL_IMAGE_MAP.keys()]) + r")$"
    if GLOBAL_VIDEO_MAP: GLOBAL_VIDEO_PATTERN = r"^(" + "|".join([re.escape(k) for k in GLOBAL_VIDEO_MAP.keys()]) + r")$"

    for i in range(1, 10):
        token = os.getenv(f"BOT_TOKEN_{i}")
        if token:
            application = Application.builder().token(token).build()
            application.bot_data["fastapi_app"] = app
            application.bot_data["bot_index"] = i 
            await application.initialize()
            setup_bot(application, i)
            path = f"bot{i}_webhook"
            BOT_APPLICATIONS[path] = application
            if url := os.getenv(f"BOT_{i}_API_URL"): BOT_API_URLS[path] = url
            if url := os.getenv(f"BOT_{i}_APK_URL"): BOT_APK_URLS[path] = url
            if al := os.getenv(f"BOT_{i}_ALLOWED_CHAT_IDS"): BOT_ALLOWED_CHATS[path] = [cid.strip() for cid in al.split(',') if cid.strip()]
            s_cids, s_times, s_msg = os.getenv(f"BOT_{i}_SCHEDULE_CHAT_ID"), os.getenv(f"BOT_{i}_SCHEDULE_TIMES_UTC"), os.getenv(f"BOT_{i}_SCHEDULE_MESSAGE")
            if s_cids and s_times and s_msg:
                BOT_SCHEDULES[path] = {"chat_ids": [c.strip() for c in s_cids.split(',')], "times": [t.strip() for t in s_times.split(',')], "message": s_msg, "last_sent": None}
            logger.info(f"Bot #{i} ({token[-4:]}) 加载完成")

    calc_token = os.getenv("CALC_BOT_TOKEN")
    if calc_token:
        try:
            calc_app = Application.builder().token(calc_token).build()
            await calc_app.initialize()
            setup_calculator_bot(calc_app)
            BOT_APPLICATIONS["calc_bot_webhook"] = calc_app
            logger.info(f"🧮 计算器 Bot ({calc_token[-4:]}) 加载完成")
        except Exception as e: logger.error(f"❌ 计算器 Bot 启动失败: {e}")
    else: logger.info("⚠️ 未检测到 CALC_BOT_TOKEN，计算器 Bot 跳过启动。")

    try:
        PLAYWRIGHT_INSTANCE = await async_playwright().start()
        BROWSER_INSTANCE = await PLAYWRIGHT_INSTANCE.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--disable-software-rasterizer"])
        app.state.browser = BROWSER_INSTANCE
        logger.info("✅ Playwright 启动成功")
    except Exception as e: logger.error(f"❌ Playwright 启动失败: {e}")
    asyncio.create_task(background_scheduler())

@app.on_event("shutdown")
async def shutdown_event():
    if GLOBAL_HTTP_CLIENT: await GLOBAL_HTTP_CLIENT.aclose()
    if BROWSER_INSTANCE: await BROWSER_INSTANCE.close()
    if PLAYWRIGHT_INSTANCE: await PLAYWRIGHT_INSTANCE.stop()

@app.post("/{webhook_path}")
async def handle_webhook(webhook_path: str, request: Request):
    if webhook_path not in BOT_APPLICATIONS: return Response(status_code=404)
    try:
        update = Update.de_json(await request.json(), BOT_APPLICATIONS[webhook_path].bot)
        asyncio.create_task(BOT_APPLICATIONS[webhook_path].process_update(update))
        return Response(status_code=200)
    except Exception: return Response(status_code=500)

@app.get("/")
async def root(): return {"status": "OK", "bots": len(BOT_APPLICATIONS)}
