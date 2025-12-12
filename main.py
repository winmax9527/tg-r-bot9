import os
import logging
import asyncio
import re
import random
import string
import datetime
from datetime import date, datetime
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
BOT_API_URLS: Dict[str, str] = {}
BOT_APK_URLS: Dict[str, str] = {}
BOT_SCHEDULES: Dict[str, Dict[str, Any]] = {}
BOT_ALLOWED_CHATS: Dict[str, List[str]] = {}
PLAYWRIGHT_INSTANCE: Playwright | None = None
BROWSER_INSTANCE: Browser | None = None
GLOBAL_HTTP_CLIENT: httpx.AsyncClient | None = None
# 注意：如果你之前内存溢出过，建议这里改为 Semaphore(2)
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
        # 1. 第一次尝试：按要求的格式（Markdown/HTML）发送
        if parse_mode:
            await update.message.reply_text(text, parse_mode=parse_mode)
        else:
            await update.message.reply_text(text)
    except BadRequest as e:
        # 2. 如果报错说格式不对 (Can't parse entities)
        logger.warning(f"Reply failed with {parse_mode}: {e} -> ⚠️ 正在降级为纯文本重发...")
        try:
            # 3. 【关键补救】：去掉 parse_mode，以纯文本方式再发一次！
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
            
            browser = context.bot_data.get("fastapi_app").state.browser if context.bot_data.get("fastapi_app") else None
            if not browser:
                 if BROWSER_INSTANCE: browser = BROWSER_INSTANCE
                 else: raise RuntimeError("Browser not ready")
            
            context_p = await browser.new_context(user_agent=user_agent)
            page = await context_p.new_page()
            
            context_p = await browser.new_context(user_agent=user_agent)
            page = await context_p.new_page()

            # 🔥🔥🔥 新增优化：拦截垃圾请求，只看 HTML，不看图片和样式 🔥🔥🔥
            await page.route("**/*", lambda route: route.abort() 
                if route.request.resource_type in ["image", "media", "font", "stylesheet"] 
                else route.continue_())
            
            try:
                # 注意：如果不加载资源，networkidle 可能不准确，建议改用 domcontentloaded 会更快
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

# 新股逻辑 (🔥 已修复：过滤多余表头行)
@log_interaction
async def get_ipo_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not BROWSER_INSTANCE: return await safe_reply(update, "❌ 浏览器未就绪")
    
    await safe_reply(update, "🔍 正在检索并筛选最新新股...")
    
    async with BROWSER_LOCK:
        page = None
        try:
            page = await BROWSER_INSTANCE.new_page()
            await page.goto("https://vip.stock.finance.sina.com.cn/corp/go.php/vRPD_NewStockIssue/page/1.phtml", timeout=30000)
            
            try:
                await page.wait_for_selector("#NewStockTable", state="visible", timeout=10000)
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
                    # 🔥 核心修改：如果代码这一栏包含“代码”两个字，说明是表头垃圾，跳过！
                    if "代码" in item['code'] or "简称" in item['name']:
                        continue
                    
                    l_str = item['list_date']
                    keep = False
                    if l_str == "-" or not l_str:
                        keep = True
                    else:
                        try:
                            l_date = datetime.strptime(l_str, "%Y-%m-%d").date()
                            if l_date >= today:
                                keep = True
                        except:
                            keep = True
                    if keep:
                        valid_rows.append(item)
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

# 日报逻辑
@log_interaction
async def get_daily_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
# 6. Bot Setup
# ==============================================================================

