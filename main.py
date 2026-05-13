import os
import logging
import asyncio
import re
import random
import string
import datetime
from datetime import date, datetime, timezone, timedelta, time
from urllib.parse import urlparse
from typing import List, Dict, Any
from functools import wraps

# 🔥 核心依赖
import httpx
import feedparser
from simpleeval import simple_eval
from fastapi import FastAPI, Request, Response
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import BadRequest, Conflict
from playwright.async_api import async_playwright, Playwright, Browser, TimeoutError as PlaywrightTimeoutError

# ==============================================================================
# 1. 日志配置
# ==============================================================================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger("BotLogic")

logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

# ==============================================================================
# 2. 全局变量
# ==============================================================================
BOT_APPLICATIONS: Dict[str, Application] = {}
BOT_APK_URLS: Dict[str, str] = {} 
BOT_ALLOWED_CHATS: Dict[str, List[str]] = {}
PLAYWRIGHT_INSTANCE: Playwright | None = None
BROWSER_INSTANCE: Browser | None = None
GLOBAL_HTTP_CLIENT: httpx.AsyncClient | None = None

# ⚠️ 并发锁：保命符！强制改为 1，防止双平台同时触发导致 512M 内存溢出
BROWSER_LOCK = asyncio.Semaphore(1)

# RSS 源
DEFAULT_RSS_FEEDS = [
    "https://36kr.com/feed",
    "https://www.cnbeta.com.tw/backend.php",
    "https://www.ithome.com/rss/",
    "https://sspai.com/feed",
    "http://www.zhihudaily.com/#/index",
]

# 正则
UNIVERSAL_COMMAND_PATTERN = r"^(苹果专属|苹果专属链接|苹果专用地址|苹果专属地址|苹果专用|苹果连接|安卓连接|连接|地址|安装地址|安装链接|下载地址|下载链接|最新地址|安卓地址|苹果地址|安卓下载地址|苹果手机链接|苹果下载地址|链接|最新链接|安卓链接|安卓下载链接|最新安卓链接|苹果链接|苹果下载链接|ios链接|最新苹果链接)$"
ANDROID_SPECIFIC_COMMAND_PATTERN = r"^(安卓专属链接|安卓专属地址|安卓专属连接|安卓专属|安卓专属连接|提包|安卓专用|安卓专用链接|安卓提包链接|安卓专用地址|安卓提包地址|安卓专用下载|安卓提包|安装包|安卓安装包)$"
IOS_QUIT_PATTERN = r"^(苹果大退|苹果重启|苹果大退重启|苹果黑屏|苹果重开)$"
ANDROID_QUIT_PATTERN = r"^(安卓大退|安卓重启|安卓大退重启|安卓黑屏|安卓重开|大退|重开|闪退|卡了|黑屏)$"
ANDROID_BROWSER_PATTERN = r"^(安卓浏览器手机版|安卓桌面版|安卓浏览器|浏览器设置)$"
IOS_BROWSER_PATTERN = r"^(苹果浏览器手机版|苹果浏览器|苹果桌面版)$"
ANDROID_TAB_LIMIT_PATTERN = r"^(安卓窗口上限|窗口上限|标签上限)$"
IOS_TAB_LIMIT_PATTERN = r"^(苹果窗口上限|苹果标签上限)$"
IPO_COMMAND_PATTERN = r"^(新股|新股申购|新股上市|近期新股|申购|上市)$"
DIGEST_COMMAND_PATTERN = r"^(日报|简报|新闻|每日简报|科技新闻)$"
DOWNLOAD_HELP_PATTERN = r"^(下载方式|下载说明)$"
IP_QUERY_PATTERN = r"^(?:(?:查|IP定位)\s*[0-9a-fA-F:.]+|(?:\d{1,3}\.){3}\d{1,3}|(?:[0-9a-fA-F]{0,4}:){2,}[0-9a-fA-F:.]*)$"

GLOBAL_IMAGE_MAP: Dict[str, str] = {}
GLOBAL_IMAGE_PATTERN: str = ""
GLOBAL_VIDEO_MAP: Dict[str, str] = {}
GLOBAL_VIDEO_PATTERN: str = ""

# ==============================================================================
# 3. 辅助函数
# ==============================================================================

def is_chat_allowed(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    allowed_list = context.bot_data.get("allowed_chats", [])
    if not allowed_list: return True
    chat_id_str = str(chat_id)
    possible_ids = {chat_id_str}
    if chat_id_str.startswith("-100"): possible_ids.add(f"-{chat_id_str[4:]}")
    elif chat_id_str.startswith("-"): possible_ids.add(f"-100{chat_id_str[1:]}")
    for cid in possible_ids:
        if cid in allowed_list: return True
    return False

def log_interaction(func):
    @wraps(func)
    async def wrapper(update, context, *args, **kwargs):
        if not update or not update.effective_user:
            return await func(update, context, *args, **kwargs)
        user = update.effective_user
        chat = update.effective_chat
        try: bot_username = context.bot.username or f"Bot{context.bot.id}"
        except: bot_username = "UnknownBot"
        message_text = update.message.text if update.message else "Action"
        logger.info(f"🤖[{bot_username}] 👤{user.full_name or user.first_name}({user.id}) 🏠Chat:{chat.title or '私聊'}({chat.id}) -> 📝Cmd: {message_text}")
        try:
            result = await func(update, context, *args, **kwargs)
            logger.info(f"✅[{bot_username}] -> 处理完成 (User:{user.id})")
            return result
        except Exception as e:
            logger.error(f"❌[{bot_username}] -> 执行出错: {e}", exc_info=True)
            raise e
    return wrapper
    
async def safe_reply(update: Update, text: str, parse_mode=None):
    try:
        if parse_mode: await update.message.reply_text(text, parse_mode=parse_mode)
        else: await update.message.reply_text(text)
    except BadRequest as e:
        logger.warning(f"Reply failed with {parse_mode}: {e} -> ⚠️ 正在降级为纯文本重发...")
        try: await update.message.reply_text(text)
        except Exception as e2: logger.error(f"Retry failed: {e2}")
    except Exception as e:
        logger.error(f"General Reply Error: {e}")

# ==============================================================================
# 4. 核心业务逻辑 (TG & Potato 共用)
# ==============================================================================

def generate_universal_subdomain(min_len: int = 4, max_len: int = 7) -> str:
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=random.randint(min_len, max_len)))

