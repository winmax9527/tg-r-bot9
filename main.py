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
# 下面这几个字典仅做辅助，核心逻辑已改为 bot_data 存储
BOT_APK_URLS: Dict[str, str] = {} 
BOT_ALLOWED_CHATS: Dict[str, List[str]] = {}
PLAYWRIGHT_INSTANCE: Playwright | None = None
BROWSER_INSTANCE: Browser | None = None
GLOBAL_HTTP_CLIENT: httpx.AsyncClient | None = None

# ⚠️ 并发锁：防止内存溢出
BROWSER_LOCK = asyncio.Semaphore(3)

# RSS 源
DEFAULT_RSS_FEEDS = [
    "https://36kr.com/feed",
    "https://www.cnbeta.com.tw/backend.php",
    "https://www.ithome.com/rss/",
    "https://sspai.com/feed",
    "http://www.zhihudaily.com/#/index",
]

# 正则
UNIVERSAL_COMMAND_PATTERN = r"^(苹果专属链接|苹果专用地址|苹果专用地址|苹果专用|苹果连接|安卓连接|连接|地址|安装地址|安装链接|下载地址|下载链接|最新地址|安卓地址|苹果地址|安卓下载地址|苹果专用链接|苹果下载地址|链接|最新链接|安卓链接|安卓下载链接|最新安卓链接|苹果链接|苹果下载链接|ios链接|最新苹果链接)$"
ANDROID_SPECIFIC_COMMAND_PATTERN = r"^(安卓专属链接|安卓专属地址|安卓专属连接|安卓专属|安卓专属连接|提包|安卓专用|安卓专用链接|安卓提包链接|安卓专用地址|安卓提包地址|安卓专用下载|安卓提)$"
IOS_QUIT_PATTERN = r"^(苹果大退|苹果重启|苹果大退重启|苹果黑屏|苹果重开)$"
ANDROID_QUIT_PATTERN = r"^(安卓大退|安卓重启|安卓大退重启|安卓黑屏|安卓重开|大退|重开|闪退|卡了|黑屏)$"
ANDROID_BROWSER_PATTERN = r"^(安卓浏览器手机版|安卓桌面版|安卓浏览器|浏览器设置)$"
IOS_BROWSER_PATTERN = r"^(苹果浏览器手机版|苹果浏览器|苹果桌面版)$"
ANDROID_TAB_LIMIT_PATTERN = r"^(安卓窗口上限|窗口上限|标签上限)$"
IOS_TAB_LIMIT_PATTERN = r"^(苹果窗口上限|苹果标签上限)$"
IPO_COMMAND_PATTERN = r"^(新股|新股申购|新股上市|近期新股|申购|上市)$"
DIGEST_COMMAND_PATTERN = r"^(日报|简报|新闻|每日简报|科技新闻)$"
# 下载方式指引
DOWNLOAD_HELP_PATTERN = r"^(下载方式|下载说明)$"

# 🔥【智能IP正则】兼容 IPv4 和 IPv6
IP_QUERY_PATTERN = r"^(?:(?:查|IP定位)\s*[0-9a-fA-F:.]+|(?:\d{1,3}\.){3}\d{1,3}|(?:[0-9a-fA-F]{0,4}:){2,}[0-9a-fA-F:.]*)$"

GLOBAL_IMAGE_MAP: Dict[str, str] = {}
GLOBAL_IMAGE_PATTERN: str = ""
GLOBAL_VIDEO_MAP: Dict[str, str] = {}
GLOBAL_VIDEO_PATTERN: str = ""

# ==============================================================================
# 3. 辅助函数
# ==============================================================================

