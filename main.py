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
# 1. 日志与全局配置
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
BROWSER_LOCK = asyncio.Semaphore(1)

# 🔥 [升级版] RSS 源列表 (涵盖科技、AI、数码、深度)
DEFAULT_RSS_FEEDS = [
    "https://36kr.com/feed",                 # 36氪 (商业/创投)
    "https://www.cnbeta.com.tw/backend.php", # cnBeta (IT资讯)
    "https://www.ithome.com/rss/",           # IT之家 (数码硬件)
    "https://sspai.com/feed",                # 少数派 (软件/应用)
    "http://www.zhihudaily.com/#/index",     # 知乎日报
]

GLOBAL_IMAGE_MAP: Dict[str, str] = {} 
GLOBAL_IMAGE_PATTERN: str = "" 
GLOBAL_VIDEO_MAP: Dict[str, str] = {} 
GLOBAL_VIDEO_PATTERN: str = "" 

# --- 3. 核心正则 (严格保留原样) ---
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

# ==============================================================================
# 4. 辅助函数 (权限、工具、AI调用)
# ==============================================================================

def is_chat_allowed(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    """权限检查逻辑"""
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
        return parsed._replace(netloc=new_netloc).geturl()
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

async def send_static_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, log_msg: str, html_msg: str):
    if not update.message or not is_chat_allowed(context, update.message.chat_id): return
    try: await safe_reply(update, html_msg, parse_mode='HTML')
    except Exception: pass

# 🔥 核心 AI 调用函数 (Gemini 2.5 Flash)
async def call_gemini_api(prompt: str, model: str = "gemini-2.5-flash") -> str:
    MY_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_GEMINI_KEY")
    if not MY_KEY: return "❌ 未配置 API Key"
    if not GLOBAL_HTTP_CLIENT: return "❌ HTTP Client 未就绪"

    # 使用 v1beta 接口以支持新模型
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
# 5. AI 文化插件 (/book, /quote, /deep)
# ==============================================================================

async def handle_book_recommend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not is_chat_allowed(context, update.message.chat_id): return
    user_input = " ".join(context.args)
    if not user_input:
        await safe_reply(update, "📚 **请告诉我您想看什么类型的书？**\n例如：\n<code>/book 科幻小说</code>", parse_mode='HTML')
        return
    await safe_reply(update, f"🤔 正在为您检索【{user_input}】相关的书籍...")
    prompt = f"""你是一位资深图书编辑。用户想找【{user_input}】类型的书。请推荐 3 本高质量书籍。
    格式要求：
    📖 **《书名》** (作者)
    🏷️ 关键词：#标签
    💡 **推荐理由**：简练介绍。
    📝 **经典摘抄**：一句原文。"""
    result = await call_gemini_api(prompt)
    await safe_reply(update, result, parse_mode='Markdown')

async def handle_novel_quote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not is_chat_allowed(context, update.message.chat_id): return
    user_input = " ".join(context.args)
    target = f"《{user_input}》" if user_input else "世界经典文学名著"
    await safe_reply(update, "📜 正在翻阅藏书，挑选片段...")
    prompt = f"""请从{target}中挑选一段经典原文摘抄（100-200字）。
    然后以文学评论家身份进行【深度赏析】（背景、美感、哲理）。
    格式：
    📜 **原文**：“...”
    ✨ **赏析**：..."""
    result = await call_gemini_api(prompt)
    await safe_reply(update, result, parse_mode='Markdown')

async def handle_deep_dive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not is_chat_allowed(context, update.message.chat_id): return
    user_input = " ".join(context.args)
    if not user_input:
        await safe_reply(update, "📰 **请输入您想深入了解的话题**\n例如：<code>/deep Sora模型</code>", parse_mode='HTML')
        return
    await safe_reply(update, "🧐 正在进行深度分析...")
    prompt = f"""用户想深入了解话题：【{user_input}】。请提供一份深度分析报告。
    包含维度：1.🧐本质是什么(通俗类比) 2.⚖️核心争议/难点 3.🚀未来影响 4.📚延伸阅读"""
    result = await call_gemini_api(prompt)
    await safe_reply(update, result, parse_mode='Markdown')