def generate_android_specific_subdomain(min_len: int = 5, max_len: int = 9) -> str:
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=random.randint(min_len, max_len)))

def modify_url_subdomain(url_str: str, new_sub: str) -> str:
    try:
        parsed = urlparse(url_str)
        domain_parts = parsed.netloc.split('.')
        if len(domain_parts) < 2: return url_str
        domain_parts[0] = new_sub
        new_netloc = '.'.join(domain_parts)
        return parsed._replace(netloc=new_netloc).geturl()
    except Exception: return url_str

async def fetch_universal_link_core(api_url: str) -> str:
    """提取出的核心 Playwright 抓取逻辑，供 TG 和 Potato 共用"""
    user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    page = None
    async with BROWSER_LOCK:
        if GLOBAL_HTTP_CLIENT is None: raise RuntimeError("HTTP Client error")
        resp = await GLOBAL_HTTP_CLIENT.get(api_url, headers={'User-Agent': user_agent})
        api_data = resp.json()
        domain_a = api_data.get("data", "").strip()
        if not domain_a.startswith(('http://', 'https://')): domain_a = 'http://' + domain_a
        
        if not BROWSER_INSTANCE: raise RuntimeError("Browser not ready")
        context_p = await BROWSER_INSTANCE.new_context(user_agent=user_agent)
        page = await context_p.new_page()
        
        await page.route("**/*", lambda route: route.abort() 
            if route.request.resource_type in ["image", "media", "font", "stylesheet"] 
            else route.continue_())
        
        try: await page.goto(domain_a, wait_until="domcontentloaded", timeout=25000)
        except Exception: pass

        try: await page.wait_for_timeout(3000)
        except: pass
        
        final_url_b = page.url
        if "chrome-error" in final_url_b: raise Exception("Chrome Error")

        rand_sub = generate_universal_subdomain()
        final_url = modify_url_subdomain(final_url_b, rand_sub)
        
        await context_p.close()
        return final_url

# ==============================================================================
# 5. Telegram 工兵处理函数
# ==============================================================================

@log_interaction
async def get_universal_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_chat_allowed(context, update.message.chat_id): return
    api_url = context.bot_data.get("api_url")
    if not api_url:
        await safe_reply(update, "❌ 配置错误：未找到 API。")
        return

    try: await safe_reply(update, "正在为您获取专属通用下载链接，请稍候 ...")
    except: pass
    
    try:
        final_url = await fetch_universal_link_core(api_url)
        msg = f"✅ <b>您的专属通用下载链接已生成！</b>\n👇 <b>点击下方链接即可复制：</b>\n<code>{final_url}</code>\n💡 <i>请务必在手机自带浏览器中打开</i>"
        await safe_reply(update, msg, parse_mode='HTML')
    except Exception as e:
        logger.error(f"Playwright Fetch Error: {e}")
        await safe_reply(update, "❌ 获取失败，请重试。")

@log_interaction
async def get_android_specific_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_chat_allowed(context, update.message.chat_id): return
    apk_template = context.bot_data.get("apk_url")
    if not apk_template: return
    try:
        random_sub = generate_android_specific_subdomain()
        final_url = apk_template.replace("*", random_sub, 1)
        msg = f"✅ <b>您的专属安卓专用链接已生成！</b>\n👇 <b>点击下方链接即可复制：</b>\n<code>{final_url}</code>\n💡 <i>请务必在手机自带浏览器中打开</i>"
        await safe_reply(update, msg, parse_mode='HTML')
    except Exception: pass

@log_interaction
async def send_static_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, html_msg: str):
    if not update.message or not is_chat_allowed(context, update.message.chat_id): return
    await safe_reply(update, html_msg, parse_mode='HTML')

from io import BytesIO

@log_interaction
async def send_global_media(update: Update, context: ContextTypes.DEFAULT_TYPE, is_video=False):
    if not update.message: return
    key = update.message.text.strip()
    url = GLOBAL_VIDEO_MAP.get(key) if is_video else GLOBAL_IMAGE_MAP.get(key)
    if not url: return

    try:
        if is_video:
            # 🔥 核心修复：直接把 URL 传给 Telegram，不经过本地内存下载！
            # 兼容把 mp3 / m4a / wav / ogg 放在 VIDEO_x_URL 里的情况
            clean_path = urlparse(url).path.lower()
            if clean_path.endswith((".mp3", ".m4a", ".wav", ".ogg")):
                await update.message.reply_audio(audio=url, caption=f"🎬 视频：{key}")
            else:
                await update.message.reply_video(video=url, caption=f"🎬 视频：{key}")
        else:
            await update.message.reply_photo(photo=url)
    except Exception as e:
        logger.error(f"媒体发送异常: {e}")
        await update.message.reply_text("⚠️ 文件处理失败，可能是视频太大导致 Telegram 拒绝、链接失效或网络波动。")

# ==============================================================================
# 6. 新增：Potato 机器人完整逻辑
# ==============================================================================

async def potato_request(token, method, payload):
    url = f"https://api.potato.im/bot{token}/{method}"
    try:
        if GLOBAL_HTTP_CLIENT is None: return None
        resp = await GLOBAL_HTTP_CLIENT.post(url, json=payload, timeout=20.0)
        return resp.json()
    except Exception as e:
        logger.error(f"Potato API {method} 失败: {e}")
        return None