def is_chat_allowed(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    # 优先从 bot_data 获取白名单，如果没有则放行
    allowed_list = context.bot_data.get("allowed_chats", [])
    if not allowed_list: return True
    
    chat_id_str = str(chat_id)
    possible_ids = {chat_id_str}
    if chat_id_str.startswith("-100"): possible_ids.add(f"-{chat_id_str[4:]}")
    elif chat_id_str.startswith("-"): possible_ids.add(f"-100{chat_id_str[1:]}")
    
    for cid in possible_ids:
        if cid in allowed_list: return True
    return False

# 日志装饰器
def log_interaction(func):
    @wraps(func)
    async def wrapper(update, context, *args, **kwargs):
        if not update or not update.effective_user:
            return await func(update, context, *args, **kwargs)

        user = update.effective_user
        chat = update.effective_chat
        try:
            bot_username = context.bot.username or f"Bot{context.bot.id}"
        except:
            bot_username = "UnknownBot"
        
        message_text = update.message.text if update.message else "Action"
        
        log_msg_start = (
            f"🤖[{bot_username}] "
            f"👤{user.full_name or user.first_name}({user.id}) "
            f"🏠Chat:{chat.title or '私聊'}({chat.id}) "
            f"-> 📝Cmd: {message_text}"
        )
        logger.info(log_msg_start)

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
        if parse_mode:
            await update.message.reply_text(text, parse_mode=parse_mode)
        else:
            await update.message.reply_text(text)
    except BadRequest as e:
        logger.warning(f"Reply failed with {parse_mode}: {e} -> ⚠️ 正在降级为纯文本重发...")
        try:
            await update.message.reply_text(text)
        except Exception as e2:
            logger.error(f"Retry failed: {e2}")
    except Exception as e:
        logger.error(f"General Reply Error: {e}")

# ==============================================================================
# 4. 工兵逻辑
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

@log_interaction
async def get_universal_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_chat_allowed(context, update.message.chat_id): return

    # 🔥🔥🔥 修复核心：直接从 bot_data 获取 API URL，不再查全局字典 🔥🔥🔥
    api_url = context.bot_data.get("api_url")
    
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
            
            browser = context.bot_data.get("fastapi_app").state.browser if context.bot_data.get("fastapi_app") else None
            if not browser:
                 if BROWSER_INSTANCE: browser = BROWSER_INSTANCE
                 else: raise RuntimeError("Browser not ready")
            
            context_p = await browser.new_context(user_agent=user_agent)
            page = await context_p.new_page()
            
            await page.route("**/*", lambda route: route.abort() 
                if route.request.resource_type in ["image", "media", "font", "stylesheet"] 
                else route.continue_())
            
            try:
                await page.goto(domain_a, wait_until="domcontentloaded", timeout=25000)
            except Exception:
                pass

            try: await page.wait_for_timeout(3000)
            except: pass
            
            final_url_b = page.url
            if "chrome-error" in final_url_b: raise Exception("Chrome Error")

            rand_sub = generate_universal_subdomain()
            final_url = modify_url_subdomain(final_url_b, rand_sub)
            
            msg = f"✅ <b>您的专属通用下载链接已生成！</b>\n👇 <b>点击下方链接即可复制：</b>\n<code>{final_url}</code>\n💡 <i>请务必在手机自带浏览器中打开</i>"
            await safe_reply(update, msg, parse_mode='HTML')
            await context_p.close()
        except Exception as e:
            await safe_reply(update, "❌ 获取失败，请重试。")
            raise e
        finally:
            if page: await page.close()

@log_interaction
async def get_android_specific_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_chat_allowed(context, update.message.chat_id): return
    
    # 🔥🔥🔥 修复核心：直接从 bot_data 获取 APK URL 🔥🔥🔥
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

@log_interaction
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
# 5. 🔥 计算器/AI 逻辑
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
                    return {
                        code: tds[0].innerText.trim(),
                        name: tds[2].innerText.trim(),
                        sub_date: tds[3].innerText.trim(),
                        list_date: tds[4].innerText.trim()
                    };
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
                            l_date = datetime.strptime(l_str, "%Y-%m-%d").date()
                            if l_date >= today: keep = True
                        except: keep = True
                    if keep: valid_rows.append(item)
                valid_rows = valid_rows[:15]

            if valid_rows:
                msg_lines = ["🔔 <b>近期新股日历 (从今日起)</b>"]
                msg_lines.append("• 证券代码 证券简称 申购日 / 上市日") 
                for item in valid_rows:
                    l_date = item['list_date'] if item['list_date'] else "-"
                    s_date = item['sub_date'] if item['sub_date'] else "-"
                    line = f"• <code>{item['code']}</code> {item['name']} {s_date} / {l_date}"
                    msg_lines.append(line)
                final_text = "\n".join(msg_lines)
                await safe_reply(update, final_text, parse_mode='HTML')
            else:
                await safe_reply(update, "⚠️ 近期没有待上市的新股。")
        except Exception as e:
            await safe_reply(update, f"查询失败: {e}")
            raise e
        finally:
            if page: await page.close()

# 🔥 核心：生成日报内容的函数
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

# 手动触发日报
@log_interaction
async def get_daily_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update, "☕️ 正在搜集新闻并生成简报...")
    content = await generate_digest_content()
    await safe_reply(update, content, parse_mode='HTML')

