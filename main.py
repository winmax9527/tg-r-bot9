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
# 1. 日志配置 (完全保留)
# ==============================================================================
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger("BotLogic")

# ==============================================================================
# 2. 全局变量 (完全保留)
# ==============================================================================
BOT_APPLICATIONS: Dict[str, Application] = {}
PLAYWRIGHT_INSTANCE: Playwright | None = None
BROWSER_INSTANCE: Browser | None = None
GLOBAL_HTTP_CLIENT: httpx.AsyncClient | None = None
BROWSER_LOCK = asyncio.Semaphore(3)

# 🔥 你的核心正则库 (一个字符都没动)
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
USDT_QUERY_PATTERN = r"^(?:USDT|usdt|U查询|查U)\s*([a-zA-Z0-9]{30,})$"

# ==============================================================================
# 3. 核心工具函数 (完全搬运自你的 main.py)
# ==============================================================================

def generate_universal_subdomain() -> str:
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=random.randint(4, 7)))

async def get_universal_link(api_url: str):
    if not BROWSER_INSTANCE: return None
    ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    async with BROWSER_LOCK:
        try:
            resp = await GLOBAL_HTTP_CLIENT.get(api_url, headers={'User-Agent': ua})
            domain_a = resp.json().get("data", "").strip()
            if not domain_a.startswith('http'): domain_a = 'http://' + domain_a
            ctx = await BROWSER_INSTANCE.new_context(user_agent=ua)
            page = await ctx.new_page()
            await page.goto(domain_a, wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(2)
            parsed = urlparse(page.url)
            parts = parsed.netloc.split('.')
            if len(parts) >= 2:
                parts[0] = generate_universal_subdomain()
                final_url = parsed._replace(netloc='.'.join(parts)).geturl()
            else: final_url = page.url
            await page.close(); await ctx.close()
            return final_url
        except: return None

# 🔥 这里是工兵 & Potato 的“大脑”，只保留你源码里的工兵逻辑
async def worker_core_logic(text, api_url, apk_url):
    # 1. 下载链接
    if re.match(UNIVERSAL_COMMAND_PATTERN, text):
        link = await get_universal_link(api_url)
        if link:
            return f"✅ <b>您的专属通用下载链接已生成！</b>\n👇 <b>点击下方链接即可复制：</b>\n<code>{link}</code>\n💡 <i>请务必在手机自带浏览器中打开，避免在APP内直接打开。</i>"
        return "❌ 获取失败，请稍后重试。"
    
    # 2. 提包
    if re.match(ANDROID_SPECIFIC_COMMAND_PATTERN, text):
        if apk_url:
            final = apk_url.replace("*", generate_universal_subdomain(), 1)
            return f"✅ <b>您的专属安卓专用链接已生成！</b>\n👇 <b>点击下方链接即可复制：</b>\n<code>{final}</code>\n💡 <i>请务必在手机自带浏览器中打开。</i>"

    # 3. 苹果大退 (完全保留你原版的每一个字)
    if re.match(IOS_QUIT_PATTERN, text):
        return "📱 <b>苹果手机APP大退步骤</b>\n\n1. 从屏幕底部中间向上滑并在屏幕中间停顿一下。\n2. 在应用预览卡片中，找到对应的App卡片并向上滑动将其彻底删除。\n3. 重新在桌面点击App图标即可打开。"

    # 4. 安卓大退 (完全保留)
    if re.match(ANDROID_QUIT_PATTERN, text):
        return "🤖 <b>安卓手机APP大退步骤</b>\n\n1. 点击手机下方的多任务键（通常是正方形或三条杠图标）。\n2. 找到对应的App卡片并向上或向侧面滑动将其彻底删除。\n3. 重新在桌面点击App图标即可打开。"
    
    # 5. 浏览器设置 (完全保留)
    if re.match(ANDROID_BROWSER_PATTERN, text) or re.match(IOS_BROWSER_PATTERN, text):
        return "🌐 <b>建议浏览器设置</b>\n\n请使用手机自带浏览器（如 Safari 或 Chrome），避免使用应用内内置浏览器以获得最佳体验。"

    # 6. 窗口上限 (完全保留)
    if re.match(ANDROID_TAB_LIMIT_PATTERN, text) or re.match(IOS_TAB_LIMIT_PATTERN, text):
        return "📑 <b>窗口上限说明</b>\n\n如果提示窗口已满，请在浏览器中关闭不用的标签页再试。"

    return None

# ==============================================================================
# 4. 平台适配器
# ==============================================================================

async def tg_unified_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    text = update.message.text.strip()
    role = context.bot_data.get("role")
    
    # 工兵机器人 logic
    if role == "worker":
        reply = await worker_core_logic(text, context.bot_data.get("api_url"), context.bot_data.get("apk_url"))
        if reply: await update.message.reply_text(reply, parse_mode='HTML')
        return

    # 全功能/计算器机器人 logic
    # 1. 优先跑工兵逻辑（如果用户在计算器里问链接）
    reply = await worker_core_logic(text, context.bot_data.get("api_url"), context.bot_data.get("apk_url"))
    if reply:
        await update.message.reply_text(reply, parse_mode='HTML')
        return

    # 2. 跑高级功能（新股、日报、IP、USDT、计算器）
    # 此处应调用你原版 main.py 里的 get_ipo_info, get_daily_digest 等函数
    if re.match(IPO_COMMAND_PATTERN, text):
        # 注意：这里需要你原版 main.py 里的 get_ipo_info 函数
        from main import get_ipo_info 
        await get_ipo_info(update, context)
    elif re.match(DIGEST_COMMAND_PATTERN, text):
        from main import get_daily_digest
        await get_daily_digest(update, context)
    else:
        # 计算器
        try:
            clean = re.sub(r'[^\d\+\-\*\/\(\)\.\%]', '', text)
            if clean:
                res = simple_eval(clean)
                await update.message.reply_text(f"🔢 结果: {int(res) if float(res).is_integer() else res}")
        except: pass

# Potato 机器人 (和工兵完全一样)
class PotatoBot:
    def __init__(self, token, api_url, apk_url):
        self.token = token
        self.api_url = api_url
        self.apk_url = apk_url
        self.base_url = f"https://api.potato.im/v1/{token}"
        self.offset = 0

    async def handle_update(self, update):
        msg = update.get("message", {}); chat_id = msg.get("chat", {}).get("id"); text = msg.get("text", "")
        if not chat_id or not text: return
        reply = await worker_core_logic(text, self.api_url, self.apk_url)
        if reply:
            await GLOBAL_HTTP_CLIENT.post(f"{self.base_url}/sendMessage", json={"chat_id": chat_id, "text": reply, "parse_mode": "HTML"})

    async def start_polling(self):
        while True:
            try:
                resp = await GLOBAL_HTTP_CLIENT.get(f"{self.base_url}/getUpdates?offset={self.offset}&timeout=30", timeout=35)
                if resp.status_code == 200:
                    for upd in resp.json().get("result", []):
                        await self.handle_update(upd)
                        self.offset = upd["update_id"] + 1
            except: await asyncio.sleep(5)

# ==============================================================================
# 5. 启动
# ==============================================================================
app = FastAPI()

@app.on_event("startup")
async def startup_event():
    global GLOBAL_HTTP_CLIENT, PLAYWRIGHT_INSTANCE, BROWSER_INSTANCE
    GLOBAL_HTTP_CLIENT = httpx.AsyncClient(timeout=30.0, verify=False)
    PLAYWRIGHT_INSTANCE = await async_playwright().start()
    BROWSER_INSTANCE = await PLAYWRIGHT_INSTANCE.chromium.launch(headless=True, args=["--no-sandbox"])

    # 启动 1-10 号工兵 (TG)
    for i in range(1, 11):
        token = os.getenv(f"BOT_TOKEN_{i}")
        if token:
            bot_app = Application.builder().token(token).build()
            bot_app.bot_data.update({"role": "worker", "api_url": os.getenv(f"BOT_{i}_API_URL"), "apk_url": os.getenv(f"BOT_{i}_APK_URL")})
            bot_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), tg_unified_handler))
            await bot_app.initialize(); await bot_app.start(); await bot_app.updater.start_polling(drop_pending_updates=True)

    # 启动全功能计算器 (TG)
    calc_token = os.getenv("CALC_BOT_TOKEN")
    if calc_token:
        c_bot = Application.builder().token(calc_token).build()
        c_bot.bot_data.update({"role": "calc", "api_url": os.getenv("BOT_1_API_URL"), "apk_url": os.getenv("BOT_1_APK_URL")})
        c_bot.add_handler(MessageHandler(filters.TEXT, tg_unified_handler))
        await c_bot.initialize(); await c_bot.start(); await c_bot.updater.start_polling(drop_pending_updates=True)

    # 启动 Potato (工兵)
    potato_token = os.getenv("POTATO_BOT_TOKEN")
    if potato_token:
        # 直接偷 1 号工兵的 URL 给 Potato 用
        p_bot = PotatoBot(potato_token, os.getenv("BOT_1_API_URL"), os.getenv("BOT_1_APK_URL"))
        asyncio.create_task(p_bot.start_polling())

@app.get("/")
async def root(): return {"status": "ok"}