# ==============================================================================
# 6. 核心业务 Handlers (保留原有的链接获取、指南等)
# ==============================================================================

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
        msg = f"✅ <b>您的安卓专用下载链接已生成！</b>\n👇 <b>点击下方链接即可复制：</b>\n<code>{final_url}</code>\n💡 <i>请务必在手机自带浏览器中打开</i>"
        await safe_reply(update, msg, parse_mode='HTML')
    except Exception: pass

# --- 静态指南回复 (保留所有文字) ---
async def send_ios_quit_guide(u, c): await send_static_reply(u, c, "发送苹果大退", "📱 <b>苹果APP大退重新打开步骤</b>\n\n1. 上滑停留调出后台。\n2. 上滑关闭App卡片。\n3. 重新点击图标打开。")
async def send_android_quit_guide(u, c): await send_static_reply(u, c, "发送安卓大退", "🤖 <b>安卓APP大退重新打开步骤</b>\n\n1. 上滑或点击多任务键进入后台。\n2. 上滑关闭App卡片。\n3. 重新打开App。")
async def send_android_browser_guide(u, c): await send_static_reply(u, c, "发送安卓浏览器", "🤖 <b>安卓浏览器设置手机版</b>\n\n1. 打开浏览器菜单(≡或⋮)。\n2. 找到“桌面版”或“电脑模式”。\n3. <b>取消勾选</b>它。")
async def send_ios_browser_guide(u, c): await send_static_reply(u, c, "发送苹果浏览器", "📱 <b>苹果浏览器设置手机版</b>\n\n1. 点击地址栏左侧(大小/AA)。\n2. 选择“请求移动网站”。\n(如果显示“请求桌面网站”则无需操作)")
async def send_android_tab_limit_guide(u, c): await send_static_reply(u, c, "发送安卓窗口上限", "🤖 <b>安卓窗口上限解决</b>\n\n1. 点击浏览器标签页图标(数字框)。\n2. 选择“关闭所有标签页”或手动关闭旧标签。")
async def send_ios_tab_limit_guide(u, c): await send_static_reply(u, c, "发送苹果窗口上限", "📱 <b>苹果窗口上限解决</b>\n\n1. 长按右下角标签图标。\n2. 选择“关闭所有标签页”。")

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

# IPO / Stock Info
async def get_stock_ipo_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BROWSER_INSTANCE
    if not update.message or not is_chat_allowed(context, update.message.chat_id): return
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

# ==============================================================================
# 7. 豪华版日报 (RSS + Gemini 2.5)
# ==============================================================================

async def handle_daily_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not is_chat_allowed(context, update.message.chat_id): return
    await safe_reply(update, "☕️ 正在全网搜集新闻，并召唤 Gemini 2.5 进行深度分析...")
    all_entries = []
    try:
        tasks = [asyncio.to_thread(feedparser.parse, url) for url in DEFAULT_RSS_FEEDS]
        feeds = await asyncio.gather(*tasks)
        for feed in feeds:
            if feed.entries: all_entries.extend(feed.entries[:5]) 
    except Exception: pass

    if not all_entries:
        await safe_reply(update, "📭 尴尬，今日全网暂无更新。")
        return

    news_content = ""
    for entry in all_entries:
        title = entry.get('title', '无标题').replace("\n", " ")
        link = entry.get('link', '')
        news_content += f"- {title} ({link})\n"

    prompt_text = f"""你是一名资深的科技新闻主编。请根据以下素材，撰写一份《今日科技内参》。
要求：
1. 筛选 8-12 条最有价值的新闻。
2. 分类：🤖AI前沿、📱数码硬件、🚀商业业界、💡深度有趣。
3. 深度概括：每条新闻用中文进行 1-2 句解读。
4. 格式美观，带Emoji和链接。
素材流：\n{news_content}"""

    result = await call_gemini_api(prompt_text, model="gemini-2.5-flash")
    
    if "❌" in result and len(result) < 50:
        await safe_reply(update, result)
    else:
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        final_msg = f"📅 <b>今日科技内参</b> ({today})\nFrom: Gemini 2.5 Flash\n\n{result}"
        await safe_reply(update, final_msg, parse_mode='HTML')