# 🔥 计算器定时任务回调
async def auto_send_digest(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data.get('chat_id') if context.job.data else None
    if not chat_id: return
    content = await generate_digest_content()
    try:
        await context.bot.send_message(chat_id=chat_id, text=content, parse_mode='HTML')
        logger.info("✅ 定时日报发送成功")
    except Exception as e:
        logger.error(f"❌ 定时发送失败: {e}")

# 🔥🔥 【工兵Bot】定时发送回调 (重点修复：自动替换 br 和降级重试) 🔥🔥
async def send_scheduled_worker_message(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    if not job_data: return
    
    raw_text = job_data['text']
    # 1. 自动清洗 <br> 为换行符
    clean_text = raw_text.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    
    chat_id = job_data['chat_id']
    try:
        # 2. 尝试用 HTML 发送
        await context.bot.send_message(chat_id=chat_id, text=clean_text, parse_mode='HTML')
        logger.info(f"✅ 工兵定时消息已发送到 {chat_id}")
    except BadRequest as e:
        logger.warning(f"⚠️ HTML 解析失败 ({e})，正在降级为纯文本重试...")
        try:
            # 3. 失败则用纯文本发送 (保底)
            await context.bot.send_message(chat_id=chat_id, text=clean_text)
            logger.info(f"✅ 工兵定时消息 (纯文本) 已发送到 {chat_id}")
        except Exception as e2:
            logger.error(f"❌ 工兵定时消息发送彻底失败: {e2}")

# 纯文本计算核心 (防呆优化版)
def do_calc(text):
    if ':' in text: return None
    try:
        clean = re.sub(r'[^\d\+\-\*\/\(\)\.\%]', '', text)
        if not clean: return None
        res = simple_eval(clean)
        return int(res) if float(res).is_integer() else res
    except: return None

# ==============================================================================
# 6. Bot Setup
# ==============================================================================

def setup_worker_bot(app_instance: Application, bot_index: int) -> None:
    token_end = app_instance.bot.token[-4:]
    @log_interaction
    async def start(u, c):
        await safe_reply(u, f"🤖 工兵 #{bot_index} ({token_end}) 就绪。")

    app_instance.add_handler(CommandHandler("start", start))
    
    # --- 🔥 新增：下载方式说明 ---
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
    # 注册“下载方式”指令
    app_instance.add_handler(MessageHandler(filters.Regex(DOWNLOAD_HELP_PATTERN), lambda u,c: send_static_reply(u,c,t_download_help)))

    # 🔥 IP 查询 (统一使用 ip-api.com)
    @log_interaction
    async def query_ip(u, c):
        if not u.message.text: return
        text = u.message.text.strip()
        target_ip = re.sub(r"^(查|IP定位)\s*", "", text).strip()
        await safe_reply(u, f"🔍 正在查询 IP: {target_ip} ...")
        try:
            # 使用 ip-api.com (无月限额)
            url = f"http://ip-api.com/json/{target_ip}?lang=zh-CN"
            resp = await GLOBAL_HTTP_CLIENT.get(url)
            data = resp.json()
            
            if data.get('status') != 'success':
                return await safe_reply(u, f"❌ 查询失败: {data.get('message', '未知错误')}")
            
            # 字段映射
            ip = data.get('query', target_ip)
            country = data.get('country', '')
            region = data.get('regionName', '')
            city = data.get('city', '')
            org = data.get('isp', '') 
            asn = data.get('as', '')
            tz = data.get('timezone', '')
            lat = data.get('lat', 0)
            lon = data.get('lon', 0)
            flag = '🌍'

            msg = (
                f"{flag} <b>IP定位结果</b>\n"
                f"IP: <code>{ip}</code>\n"
                f"位置: {country} {region} {city}\n"
                f"运营商/组织: {org}\n"
                f"ASN: {asn}\n"
                f"时区: {tz}\n"
                f"当地时间: N/A\n"
                f"坐标: {lat}, {lon}\n"
                f"地图: <a href=\"https://www.google.com/maps?q={lat},{lon}\">Google Maps</a>\n"
                f"来源: ip-api.com"
            )
            await safe_reply(u, msg, parse_mode='HTML')
        except Exception as e:
            logger.error(f"IP Query Error: {e}")
            await safe_reply(u, "❌ 查询出错，请稍后重试。")

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

    if GLOBAL_IMAGE_PATTERN:
        app_instance.add_handler(MessageHandler(filters.Regex(GLOBAL_IMAGE_PATTERN), lambda u,c: send_global_media(u,c,False)))
    if GLOBAL_VIDEO_PATTERN:
        app_instance.add_handler(MessageHandler(filters.Regex(GLOBAL_VIDEO_PATTERN), lambda u,c: send_global_media(u,c,True)))

def setup_calculator_bot(app_instance: Application) -> None:
    @log_interaction
    async def start(u, c):
        await safe_reply(u, "👋 我是智能计算器。\n功能：计算、新股、日报、IP定位、查U、/book、/quote")
    
    app_instance.add_handler(CommandHandler("start", start))
    
    # --- 🔥 IP 查询 (统一使用 ip-api.com) ---
    @log_interaction
    async def query_ip(u, c):
        if not u.message.text: return
        text = u.message.text.strip()
        target_ip = re.sub(r"^(查|IP定位)\s*", "", text).strip()
        
        await safe_reply(u, f"🔍 正在查询 IP: {target_ip} ...")
        try:
            # 统一使用 ip-api.com
            url = f"http://ip-api.com/json/{target_ip}?lang=zh-CN"
            resp = await GLOBAL_HTTP_CLIENT.get(url)
            data = resp.json()
            
            if data.get('status') != 'success':
                return await safe_reply(u, f"❌ 查询失败: {data.get('message', '未知错误')}")
                
            flag = data.get('flag', {}).get('emoji', '🌍')
            msg = (
                f"{flag} <b>IP定位结果</b>\n"
                f"IP: <code>{data.get('query')}</code>\n"
                f"位置: {data.get('country')} {data.get('regionName')} {data.get('city')}\n"
                f"运营商/组织: {data.get('isp')}\n"
                f"ASN: {data.get('as')}\n"
                f"时区: {data.get('timezone')}\n"
                f"当地时间: N/A\n"
                f"坐标: {data.get('lat')}, {data.get('lon')}\n"
                f"地图: <a href=\"https://www.google.com/maps?q={data.get('lat')},{data.get('lon')}\">Google Maps</a>\n"
                f"来源: ip-api.com"
            )
            await safe_reply(u, msg, parse_mode='HTML')
        except Exception as e:
            logger.error(f"IP Query Error: {e}")
            await safe_reply(u, "❌ 查询出错")

    # --- 🔥 USDT 查询 ---
    @log_interaction
    async def query_usdt(u, c):
        if not u.message.text: return
        try:
            address = u.message.text.strip().replace("查", "").strip()
        except: return
        
        if not address.startswith("T") or len(address) != 34:
             return await safe_reply(u, "⚠️ 地址格式不对，请输入正确的 TRC20 地址。")

        await safe_reply(u, f"🔗 正在查询链上数据: {address} ...")

        try:
            # 读取 API Key
            api_key = os.getenv("TRONSCAN_API_KEY", "")
            headers = {
                "User-Agent": "Mozilla/5.0",
                "TRON-PRO-API-KEY": api_key 
            }
            
            # 1. 查余额
            balance_url = f"https://apilist.tronscanapi.com/api/account/tokens?address={address}&start=0&limit=20&hidden=0&show=0&sortType=0"
            
            # 2. 查转账 (只查 USDT 合约)
            usdt_contract = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
            transfer_url = f"https://apilist.tronscanapi.com/api/token_trc20/transfers?limit=10&start=0&sort=-timestamp&count=true&relatedAddress={address}&contract_address={usdt_contract}"
            
            resp_bal, resp_trans = await asyncio.gather(
                GLOBAL_HTTP_CLIENT.get(balance_url, headers=headers),
                GLOBAL_HTTP_CLIENT.get(transfer_url, headers=headers)
            )

            if resp_bal.status_code != 200:
                return await safe_reply(u, f"❌ 查询被拦截 (HTTP {resp_bal.status_code})。")

            bal_data = resp_bal.json()
            trans_data = resp_trans.json()
            
            # --- 处理余额 ---
            usdt_balance = 0.0
            for t in bal_data.get('data', []):
                if t.get('tokenId') == usdt_contract or t.get('tokenAbbr') == 'USDT':
                    # 强制 6 位精度
                    raw_balance = float(t.get('balance', 0))
                    usdt_balance = raw_balance / 1000000
                    break
            
            balance_str = "{:,.2f}".format(usdt_balance)

            # --- 处理转账记录 ---
            transfers = trans_data.get('token_transfers', [])
            trans_lines = []
            
            if not transfers:
                trans_lines.append("暂无近 10 笔 USDT 记录")
            else:
                for tx in transfers:
                    # 双重保险：再次确认 USDT
                    if tx.get('contract_address') != usdt_contract:
                        continue

                    is_in = tx.get('to_address') == address
                    arrow = "🟢收" if is_in else "🔴转"
                    
                    amt = float(tx.get('quant', 0)) / 1000000
                    amt_str = "{:,.2f}".format(amt)
                    
                    # 时间 (转为北京时间 UTC+8)
                    ts = int(tx.get('block_ts', 0)) / 1000
                    dt_object = datetime.fromtimestamp(ts, timezone(timedelta(hours=8)))
                    time_str = dt_object.strftime('%m-%d %H:%M')
                    
                    other = tx.get('from_address') if is_in else tx.get('to_address')
                    other_short = f"{other[:4]}...{other[-4:]}"
                    
                    status_icon = "" if tx.get('confirmed') else "⏳"
                    
                    trans_lines.append(f"{arrow} {amt_str} | {other_short} | {time_str} {status_icon}")

            # 只取前 5 条
            final_list = trans_lines[:6]
            trans_text = "\n".join(final_list)

            msg = (
                f"💰 <b>钱包查询结果</b>\n"
                f"地址: <code>{address}</code>\n"
                f"💎 <b>USDT余额:</b> <code>{balance_str}</code>\n\n"
                f"📋 <b>最近 USDT 真实流向:</b>\n"
                f"{trans_text}\n\n"
                f"🔗 <a href=\"https://tronscan.org/#/address/{address}/transfers\">点击查看 TronScan 完整明细</a>"
            )
            await safe_reply(u, msg, parse_mode='HTML')

        except Exception as e:
            logger.error(f"USDT Query Error: {e}")
            await safe_reply(u, f"❌ 查询失败: {e}")

    app_instance.add_handler(MessageHandler(filters.Regex(IP_QUERY_PATTERN), query_ip))
    app_instance.add_handler(MessageHandler(filters.Regex(r"^查\s*T[a-zA-Z0-9]{33}$"), query_usdt))

    @log_interaction
    async def book(u,c): 
        q = " ".join(c.args)
        if not q: return await safe_reply(u, "请加关键词")
        await safe_reply(u, "📚 正在为您寻找好书，请稍候...") 
        ai_text = await call_gemini(f"推荐3本关于{q}的书，带理由和摘抄")
        await safe_reply(u, ai_text, parse_mode='Markdown')
    
    @log_interaction
    async def quote(u,c): 
        q = " ".join(c.args)
        t = f"《{q}》" if q else "名著"
        await safe_reply(u, "📜 正在翻阅名著摘录金句，请稍候...") 
        ai_text = await call_gemini(f"从{t}找一段经典摘抄并赏析")
        await safe_reply(u, ai_text, parse_mode='Markdown')
    
    @log_interaction
    async def deep(u,c): 
        q = " ".join(c.args)
        if not q: return await safe_reply(u, "请加话题")
        await safe_reply(u, "🧠 正在深度思考，请稍候...") 
        ai_text = await call_gemini(f"深度解析话题：{q}")
        await safe_reply(u, ai_text, parse_mode='Markdown')

    app_instance.add_handler(CommandHandler("book", book))
    app_instance.add_handler(CommandHandler("quote", quote))
    app_instance.add_handler(CommandHandler("deep", deep))
    app_instance.add_handler(MessageHandler(filters.Regex(IPO_COMMAND_PATTERN), get_ipo_info))
    app_instance.add_handler(MessageHandler(filters.Regex(DIGEST_COMMAND_PATTERN), get_daily_digest))

    # --- 🔥 计算器升级：支持回复连续计算 + 兼容 / 开头 ---
    @log_interaction
    async def calc(u,c):
        text = u.message.text
        if not text: return
        
        # 1. 尝试 "引用回复" 连续计算
        if u.message.reply_to_message and u.message.reply_to_message.text:
            prev_msg = u.message.reply_to_message.text
            match = re.search(r'🔢\s*([0-9\.]+)', prev_msg.replace(',', ''))
            if match:
                prev_num = match.group(1)
                if text.strip()[0] in ['+', '-', '*', '/']:
                    combined_text = f"{prev_num}{text.strip()}"
                    res = do_calc(combined_text)
                    if res is not None:
                        await safe_reply(u, f"🔢 {res}")
                        return

        # 2. 普通计算 (不再拦截 / 开头)
        res = do_calc(text)
        if res is not None: 
            await safe_reply(u, f"🔢 {res}")

    app_instance.add_handler(MessageHandler(filters.TEXT | filters.COMMAND, calc))

# ==============================================================================
# 7. 启动
# ==============================================================================
app = FastAPI()

@app.get("/")
async def root():
    return {"status": "ok", "msg": "Bot Service is Running (Shield Mode)"}

@app.on_event("startup")
async def startup_event():
    global GLOBAL_HTTP_CLIENT, PLAYWRIGHT_INSTANCE, BROWSER_INSTANCE
    
    GLOBAL_HTTP_CLIENT = httpx.AsyncClient(timeout=30.0, verify=False)
    
    try:
        logger.info("🚀 Starting Playwright...")
        PLAYWRIGHT_INSTANCE = await async_playwright().start()
        BROWSER_INSTANCE = await PLAYWRIGHT_INSTANCE.chromium.launch(
            headless=True, 
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]
        )
        app.state.browser = BROWSER_INSTANCE
        logger.info("✅ System Ready: Playwright Started")
    except Exception as e: 
        logger.error(f"❌ System Start Error (Playwright): {e}")

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
    if GLOBAL_VIDEO_PATTERN: GLOBAL_VIDEO_PATTERN = r"^(" + "|".join([re.escape(k) for k in GLOBAL_VIDEO_MAP.keys()]) + r")$"

    active_tokens = set()

    # --- 工兵 (1-10) ---
    for i in range(1, 11):
        raw_token = os.getenv(f"BOT_TOKEN_{i}")
        
        if raw_token:
            token = raw_token.strip() 
            if len(token) < 10:
                continue
                
            if token in active_tokens:
                continue
            
            try:
                bot = Application.builder().token(token).build()
                bot.bot_data["fastapi_app"] = app
                bot.bot_data["bot_index"] = i
                
                # 🔥🔥🔥🔥 修复核心：直接读取环境变量并存入 bot_data (解决 "配置错误") 🔥🔥🔥🔥
                # 这样 handler 直接取 bot_data['api_url']，绝对不会找不到
                if url := os.getenv(f"BOT_{i}_API_URL", "").strip():
                    bot.bot_data["api_url"] = url
                if apk := os.getenv(f"BOT_{i}_APK_URL", "").strip():
                    bot.bot_data["apk_url"] = apk
                if al := os.getenv(f"BOT_{i}_ALLOWED_CHAT_IDS", "").strip(): 
                    bot.bot_data["allowed_chats"] = [c.strip() for c in al.split(',')]
                
                # 🔥🔥🔥【CRITICAL FIX HERE】🔥🔥🔥
                # Ensure bot is initialized BEFORE adding jobs to the queue
                await bot.initialize()
                
                setup_worker_bot(bot, i) 
                path = f"bot{i}_webhook"
                BOT_APPLICATIONS[path] = bot
                
                # 恢复定时任务逻辑
                schedule_chat_id = os.getenv(f"BOT_{i}_SCHEDULE_CHAT_ID")
                schedule_msg = os.getenv(f"BOT_{i}_SCHEDULE_MESSAGE")
                schedule_times = os.getenv(f"BOT_{i}_SCHEDULE_TIMES_UTC")

                if schedule_chat_id and schedule_msg and schedule_times:
                    for t_str in schedule_times.split(','):
                        try:
                            h, m = map(int, t_str.strip().split(':'))
                            bot.job_queue.run_daily(
                                send_scheduled_worker_message,
                                time=time(hour=h, minute=m),
                                data={'chat_id': schedule_chat_id, 'text': schedule_msg},
                                name=f"bot_{i}_schedule_{h}_{m}"
                            )
                            logger.info(f"⏰ 工兵 #{i} 定时任务已添加: UTC {h}:{m}")
                        except ValueError:
                            logger.error(f"❌ 工兵 #{i} 时间格式错误: {t_str}")

                await bot.start()
                try:
                    await bot.updater.start_polling(drop_pending_updates=True)
                    logger.info(f"✅ Worker {i} Started Polling")
                except Conflict:
                    logger.warning(f"🛡️ 触发盾牌: Worker {i} 遇到 Conflict (正常)，等待旧实例退出...")
                except Exception as e:
                    logger.error(f"❌ Worker {i} Polling Error: {e}")

                active_tokens.add(token)
            except Exception as e:
                logger.error(f"❌ Worker {i} 启动失败: {e}")

    # --- 计算器 ---
    raw_calc_token = os.getenv("CALC_BOT_TOKEN")
    if raw_calc_token:
        calc_token = raw_calc_token.strip()
        if calc_token in active_tokens:
             logger.warning("⚠️ 跳过计算器 Bot: Token 已经被工兵 Bot 使用了！")
        else:
            try:
                c_bot = Application.builder().token(calc_token).build()
                await c_bot.initialize()
                setup_calculator_bot(c_bot)
                await c_bot.start()
                
                target_chat_id = os.getenv("CALC_CHAT_ID")
                if target_chat_id:
                    c_bot.job_queue.run_daily(auto_send_digest, time=time(hour=0, minute=0), data={'chat_id': target_chat_id})
                    logger.info(f"⏰ 计算器日报定时任务已启动: {target_chat_id}")

                try:
                    await c_bot.updater.start_polling(drop_pending_updates=True)
                    logger.info(f"✅ Calc Bot Started Polling")
                except Conflict:
                    logger.warning(f"🛡️ 触发盾牌: Calc Bot 遇到 Conflict (正常)，等待旧实例退出...")
                except Exception as e:
                    logger.error(f"❌ Calc Bot Polling Error: {e}")

                BOT_APPLICATIONS["calc"] = c_bot
                active_tokens.add(calc_token)
                logger.info(f"✅ Calc Bot Started (Token ends with {calc_token[-4:]})")
            except Exception as e:
                logger.error(f"❌ Calc Bot 启动失败: {e}")
    else:
        logger.info("ℹ️ 未检测到 CALC_BOT_TOKEN，计算器未启动。")

@app.on_event("shutdown")
async def shutdown():
    logger.info("Starting graceful shutdown...")
    for b in BOT_APPLICATIONS.values():
        try:
            if b.updater and b.updater.running: await b.updater.stop()
            if b.running: await b.stop()
            await b.shutdown()
        except Exception as e:
            logger.error(f"Error shutting down bot: {e}")
    if GLOBAL_HTTP_CLIENT: await GLOBAL_HTTP_CLIENT.aclose()
    if BROWSER_INSTANCE: await BROWSER_INSTANCE.close()
    if PLAYWRIGHT_INSTANCE: await PLAYWRIGHT_INSTANCE.stop()
    logger.info("Shutdown complete.")