async def handle_potato_update(bot_index, token, update, api_url, apk_url):
    if "message" not in update or "text" not in update["message"]: return
    msg = update["message"]
    chat_id = msg["chat"]["id"]
    text = msg["text"].strip()

    # --- 1. 通用链接 ---
    if re.match(UNIVERSAL_COMMAND_PATTERN, text):
        await potato_request(token, "sendMessage", {"chat_id": chat_id, "text": "正在为您获取专属通用下载链接，请稍候 ..."})
        if not api_url:
            await potato_request(token, "sendMessage", {"chat_id": chat_id, "text": "❌ 配置错误：未找到 API。"})
            return
        try:
            final_url = await fetch_universal_link_core(api_url)
            res = f"✅ <b>您的专属通用下载链接已生成！</b>\n👇 <b>点击下方链接即可复制：</b>\n<code>{final_url}</code>\n💡 <i>请务必在手机自带浏览器中打开</i>"
            await potato_request(token, "sendMessage", {"chat_id": chat_id, "text": res, "parse_mode": "HTML"})
        except Exception as e:
            await potato_request(token, "sendMessage", {"chat_id": chat_id, "text": "❌ 获取失败，请重试。"})

    # --- 2. 安卓专用 ---
    elif re.match(ANDROID_SPECIFIC_COMMAND_PATTERN, text):
        if not apk_url: return
        random_sub = generate_android_specific_subdomain()
        final_url = apk_url.replace("*", random_sub, 1)
        res = f"✅ <b>您的专属安卓专用链接已生成！</b>\n👇 <b>点击下方链接即可复制：</b>\n<code>{final_url}</code>\n💡 <i>请务必在手机自带浏览器中打开</i>"
        await potato_request(token, "sendMessage", {"chat_id": chat_id, "text": res, "parse_mode": "HTML"})

    # --- 3. 静态回复 (还原原版文字) ---
    elif re.match(IOS_QUIT_PATTERN, text):
        res = "📱 <b>苹果手机APP大退步骤</b>\n\n1. 上滑停留调出后台。\n2. 上滑关闭App卡片。\n3. 重新点击图标打开。"
        await potato_request(token, "sendMessage", {"chat_id": chat_id, "text": res, "parse_mode": "HTML"})
        
    elif re.match(ANDROID_QUIT_PATTERN, text):
        res = "🤖 <b>安卓手机APP大退步骤</b>\n\n1. 上滑或点击多任务键进入后台。\n2. 上滑关闭App卡片。\n3. 重新打开App。"
        await potato_request(token, "sendMessage", {"chat_id": chat_id, "text": res, "parse_mode": "HTML"})
        
    elif re.match(ANDROID_BROWSER_PATTERN, text):
        res = "🤖 <b>安卓浏览器设置手机版</b>\n\n1. 打开浏览器菜单(≡或⋮)。\n2. 找到“桌面版”或“电脑模式”。\n3. <b>取消勾选</b>它。"
        await potato_request(token, "sendMessage", {"chat_id": chat_id, "text": res, "parse_mode": "HTML"})
        
    elif re.match(IOS_BROWSER_PATTERN, text):
        res = "📱 <b>苹果浏览器设置手机版</b>\n\n1. 点击地址栏左侧(大小/AA)。\n2. 选择“请求移动网站”。\n(如果显示“请求桌面网站”则无需操作)"
        await potato_request(token, "sendMessage", {"chat_id": chat_id, "text": res, "parse_mode": "HTML"})
        
    elif re.match(ANDROID_TAB_LIMIT_PATTERN, text):
        res = "🤖 <b>安卓窗口上限解决</b>\n\n1. 点击浏览器标签页图标(数字框)。\n2. 选择“关闭所有标签页”或手动关闭旧标签。"
        await potato_request(token, "sendMessage", {"chat_id": chat_id, "text": res, "parse_mode": "HTML"})
        
    elif re.match(IOS_TAB_LIMIT_PATTERN, text):
        res = "📱 <b>苹果窗口上限解决</b>\n\n1. 长按右下角标签图标。\n2. 选择“关闭所有标签页”。"
        await potato_request(token, "sendMessage", {"chat_id": chat_id, "text": res, "parse_mode": "HTML"})
        
    elif re.match(DOWNLOAD_HELP_PATTERN, text):
        res = (
            "🤖 <b>获取 APP 最新下载链接方式</b>\n\n"
            "📱 <b>通用链接 (苹果/安卓)</b>\n"
            "✅ 含落地引导页，发送下方任一词：\n"
            "🔴<code>链接</code>  🔴<code>苹果链接</code>  🔴<code>安卓链接</code>\n"
            "〰️〰️〰️〰️〰️〰️〰️〰️\n"
            "📦 <b>安卓专用 (安装包直连)</b>\n"
            "❌ 无落地页，发送下方任一词：\n"
            "🔴<code>提包</code>  🔴<code>安卓专用</code>\n\n"
            "💡 <i>说明：每次重新获取，有效时间为半小时左右！</i>"
        )
        await potato_request(token, "sendMessage", {"chat_id": chat_id, "text": res, "parse_mode": "HTML"})

async def potato_worker_loop(bot_index, token, api_url, apk_url):
    offset = 0
    logger.info(f"🚀 Potato Bot #{bot_index} 启动轮询中...")
    while True:
        try:
            url = f"https://api.potato.im/bot{token}/getUpdates?offset={offset}&timeout=30"
            if GLOBAL_HTTP_CLIENT is None:
                await asyncio.sleep(1)
                continue
            resp = await GLOBAL_HTTP_CLIENT.get(url, timeout=40.0)
            if resp.status_code == 200:
                data = resp.json()
                for update in data.get("result", []):
                    await handle_potato_update(bot_index, token, update, api_url, apk_url)
                    offset = update["update_id"] + 1
        except Exception as e:
            logger.error(f"Potato #{bot_index} 轮询出错: {e}")
            await asyncio.sleep(10) # 报错缓一缓
        await asyncio.sleep(0.5)