# ==============================================================================
# 8. Bot Setup & Startup
# ==============================================================================

def setup_calculator_bot(app_instance: Application) -> None:
    async def calc_start(update, context):
        await safe_reply(update, "👋 我是智能计算器。\n\n1️⃣ 发送算式\n2️⃣ 发送 <b>新股</b>\n3️⃣ 发送 <b>日报</b>\n4️⃣ 试用 /book, /quote, /deep", parse_mode='HTML')
    async def calc_handle_message(update, context):
        if not update.message or not update.message.text: return
        user_text = update.message.text.strip()
        if user_text.startswith("/start"): return
        # 如果是命令，跳过计算逻辑
        if user_text.startswith("/"): return
        
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
    # ✅ 注册 AI 功能到计算器
    app_instance.add_handler(CommandHandler("book", handle_book_recommend))
    app_instance.add_handler(CommandHandler("quote", handle_novel_quote))
    app_instance.add_handler(CommandHandler("deep", handle_deep_dive))
    # 注册日报和新股
    app_instance.add_handler(MessageHandler(filters.TEXT & filters.Regex(IPO_COMMAND_PATTERN), get_stock_ipo_info))
    app_instance.add_handler(MessageHandler(filters.TEXT & filters.Regex(DIGEST_COMMAND_PATTERN), handle_daily_digest))
    app_instance.add_handler(MessageHandler(filters.TEXT, calc_handle_message))

def setup_bot(app_instance: Application, bot_index: int) -> None:
    token_end = app_instance.bot.token[-4:]
    
    # ✅ 注册 AI 文化插件 (新功能)
    app_instance.add_handler(CommandHandler("book", handle_book_recommend))
    app_instance.add_handler(CommandHandler("quote", handle_novel_quote))
    app_instance.add_handler(CommandHandler("deep", handle_deep_dive))
    
    # 注册原始正则 Handlers (保留原功能)
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
        await safe_reply(update, f"🤖 Bot #{bot_index} ({token_end}) 就绪。\n试试 /book, /quote, /deep", parse_mode='HTML')
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
            # Start polling for simplicity on Render
            await application.start()
            await application.updater.start_polling()
            logger.info(f"Bot #{i} ({token[-4:]}) Polling Started")

    calc_token = os.getenv("CALC_BOT_TOKEN")
    if calc_token:
        try:
            calc_app = Application.builder().token(calc_token).build()
            await calc_app.initialize()
            setup_calculator_bot(calc_app)
            await calc_app.start()
            await calc_app.updater.start_polling()
            BOT_APPLICATIONS["calc_bot_webhook"] = calc_app
            logger.info(f"🧮 计算器 Bot ({calc_token[-4:]}) Polling Started")
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
    for app_inst in BOT_APPLICATIONS.values():
        await app_inst.stop()
        await app_inst.shutdown()

@app.post("/{webhook_path}")
async def handle_webhook(webhook_path: str, request: Request):
    # Render 上我们主要用 Polling，保留 Webhook 接口以防万一
    if webhook_path not in BOT_APPLICATIONS: return Response(status_code=404)
    try:
        update = Update.de_json(await request.json(), BOT_APPLICATIONS[webhook_path].bot)
        asyncio.create_task(BOT_APPLICATIONS[webhook_path].process_update(update))
        return Response(status_code=200)
    except Exception: return Response(status_code=500)

@app.get("/")
async def root(): return {"status": "OK", "bots": len(BOT_APPLICATIONS)}
