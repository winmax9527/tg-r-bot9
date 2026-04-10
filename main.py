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

BROWSER_LOCK = asyncio.Semaphore(3)

DEFAULT_RSS_FEEDS = [
    "https://36kr.com/feed",
    "https://www.cnbeta.com.tw/backend.php",
    "https://www.ithome.com/rss/",
    "https://sspai.com/feed",
    "http://www.zhihudaily.com/#/index",
]

# 🔥 核心正则词库 (完全保留您的原样)
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
DOWNLOAD_HELP_PATTERN = r"^(下载方式|下载说明)$"
IP_QUERY_PATTERN = r"^(?:(?:查|IP定位)\s*[0-9a-fA-F:.]+|(?:\d{1,3}\.){3}\d{1,3}|(?:[0-9a-fA-F]{0,4}:){2,}[0-9a-fA-F:.]*)$"

GLOBAL_IMAGE_MAP: Dict[str, str] = {}
GLOBAL_IMAGE_PATTERN: str = ""
GLOBAL_VIDEO_MAP: Dict[str, str] = {}
GLOBAL_VIDEO_PATTERN: str = ""

# ==============================================================================
# 3. 辅助函数 & 核心逻辑 (完全保留)
# ==============================================================================

def do_calc(text):
    if ':' in text: return None
    try:
        clean = re.sub(r'[^\d\+\-\*\/\(\)\.\%]', '', text)
        if not clean: return None
        res = simple_eval(clean)
        if isinstance(res, (int, float)):
            return int(res) if float(res).is_integer() else res
    except: return None

def generate_universal_subdomain() -> str:
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=random.randint(4, 7)))

def modify_url_subdomain(url_str: str, new_sub: str) -> str:
    try:
        parsed = urlparse(url_str)
        domain_parts = parsed.netloc.split('.')
        if len(domain_parts) < 2: return url_str
        domain_parts[0] = new_sub
        return parsed._replace(netloc='.'.join(domain_parts)).geturl()
    except: return url_str