def setup_worker_bot(app_instance: Application, bot_index: int) -> None:
    token_end = app_instance.bot.token[-4:]
    
    @log_interaction
    async def start(u, c):
        await safe_reply(u, f"🤖 工兵 #{bot_index} ({token_end}) 就绪。")

    app_instance.add_handler(CommandHandler("start", start))
    
    # --- 🔥 新增功能：IP 查询逻辑 ---
    @log_interaction
    async def query_ip(u, c):
        if not u.message.text: return
        # 提取 IP (格式：IP定位 8.8.8.8)
        try:
            target_ip = u.message.text.split(maxsplit=1)[1].strip()
        except IndexError:
            return await safe_reply(u, "⚠️ 格式错误，请使用：IP定位 8.8.8.8")

        await safe_reply(u, f"🔍 正在查询 IP: {target_ip} ...")
        
        try:
            # 使用 ipwho.is 免费接口 (支持中文)
            url = f"http://ipwho.is/{target_ip}?lang=zh-CN"
            resp = await GLOBAL_HTTP_CLIENT.get(url)
            data = resp.json()
            
            if not data.get('success'):
                return await safe_reply(u, f"❌ 查询失败: {data.get('message', '未知错误')}")
            
            # 格式化输出 (仿照你的截图)
            flag = data.get('flag', {}).get('emoji', '🌍')
            msg = (
                f"{flag} <b>IP定位结果</b>\n"
                f"IP: <code>{data.get('ip')}</code>\n"
                f"位置: {data.get('country')} {data.get('region')} {data.get('city')}\n"
                f"运营商/组织: {data.get('connection', {}).get('org', 'N/A')}\n"
                f"ASN: {data.get('connection', {}).get('asn', 'N/A')}\n"
                f"时区: {data.get('timezone', {}).get('id', 'N/A')}\n"
                f"当地时间: {data.get('timezone', {}).get('current_time', 'N/A')}\n"
                f"坐标: {data.get('latitude')}, {data.get('longitude')}\n"
                f"地图: <a href=\"https://www.google.com/maps?q={data.get('latitude')},{data.get('longitude')}\">Google Maps</a>\n"
                f"来源: ipwho.is"
            )
            await safe_reply(u, msg, parse_mode='HTML')
        except Exception as e:
            logger.error(f"IP Query Error: {e}")
            await safe_reply(u, "❌ 查询出错，请稍后重试。")

    # 注册 IP 查询 (正则匹配：IP定位 + 空格 + 数字/点)
    app_instance.add_handler(MessageHandler(filters.Regex(r"^IP定位\s+[\d\.]+$"), query_ip))

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
    
    # --- 🔥 新增功能：IP 查询 (代码与上面相同) ---
    @log_interaction
    async def query_ip(u, c):
        if not u.message.text: return
        try:
            target_ip = u.message.text.split(maxsplit=1)[1].strip()
        except IndexError:
            return await safe_reply(u, "⚠️ 格式错误，请使用：IP定位 8.8.8.8")
        await safe_reply(u, f"🔍 正在查询 IP: {target_ip} ...")
        try:
            url = f"http://ipwho.is/{target_ip}?lang=zh-CN"
            resp = await GLOBAL_HTTP_CLIENT.get(url)
            data = resp.json()
            if not data.get('success'):
                return await safe_reply(u, f"❌ 查询失败: {data.get('message', '未知错误')}")
            flag = data.get('flag', {}).get('emoji', '🌍')
            msg = (
                f"{flag} <b>IP定位结果</b>\n"
                f"IP: <code>{data.get('ip')}</code>\n"
                f"位置: {data.get('country')} {data.get('region')} {data.get('city')}\n"
                f"运营商/组织: {data.get('connection', {}).get('org', 'N/A')}\n"
                f"ASN: {data.get('connection', {}).get('asn', 'N/A')}\n"
                f"时区: {data.get('timezone', {}).get('id', 'N/A')}\n"
                f"当地时间: {data.get('timezone', {}).get('current_time', 'N/A')}\n"
                f"坐标: {data.get('latitude')}, {data.get('longitude')}\n"
                f"地图: <a href=\"https://www.google.com/maps?q={data.get('latitude')},{data.get('longitude')}\">Google Maps</a>\n"
                f"来源: ipwho.is"
            )
            await safe_reply(u, msg, parse_mode='HTML')
        except Exception as e:
            logger.error(f"IP Query Error: {e}")
            await safe_reply(u, "❌ 查询出错")

    # --- 🔥 新增功能：USDT 钱包查询 (查 Txxxx...) ---
    @log_interaction
    async def query_usdt(u, c):
        if not u.message.text: return
        # 提取地址
        try:
            # 兼容 "查 Txxx" 或 "查Txxx"
            address = u.message.text.strip().replace("查", "").strip()
        except: return
        
        if not address.startswith("T") or len(address) != 34:
             return await safe_reply(u, "⚠️ 地址格式看起来不对，请提供正确的 TRC20 地址 (T开头)。")

        await safe_reply(u, f"🔗 正在查询链上数据: {address} ...")

        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            # 1. 查询余额 (使用 TronScan API)
            balance_url = f"https://apilist.tronscanapi.com/api/account/tokens?address={address}&start=0&limit=20&hidden=0&show=0&sortType=0"
            
            # 2. 查询最近转账
            # USDT 合约地址: TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t
            transfer_url = f"https://apilist.tronscanapi.com/api/token_trc20/transfers?limit=5&start=0&sort=-timestamp&count=true&relatedAddress={address}&tokenAddress=TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
            
            # 并发请求
            resp_bal, resp_trans = await asyncio.gather(
                GLOBAL_HTTP_CLIENT.get(balance_url, headers=headers),
                GLOBAL_HTTP_CLIENT.get(transfer_url, headers=headers)
            )
            
            # 解析余额
            usdt_balance = 0.0
            tokens = resp_bal.json().get('data', [])
            for t in tokens:
                if t.get('tokenAbbr') == 'USDT':
                    # quantity 是精度前的数字，USDT精度6，需除以 1,000,000
                    # 但 TronScan API 有时直接返回 balance，有时返回 quantity。通常是 quantity / 10^decimals
                    amount = float(t.get('quantity', 0)) if t.get('quantity') else float(t.get('balance', 0))
                    # 如果数字很大，说明是未处理精度的，USDT是6位
                    if amount > 100000000: # 简单的启发式判断，或直接用 balance 字段(通常是处理过的)
                         usdt_balance = float(t.get('balance', 0))
                    else:
                         usdt_balance = float(t.get('balance', 0))
                    break
            
            # 格式化金额 (千分位)
            balance_str = "{:,.2f}".format(usdt_balance)

            # 解析交易记录
            transfers = resp_trans.json().get('token_transfers', [])
            trans_lines = []
            if not transfers:
                trans_lines.append("暂无 USDT 交易记录")
            else:
                for tx in transfers:
                    # 判断进出
                    is_in = tx.get('to_address') == address
                    arrow = "🟢收" if is_in else "🔴转"
                    # 金额处理 (USDT精度6)
                    amt = float(tx.get('quant', 0)) / 1000000
                    amt_str = "{:,.2f}".format(amt)
                    # 时间处理
                    ts = int(tx.get('block_ts', 0)) / 1000
                    time_str = datetime.fromtimestamp(ts).strftime('%m-%d %H:%M')
                    # 对方地址 (缩略显示)
                    other = tx.get('from_address') if is_in else tx.get('to_address')
                    other_short = f"{other[:4]}...{other[-4:]}"
                    
                    trans_lines.append(f"{arrow} {amt_str} | {other_short} | {time_str}")

            trans_text = "\n".join(trans_lines)

            msg = (
                f"💰 <b>钱包查询结果</b> (数据源: TronScan)\n"
                f"地址: <code>{address}</code>\n"
                f"💎 <b>USDT余额:</b> {balance_str}\n\n"
                f"📋 <b>最近 USDT 记录:</b>\n"
                f"{trans_text}\n"
                f"🔗 <a href=\"https://tronscan.org/#/address/{address}\">TronScan详情</a>"
            )
            await safe_reply(u, msg, parse_mode='HTML')

        except Exception as e:
            logger.error(f"USDT Query Error: {e}")
            await safe_reply(u, f"❌ 查询失败: {e}")

    app_instance.add_handler(MessageHandler(filters.Regex(r"^IP定位\s+[\d\.]+$"), query_ip))
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

    @log_interaction
    async def calc(u,c):
        if not u.message.text or u.message.text.startswith("/"): return
        res = do_calc(u.message.text)
        if res: 
            await safe_reply(u, f"🔢 {res}")

    app_instance.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, calc))

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
    if GLOBAL_VIDEO_MAP: GLOBAL_VIDEO_PATTERN = r"^(" + "|".join([re.escape(k) for k in GLOBAL_VIDEO_MAP.keys()]) + r")$"

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
                await bot.initialize()
                setup_worker_bot(bot, i) 
                
                path = f"bot{i}_webhook"
                BOT_APPLICATIONS[path] = bot
                
                if url := os.getenv(f"BOT_{i}_API_URL", "").strip(): BOT_API_URLS[path] = url
                if url := os.getenv(f"BOT_{i}_APK_URL", "").strip(): BOT_APK_URLS[path] = url
                if al := os.getenv(f"BOT_{i}_ALLOWED_CHAT_IDS", "").strip(): 
                    BOT_ALLOWED_CHATS[path] = [c.strip() for c in al.split(',')]
                
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
            if b.updater and b.updater.running:
                await b.updater.stop()
            if b.running:
                await b.stop()
            await b.shutdown()
        except Exception as e:
            logger.error(f"Error shutting down bot: {e}")

    if GLOBAL_HTTP_CLIENT: await GLOBAL_HTTP_CLIENT.aclose()
    if BROWSER_INSTANCE: await BROWSER_INSTANCE.close()
    if PLAYWRIGHT_INSTANCE: await PLAYWRIGHT_INSTANCE.stop()
    
    logger.info("Shutdown complete.")