# ==============================================================================
# 7. 🔥 计算器/AI/定时任务 逻辑
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

@log_interaction
async def get_ipo_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not BROWSER_INSTANCE: return await safe_reply(update, "❌ 浏览器未就绪")
    await safe_reply(update, "🔍 正在检索并筛选最新新股...")
    async with BROWSER_LOCK:
        page = None
        try:
            page = await BROWSER_INSTANCE.new_page()
            await page.goto("https://vip.stock.finance.sina.com.cn/corp/go.php/vRPD_NewStockIssue/page/1.phtml", timeout=30000)
            try: await page.wait_for_selector("#NewStockTable", state="visible", timeout=10000)
            except: pass 

            rows_data = await page.evaluate('''() => {
                const rows = Array.from(document.querySelectorAll('#NewStockTable tr')).slice(2); 
                return rows.slice(0, 30).map(tr => {
                    const tds = tr.querySelectorAll('td');
                    if (tds.length < 5) return null; 
                    return { code: tds[0].innerText.trim(), name: tds[2].innerText.trim(), sub_date: tds[3].innerText.trim(), list_date: tds[4].innerText.trim() };
                }).filter(item => item !== null);
            }''')

            valid_rows = []
            today = date.today()
            if rows_data:
                for item in rows_data:
                    if "代码" in item['code'] or "简称" in item['name']: continue
                    l_str = item['list_date']
                    keep = False
                    if l_str == "-" or not l_str: keep = True
                    else:
                        try:
                            if datetime.strptime(l_str, "%Y-%m-%d").date() >= today: keep = True
                        except: keep = True
                    if keep: valid_rows.append(item)
                valid_rows = valid_rows[:15]

            if valid_rows:
                msg_lines = ["🔔 <b>近期新股日历 (从今日起)</b>\n• 证券代码 证券简称 申购日 / 上市日"]
                for item in valid_rows:
                    l_date = item['list_date'] if item['list_date'] else "-"
                    s_date = item['sub_date'] if item['sub_date'] else "-"
                    msg_lines.append(f"• <code>{item['code']}</code> {item['name']} {s_date} / {l_date}")
                await safe_reply(update, "\n".join(msg_lines), parse_mode='HTML')
            else:
                await safe_reply(update, "⚠️ 近期没有待上市的新股。")
        except Exception as e:
            await safe_reply(update, f"查询失败: {e}")
        finally:
            if page: await page.close()

async def generate_digest_content() -> str:
    entries = []
    try:
        tasks = [asyncio.to_thread(feedparser.parse, u) for u in DEFAULT_RSS_FEEDS]
        results = await asyncio.gather(*tasks)
        for f in results: entries.extend(f.entries[:5])
    except: return "📭 获取新闻失败"
    if not entries: return "📭 无新闻更新"
    
    content = "\n".join([f"- {e.title} ({e.link})" for e in entries])
    prompt = f"你是一名资深科技主编。请从以下素材中筛选10条重要新闻，分类为AI、数码、商业、深度。用中文一句话解读。素材：\n{content}"
    res = await call_gemini(prompt)
    return f"📅 <b>今日科技内参</b>\n\n{res}"

@log_interaction
async def get_daily_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update, "☕️ 正在搜集新闻并生成简报...")
    content = await generate_digest_content()
    await safe_reply(update, content, parse_mode='HTML')