async def get_universal_link(api_url: str):
    if not BROWSER_INSTANCE: return None
    user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    async with BROWSER_LOCK:
        try:
            resp = await GLOBAL_HTTP_CLIENT.get(api_url, headers={'User-Agent': user_agent})
            domain_a = resp.json().get("data", "").strip()
            if not domain_a.startswith('http'): domain_a = 'http://' + domain_a
            context_p = await BROWSER_INSTANCE.new_context(user_agent=user_agent)
            page = await context_p.new_page()
            await page.goto(domain_a, wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(2)
            final_url = modify_url_subdomain(page.url, generate_universal_subdomain())
            await page.close()
            await context_p.close()
            return final_url
        except Exception as e:
            logger.error(f"Playwright Error: {e}")
            return None

async def tg_unified_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    text = update.message.text.strip()
    api_url = context.bot_data.get("api_url")
    apk_url = context.bot_data.get("apk_url")

    # 1. 链接逻辑
    if re.match(UNIVERSAL_COMMAND_PATTERN, text):
        if not api_url: return
        await update.message.reply_text("⏳ 正在为您获取专属下载链接，请稍候...")
        link = await get_universal_link(api_url)
        if link:
            msg = f"✅ <b>您的专属通用下载链接已生成！</b>\n👇 <b>点击下方链接即可复制：</b>\n<code>{link}</code>\n💡 <i>请务必在手机自带浏览器中打开，避免在APP内直接打开。</i>"
            await update.message.reply_text(msg, parse_mode='HTML')
        else:
            await update.message.reply_text("❌ 获取失败，请稍后重试。")

    # 2. 安卓提包
    elif re.match(ANDROID_SPECIFIC_COMMAND_PATTERN, text):
        if not apk_url: return
        final_url = apk_url.replace("*", generate_universal_subdomain(), 1)
        msg = f"✅ <b>您的专属安卓专用链接已生成！</b>\n👇 <b>点击下方链接即可复制：</b>\n<code>{final_url}</code>\n💡 <i>请务必在手机自带浏览器中打开。</i>"
        await update.message.reply_text(msg, parse_mode='HTML')

    # 3. 计算器 (仅当没匹配到正则时尝试)
    else:
        res = do_calc(text)
        if res is not None:
            await update.message.reply_text(f"🔢 结果: {res}")

# ==============================================================================
# 🔥 4. 新增：Potato Bot 独立外挂类 (不影响原有逻辑)
# ==============================================================================
class PotatoBot:
    def __init__(self, token: str, api_url: str, apk_url: str):
        self.token = token
        self.api_url = api_url
        self.apk_url = apk_url
        self.base_url = f"https://api.potato.im/v1/{token}"
        self.offset = 0

    async def send_message(self, chat_id, text, parse_mode="HTML"):
        try:
            await GLOBAL_HTTP_CLIENT.post(f"{self.base_url}/sendMessage", json={
                "chat_id": chat_id, "text": text, "parse_mode": parse_mode
            }, timeout=10.0)
        except Exception as e: logger.error(f"Potato Send Error: {e}")

    async def handle_update(self, update):
        if "message" not in update: return
        msg = update["message"]
        chat_id = msg["chat"]["id"]
        text = msg.get("text", "")
        if not text: return

        # 完全复刻 TG 的逻辑判定
        if re.match(UNIVERSAL_COMMAND_PATTERN, text):
            await self.send_message(chat_id, "⏳ 正在为您获取专属下载链接...")
            link = await get_universal_link(self.api_url)
            if link:
                m = f"✅ <b>您的专属通用下载链接已生成！</b>\n👇 <b>点击下方链接即可复制：</b>\n<code>{link}</code>"
                await self.send_message(chat_id, m)
            else:
                await self.send_message(chat_id, "❌ 获取失败")
        
        elif re.match(ANDROID_SPECIFIC_COMMAND_PATTERN, text):
            final_url = self.apk_url.replace("*", generate_universal_subdomain(), 1)
            m = f"✅ <b>您的专属安卓专用链接已生成！</b>\n👇 <b>点击下方链接即可复制：</b>\n<code>{final_url}</code>"
            await self.send_message(chat_id, m)
        
        else:
            res = do_calc(text)
            if res is not None:
                await self.send_message(chat_id, f"🔢 结果: {res}")

    async def start_polling(self):
        logger.info("🚀 Potato Bot Polling Started")
        while True:
            try:
                resp = await GLOBAL_HTTP_CLIENT.get(f"{self.base_url}/getUpdates?offset={self.offset}&timeout=30", timeout=35)
                if resp.status_code == 200:
                    for upd in resp.json().get("result", []):
                        await self.handle_update(upd)
                        self.offset = upd["update_id"] + 1
            except: await asyncio.sleep(5)

# ==============================================================================
# 5. FastAPI & 启动 (整合 Potato)
# ==============================================================================
app = FastAPI()

@app.on_event("startup")
async def startup_event():
    global GLOBAL_HTTP_CLIENT, PLAYWRIGHT_INSTANCE, BROWSER_INSTANCE
    GLOBAL_HTTP_CLIENT = httpx.AsyncClient(timeout=30.0, verify=False)
    PLAYWRIGHT_INSTANCE = await async_playwright().start()
    BROWSER_INSTANCE = await PLAYWRIGHT_INSTANCE.chromium.launch(headless=True, args=["--no-sandbox"])

    # 1. 启动原有 Telegram 机器人 (1-10)
    for i in range(1, 11):
        token = os.getenv(f"BOT_TOKEN_{i}")
        if token:
            bot_app = Application.builder().token(token).build()
            bot_app.bot_data["api_url"] = os.getenv(f"BOT_{i}_API_URL")
            bot_app.bot_data["apk_url"] = os.getenv(f"BOT_{i}_APK_URL")
            bot_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), tg_unified_handler))
            await bot_app.initialize()
            await bot_app.start()
            await bot_app.updater.start_polling(drop_pending_updates=True)
            BOT_APPLICATIONS[f"tg_{i}"] = bot_app
            logger.info(f"✅ TG Bot {i} Started")

    # 2. 启动 Potato 机器人 (新增)
    potato_token = os.getenv("POTATO_BOT_TOKEN")
    if potato_token:
        p_bot = PotatoBot(
            token=potato_token,
            api_url=os.getenv("BOT_1_API_URL"), # 复用第一个的配置
            apk_url=os.getenv("BOT_1_APK_URL")
        )
        asyncio.create_task(p_bot.start_polling())
        logger.info("✅ Potato Bot Task Created")

@app.on_event("shutdown")
async def shutdown():
    for b in BOT_APPLICATIONS.values(): await b.stop()
    if BROWSER_INSTANCE: await BROWSER_INSTANCE.close()
    if PLAYWRIGHT_INSTANCE: await PLAYWRIGHT_INSTANCE.stop()
    await GLOBAL_HTTP_CLIENT.aclose()

@app.get("/")
async def root(): return {"status": "running"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