async def auto_send_digest(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data.get('chat_id') if context.job.data else None
    if not chat_id: return
    try:
        await context.bot.send_message(chat_id=chat_id, text=await generate_digest_content(), parse_mode='HTML')
        logger.info("✅ 定时日报发送成功")
    except Exception as e:
        logger.error(f"❌ 定时发送失败: {e}")

async def send_scheduled_worker_message(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    if not job_data: return
    clean_text = job_data['text'].replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    chat_id = job_data['chat_id']
    try:
        await context.bot.send_message(chat_id=chat_id, text=clean_text, parse_mode='HTML')
        logger.info(f"✅ 工兵定时消息已发送到 {chat_id}")
    except BadRequest as e:
        logger.warning(f"⚠️ HTML 解析失败，降级纯文本...")
        try: await context.bot.send_message(chat_id=chat_id, text=clean_text)
        except Exception as e2: logger.error(f"❌ 定时消息彻底失败: {e2}")

def do_calc(text):
    if ':' in text: return None
    try:
        clean = re.sub(r'[^\d\+\-\*\/\(\)\.\%]', '', text)
        if not clean: return None
        res = simple_eval(clean)
        return int(res) if float(res).is_integer() else res
    except: return None

# ==============================================================================
# 8. Bot Setup
# ==============================================================================

def setup_worker_bot(app_instance: Application, bot_index: int) -> None:
    token_end = app_instance.bot.token[-4:]
    @log_interaction
    async def start(u, c): await safe_reply(u, f"🤖 工兵 #{bot_index} ({token_end}) 就绪。")
    app_instance.add_handler(CommandHandler("start", start))
    
    t_download_help = (
        "🤖 <b>获取 APP 最新下载链接方式</b>\n\n"
        "📱 <b>通用链接 (苹果/安卓)</b>\n"
        "✅ 含落地引导页，发送下方任一词：\n"
        "🔴<code>链接</code>  🔴<code>苹果链接</code>  🔴<code>安卓链接</code>\n"
        "〰️〰️〰️〰️〰️〰️〰️〰️\n"
        "📦 <b>安卓专用 (安装包直连)</b>\n"
        "❌ 无落地页，发送下方任一词：\n"
        "🔴<code>提包</code>  🔴<code>安卓专用</code>\n\n"
        "💡 <i>说明：每次重新获取，有效时间为半小时左右！</i>"
    )
    app_instance.add_handler(MessageHandler(filters.Regex(DOWNLOAD_HELP_PATTERN), lambda u,c: send_static_reply(u,c,t_download_help)))

    @log_interaction
    async def query_ip(u, c):
        if not u.message.text: return
        target_ip = re.sub(r"^(查|IP定位)\s*", "", u.message.text.strip()).strip()
        await safe_reply(u, f"🔍 正在查询 IP: {target_ip} ...")
        try:
            resp = await GLOBAL_HTTP_CLIENT.get(f"http://ip-api.com/json/{target_ip}?lang=zh-CN")
            data = resp.json()
            if data.get('status') != 'success': return await safe_reply(u, f"❌ 查询失败: {data.get('message', '未知错误')}")
            
            msg = (
                f"🌍 <b>IP定位结果</b>\nIP: <code>{data.get('query', target_ip)}</code>\n"
                f"位置: {data.get('country', '')} {data.get('regionName', '')} {data.get('city', '')}\n"
                f"运营商/组织: {data.get('isp', '')}\nASN: {data.get('as', '')}\n时区: {data.get('timezone', '')}\n"
                f"地图: <a href=\"https://www.google.com/maps?q={data.get('lat', 0)},{data.get('lon', 0)}\">Google Maps</a>"
            )
            await safe_reply(u, msg, parse_mode='HTML')
        except Exception as e: await safe_reply(u, "❌ 查询出错，请稍后重试。")

    app_instance.add_handler(MessageHandler(filters.Regex(IP_QUERY_PATTERN), query_ip))
    app_instance.add_handler(MessageHandler(filters.Regex(UNIVERSAL_COMMAND_PATTERN), get_universal_link))
    app_instance.add_handler(MessageHandler(filters.Regex(ANDROID_SPECIFIC_COMMAND_PATTERN), get_android_specific_link))

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

    # 采用标准的异步函数包装，防止任务被框架吞掉
    async def trigger_image(u, c): 
        await send_global_media(u, c, False)
    async def trigger_video(u, c): 
        await send_global_media(u, c, True)

    if GLOBAL_IMAGE_PATTERN: 
        app_instance.add_handler(MessageHandler(filters.Regex(GLOBAL_IMAGE_PATTERN), trigger_image))
    if GLOBAL_VIDEO_PATTERN: 
        app_instance.add_handler(MessageHandler(filters.Regex(GLOBAL_VIDEO_PATTERN), trigger_video))

def setup_calculator_bot(app_instance: Application) -> None:
    @log_interaction
    async def start(u, c): await safe_reply(u, "👋 我是智能计算器。\n功能：计算、新股、日报、IP定位、查U、/book、/quote")
    app_instance.add_handler(CommandHandler("start", start))
    
    @log_interaction
    async def query_ip(u, c):
        if not u.message.text: return
        target_ip = re.sub(r"^(查|IP定位)\s*", "", u.message.text.strip()).strip()
        await safe_reply(u, f"🔍 正在查询 IP: {target_ip} ...")
        try:
            resp = await GLOBAL_HTTP_CLIENT.get(f"http://ip-api.com/json/{target_ip}?lang=zh-CN")
            data = resp.json()
            if data.get('status') != 'success': return await safe_reply(u, f"❌ 查询失败: {data.get('message', '未知错误')}")
            msg = (
                f"🌍 <b>IP定位结果</b>\nIP: <code>{data.get('query')}</code>\n"
                f"位置: {data.get('country')} {data.get('regionName')} {data.get('city')}\n"
                f"地图: <a href=\"https://www.google.com/maps?q={data.get('lat')},{data.get('lon')}\">Google Maps</a>"
            )
            await safe_reply(u, msg, parse_mode='HTML')
        except Exception: await safe_reply(u, "❌ 查询出错")

    @log_interaction
    async def query_usdt(u, c):
        if not u.message.text: return
        try: address = u.message.text.strip().replace("查", "").strip()
        except: return
        if not address.startswith("T") or len(address) != 34: return await safe_reply(u, "⚠️ 地址格式不对，请输入正确的 TRC20 地址。")
        await safe_reply(u, f"🔗 正在查询链上数据: {address} ...")
        try:
            api_key = os.getenv("TRONSCAN_API_KEY", "")
            headers = { "User-Agent": "Mozilla/5.0", "TRON-PRO-API-KEY": api_key }
            balance_url = f"https://apilist.tronscanapi.com/api/account/tokens?address={address}&start=0&limit=20&hidden=0&show=0&sortType=0"
            usdt_contract = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
            transfer_url = f"https://apilist.tronscanapi.com/api/token_trc20/transfers?limit=10&start=0&sort=-timestamp&count=true&relatedAddress={address}&contract_address={usdt_contract}"
            
            resp_bal, resp_trans = await asyncio.gather(GLOBAL_HTTP_CLIENT.get(balance_url, headers=headers), GLOBAL_HTTP_CLIENT.get(transfer_url, headers=headers))
            if resp_bal.status_code != 200: return await safe_reply(u, f"❌ 查询被拦截。")

            usdt_balance = 0.0
            for t in resp_bal.json().get('data', []):
                if t.get('tokenId') == usdt_contract or t.get('tokenAbbr') == 'USDT':
                    usdt_balance = float(t.get('balance', 0)) / 1000000; break
            
            trans_lines = []
            transfers = resp_trans.json().get('token_transfers', [])
            if not transfers: trans_lines.append("暂无近 10 笔 USDT 记录")
            else:
                for tx in transfers[:6]:
                    if tx.get('contract_address') != usdt_contract: continue
                    is_in = tx.get('to_address') == address
                    arrow = "🟢收" if is_in else "🔴转"
                    amt_str = "{:,.2f}".format(float(tx.get('quant', 0)) / 1000000)
                    time_str = datetime.fromtimestamp(int(tx.get('block_ts', 0)) / 1000, timezone(timedelta(hours=8))).strftime('%m-%d %H:%M')
                    other = tx.get('from_address') if is_in else tx.get('to_address')
                    trans_lines.append(f"{arrow} {amt_str} | {other[:4]}...{other[-4:]} | {time_str}")

            msg = f"💰 <b>钱包查询结果</b>\n地址: <code>{address}</code>\n💎 <b>USDT余额:</b> <code>{usdt_balance:,.2f}</code>\n\n📋 <b>最近流向:</b>\n" + "\n".join(trans_lines)
            await safe_reply(u, msg, parse_mode='HTML')
        except Exception as e: await safe_reply(u, f"❌ 查询失败: {e}")

    app_instance.add_handler(MessageHandler(filters.Regex(IP_QUERY_PATTERN), query_ip))
    app_instance.add_handler(MessageHandler(filters.Regex(r"^查\s*T[a-zA-Z0-9]{33}$"), query_usdt))

    @log_interaction
    async def book(u,c): 
        q = " ".join(c.args)
        if not q: return await safe_reply(u, "请加关键词")
        await safe_reply(u, "📚 正在寻找好书...") 
        await safe_reply(u, await call_gemini(f"推荐3本关于{q}的书，带理由和摘抄"), parse_mode='Markdown')
    
    @log_interaction
    async def quote(u,c): 
        q = " ".join(c.args)
        await safe_reply(u, "📜 正在翻阅名著摘录金句...") 
        await safe_reply(u, await call_gemini(f"从《{q}》或经典名著找一段摘抄并赏析"), parse_mode='Markdown')
    
    @log_interaction
    async def deep(u,c): 
        q = " ".join(c.args)
        if not q: return await safe_reply(u, "请加话题")
        await safe_reply(u, "🧠 正在深度思考...") 
        await safe_reply(u, await call_gemini(f"深度解析话题：{q}"), parse_mode='Markdown')

    app_instance.add_handler(CommandHandler("book", book))
    app_instance.add_handler(CommandHandler("quote", quote))
    app_instance.add_handler(CommandHandler("deep", deep))
    app_instance.add_handler(MessageHandler(filters.Regex(IPO_COMMAND_PATTERN), get_ipo_info))
    app_instance.add_handler(MessageHandler(filters.Regex(DIGEST_COMMAND_PATTERN), get_daily_digest))

    @log_interaction
    async def calc(u,c):
        text = u.message.text
        if not text: return
        if u.message.reply_to_message and u.message.reply_to_message.text:
            match = re.search(r'🔢\s*([0-9\.]+)', u.message.reply_to_message.text.replace(',', ''))
            if match and text.strip()[0] in ['+', '-', '*', '/']:
                res = do_calc(f"{match.group(1)}{text.strip()}")
                if res is not None: return await safe_reply(u, f"🔢 {res}")
        res = do_calc(text)
        if res is not None: await safe_reply(u, f"🔢 {res}")

    app_instance.add_handler(MessageHandler(filters.TEXT | filters.COMMAND, calc))

# ==============================================================================
# 9. 启动入口
# ==============================================================================
app = FastAPI()

from fastapi.responses import HTMLResponse
from fastapi import Query
import os

# ==============================================================================
# 🔥 网页版获取链接 (带暗号防御 + 自动复制 + 实时生成时间戳)
# ==============================================================================

@app.get("/web/{bot_index}", response_class=HTMLResponse)
async def web_portal(bot_index: int, key: str = Query(None)):
    """渲染网页前端界面"""
    
    # 1. 安全防御：暗号校验
    SECRET_KEY = os.getenv("WEB_SECRET", "666")
    if key != SECRET_KEY:
        return HTMLResponse("<h2 style='text-align:center; margin-top:50px; color:#ff3b30;'>⛔️ 访问被拒绝：无效的安全验证码。</h2>")

    api_url = os.getenv(f"BOT_{bot_index}_API_URL")
    if not api_url:
        return HTMLResponse("<h1>❌ 找不到该线路配置，请检查 Render 环境变量。</h1>")

    # 网页 HTML+CSS 代码
    html_content = f"""
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>专属链接获取中心 - 线路 {bot_index}</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background-color: #f4f7f6; display: flex; flex-direction: column; align-items: center; padding: 40px 20px; margin: 0; }}
            .container {{ background: white; padding: 30px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.05); width: 100%; max-width: 400px; text-align: center; }}
            h2 {{ color: #333; margin-top: 0; }}
            p {{ color: #666; font-size: 14px; margin-bottom: 25px; }}
            .btn {{ display: block; width: 100%; padding: 15px; margin: 15px 0; font-size: 16px; font-weight: bold; color: white; border: none; border-radius: 8px; cursor: pointer; transition: background 0.3s; box-sizing: border-box; }}
            .btn-uni {{ background-color: #007aff; }}
            .btn-uni:hover {{ background-color: #005bb5; }}
            .btn-apk {{ background-color: #34c759; }}
            .btn-apk:hover {{ background-color: #248a3d; }}
            .btn:disabled {{ background-color: #ccc !important; cursor: not-allowed; }}
            #result-box {{ margin-top: 25px; padding: 15px; border-radius: 8px; background-color: #f8f9fa; border: 1px solid #e9ecef; display: none; word-break: break-all; text-align: left; }}
            .success-link {{ color: #007aff; font-weight: bold; font-size: 18px; text-decoration: none; display: block; margin: 10px 0; }}
            .toast {{ position: fixed; top: 20px; left: 50%; transform: translateX(-50%); background: rgba(0,0,0,0.8); color: white; padding: 10px 20px; border-radius: 20px; font-size: 14px; display: none; z-index: 1000; }}
        </style>
    </head>
    <body>
        <div id="toast" class="toast">✅ 已自动复制到剪贴板</div>
        <div class="container">
            <h2>🚀 专属获取中心 (线路 {bot_index})</h2>
            <p>点击下方按钮，系统将实时为您生成最新地址</p>
            
            <button class="btn btn-uni" id="btn-uni" onclick="fetchLink('uni')">🍏 🤖 获取通用链接 (带落地页)</button>
            <button class="btn btn-apk" id="btn-apk" onclick="fetchLink('apk')">📦 获取安卓专用 (提包直连)</button>

            <div id="result-box"></div>
        </div>

        <script>
            function showToast(msg) {{
                const toast = document.getElementById('toast');
                toast.innerText = msg;
                toast.style.display = 'block';
                setTimeout(() => {{ toast.style.display = 'none'; }}, 2000);
            }}

            async function copyToClipboard(text) {{
                try {{
                    if (navigator.clipboard && window.isSecureContext) {{
                        await navigator.clipboard.writeText(text);
                    }} else {{
                        // 兼容老版本浏览器
                        let textArea = document.createElement("textarea");
                        textArea.value = text;
                        textArea.style.position = "fixed";
                        textArea.style.left = "-999999px";
                        textArea.style.top = "-999999px";
                        document.body.appendChild(textArea);
                        textArea.focus();
                        textArea.select();
                        document.execCommand('copy');
                        textArea.remove();
                    }}
                    showToast("✅ 已自动复制到剪贴板");
                }} catch (err) {{
                    console.error('复制失败', err);
                    showToast("⚠️ 自动复制失败，请长按链接手动复制");
                }}
            }}

            async function fetchLink(type) {{
                const btnUni = document.getElementById('btn-uni');
                const btnApk = document.getElementById('btn-apk');
                const resultBox = document.getElementById('result-box');
                const activeBtn = type === 'uni' ? btnUni : btnApk;
                
                btnUni.disabled = true;
                btnApk.disabled = true;
                const originalText = activeBtn.innerText;
                activeBtn.innerText = "⏳ 正在拉取底层数据，请稍候...";
                resultBox.style.display = "none";

                try {{
                    const response = await fetch(`/api/get_link/{bot_index}/` + type);
                    const data = await response.json();
                    
                    resultBox.style.display = "block";
                    if (data.status === "success") {{
                        // 获取当前时间并格式化
                        const now = new Date();
                        const timeString = now.getFullYear() + "-" + 
                                         String(now.getMonth() + 1).padStart(2, '0') + "-" + 
                                         String(now.getDate()).padStart(2, '0') + " " + 
                                         String(now.getHours()).padStart(2, '0') + ":" + 
                                         String(now.getMinutes()).padStart(2, '0') + ":" + 
                                         String(now.getSeconds()).padStart(2, '0');

                        // 更新后的文案，加入了时间显示
                        resultBox.innerHTML = `✅ <b>生成成功！</b><br>
                        <span style="font-size:12px; color:#999; display:block; margin-top:5px;">生成时间：${{timeString}}</span>
                        <a class="success-link" href="${{data.url}}" target="_blank">${{data.url}}</a>
                        <span style="font-size:13px; color:#666; display:block; margin-top:8px;">💡 提示：请在手机自带浏览器中打开生成的链接，每次重新获取，有效时间为半小时左右！</span>`;
                        
                        // 调用自动复制
                        await copyToClipboard(data.url);
                    }} else {{
                        resultBox.innerHTML = `❌ <b>生成失败：</b>${{data.error}}`;
                    }}
                }} catch (error) {{
                    resultBox.style.display = "block";
                    resultBox.innerHTML = `❌ <b>网络请求错误，请刷新重试。</b>`;
                }} finally {{
                    btnUni.disabled = false;
                    btnApk.disabled = false;
                    activeBtn.innerText = originalText;
                }}
            }}
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


@app.get("/api/get_link/{bot_index}/{link_type}")
async def api_generate_link(bot_index: int, link_type: str):
    """处理网页端的获取请求"""
    api_url = os.getenv(f"BOT_{bot_index}_API_URL")
    apk_url = os.getenv(f"BOT_{bot_index}_APK_URL")

    if link_type == "uni":
        if not api_url: return {"status": "error", "error": "服务器未配置 API URL"}
        try:
            # 完美复用你之前提取出来的核心 Playwright 逻辑！
            final_url = await fetch_universal_link_core(api_url)
            return {"status": "success", "url": final_url}
        except Exception as e:
            logger.error(f"网页端获取通用链接失败: {e}")
            return {"status": "error", "error": "抓取超时或失败，请重试"}

    elif link_type == "apk":
        if not apk_url: return {"status": "error", "error": "服务器未配置 APK URL"}
        random_sub = generate_android_specific_subdomain()
        final_url = apk_url.replace("*", random_sub, 1)
        return {"status": "success", "url": final_url}

    return {"status": "error", "error": "未知的请求类型"}
    
@app.get("/")
async def root(): return {"status": "ok", "msg": "Bot Service is Running (Shield + Potato Ready)"}

@app.on_event("startup")
async def startup_event():
    global GLOBAL_HTTP_CLIENT, PLAYWRIGHT_INSTANCE, BROWSER_INSTANCE
    GLOBAL_HTTP_CLIENT = httpx.AsyncClient(timeout=30.0, verify=False)
    
    try:
        logger.info("🚀 Starting Playwright...")
        PLAYWRIGHT_INSTANCE = await async_playwright().start()
        BROWSER_INSTANCE = await PLAYWRIGHT_INSTANCE.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
        app.state.browser = BROWSER_INSTANCE
        logger.info("✅ System Ready: Playwright Started")
    except Exception as e: logger.error(f"❌ System Start Error (Playwright): {e}")

    global GLOBAL_IMAGE_MAP, GLOBAL_IMAGE_PATTERN, GLOBAL_VIDEO_MAP, GLOBAL_VIDEO_PATTERN
    for i in range(1, 11):
        k = os.getenv(f"IMAGE_{i}_KEYS", "").strip()
        v = os.getenv(f"IMAGE_{i}_URL", "").strip()
        if k and v: 
            for key in k.split(','): GLOBAL_IMAGE_MAP[key.strip()] = v
        k = os.getenv(f"VIDEO_{i}_KEYS", "").strip()
        v = os.getenv(f"VIDEO_{i}_URL", "").strip()
        if k and v: 
            for key in k.split(','): GLOBAL_VIDEO_MAP[key.strip()] = v
    if GLOBAL_IMAGE_MAP: GLOBAL_IMAGE_PATTERN = r"^(" + "|".join([re.escape(k) for k in GLOBAL_IMAGE_MAP.keys()]) + r")$"
    if GLOBAL_VIDEO_MAP: GLOBAL_VIDEO_PATTERN = r"^(" + "|".join([re.escape(k) for k in GLOBAL_VIDEO_MAP.keys()]) + r")$"

    active_tokens = set()

    # --- 启动工兵 (Telegram 1-10) 及自动挂载 Potato 任务 ---
    for i in range(1, 11):
        # 1. 启动 Telegram
        raw_token = os.getenv(f"BOT_TOKEN_{i}")
        if raw_token and len(raw_token.strip()) > 10 and raw_token.strip() not in active_tokens:
            token = raw_token.strip() 
            try:
                bot = Application.builder().token(token).build()
                bot.bot_data["fastapi_app"] = app
                bot.bot_data["bot_index"] = i
                
                api_url = os.getenv(f"BOT_{i}_API_URL", "").strip()
                apk_url = os.getenv(f"BOT_{i}_APK_URL", "").strip()
                
                if api_url: bot.bot_data["api_url"] = api_url
                if apk_url: bot.bot_data["apk_url"] = apk_url
                if al := os.getenv(f"BOT_{i}_ALLOWED_CHAT_IDS", "").strip(): 
                    bot.bot_data["allowed_chats"] = [c.strip() for c in al.split(',')]
                
                await bot.initialize()
                setup_worker_bot(bot, i) 
                BOT_APPLICATIONS[f"bot{i}_webhook"] = bot
                
                schedule_chat_id = os.getenv(f"BOT_{i}_SCHEDULE_CHAT_ID")
                schedule_msg = os.getenv(f"BOT_{i}_SCHEDULE_MESSAGE")
                if schedule_chat_id and schedule_msg and (schedule_times := os.getenv(f"BOT_{i}_SCHEDULE_TIMES_UTC")):
                    for t_str in schedule_times.split(','):
                        try:
                            h, m = map(int, t_str.strip().split(':'))
                            bot.job_queue.run_daily(send_scheduled_worker_message, time=time(hour=h, minute=m), data={'chat_id': schedule_chat_id, 'text': schedule_msg})
                        except ValueError: pass

                await bot.start()
                try:
                    await bot.updater.start_polling(drop_pending_updates=True)
                    logger.info(f"✅ TG Worker {i} Started Polling")
                except Conflict: logger.warning(f"🛡️ 触发盾牌: Worker {i} Conflict")
                except Exception as e: logger.error(f"❌ Worker {i} Polling Error: {e}")

                active_tokens.add(token)
            except Exception as e: logger.error(f"❌ Worker {i} 启动失败: {e}")
            
        # 2. 🔥 启动 Potato 机器人协程
        potato_token = os.getenv(f"POTATO_TOKEN_{i}")
        if potato_token and potato_token.strip():
            api_url = os.getenv(f"BOT_{i}_API_URL", "").strip()
            apk_url = os.getenv(f"BOT_{i}_APK_URL", "").strip()
            asyncio.create_task(potato_worker_loop(i, potato_token.strip(), api_url, apk_url))
            logger.info(f"✅ Potato Bot #{i} 任务已挂载")

    # --- 启动计算器 ---
    raw_calc_token = os.getenv("CALC_BOT_TOKEN")
    if raw_calc_token and raw_calc_token.strip() not in active_tokens:
        calc_token = raw_calc_token.strip()
        try:
            c_bot = Application.builder().token(calc_token).build()
            await c_bot.initialize()
            setup_calculator_bot(c_bot)
            await c_bot.start()
            if target_chat_id := os.getenv("CALC_CHAT_ID"):
                c_bot.job_queue.run_daily(auto_send_digest, time=time(hour=0, minute=0), data={'chat_id': target_chat_id})

            try:
                await c_bot.updater.start_polling(drop_pending_updates=True)
                logger.info(f"✅ Calc Bot Started Polling")
            except Conflict: pass
            except Exception as e: logger.error(f"❌ Calc Bot Polling Error: {e}")

            BOT_APPLICATIONS["calc"] = c_bot
            active_tokens.add(calc_token)
        except Exception as e: logger.error(f"❌ Calc Bot 启动失败: {e}")

@app.on_event("shutdown")
async def shutdown():
    logger.info("Starting graceful shutdown...")
    for b in BOT_APPLICATIONS.values():
        try:
            if b.updater and b.updater.running: await b.updater.stop()
            if b.running: await b.stop()
            await b.shutdown()
        except Exception: pass
    if GLOBAL_HTTP_CLIENT: await GLOBAL_HTTP_CLIENT.aclose()
    if BROWSER_INSTANCE: await BROWSER_INSTANCE.close()
    if PLAYWRIGHT_INSTANCE: await PLAYWRIGHT_INSTANCE.stop()
    logger.info("Shutdown complete.")
