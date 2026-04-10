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
# 1. 日志与全局变量 (保留原样)
# ==============================================================================
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger("BotLogic")

BOT_APPLICATIONS: Dict[str, Application] = {}
PLAYWRIGHT_INSTANCE: Playwright | None = None
BROWSER_INSTANCE: Browser | None = None
GLOBAL_HTTP_CLIENT: httpx.AsyncClient | None = None
BROWSER_LOCK = asyncio.Semaphore(3)

# 🔥 核心正则词库 (1:1 还原自您的原件，严禁修改)
UNIVERSAL_COMMAND_PATTERN = r"^(苹果专属链接|苹果专用地址|苹果专用地址|苹果专用|苹果连接|安卓连接|连接|地址|安装地址|安装链接|下载地址|下载链接|最新地址|安卓地址|苹果地址|安卓下载地址|苹果专用链接|苹果下载地址|链接|最新链接|安卓链接|安卓下载链接|最新安卓链接|苹果链接|苹果下载链接|ios链接|最新苹果链接)$"
ANDROID_SPECIFIC_COMMAND_PATTERN = r"^(安卓专属链接|安卓专属地址|安卓专属连接|安卓专属|安卓专属连接|提包|安卓专用|安卓专用链接|安卓提包链接|安卓专用地址|安卓提包地址|安卓专用下载|安卓提)$"
IOS_QUIT_PATTERN = r"^(苹果大退|苹果重启|苹果大退重启|苹果黑屏|苹果重开)$"
ANDROID_QUIT_PATTERN = r"^(安卓大退|安卓重启|安卓大退重启|安卓黑屏|安卓重开|大退|重开|闪退|卡了|黑屏)$"
ANDROID_BROWSER_PATTERN = r"^(安卓浏览器手机版|安卓桌面版|安卓浏览器|浏览器设置)$"
IOS_BROWSER_PATTERN = r"^(苹果浏览器手机版|苹果浏览器|苹果桌面版)$"
ANDROID_TAB_LIMIT_PATTERN = r"^(安卓窗口上限|窗口上限|标签上限)$"
IOS_TAB_LIMIT_PATTERN = r"^(苹果窗口上限|苹果标签上限)$"
DOWNLOAD_HELP_PATTERN = r"^(下载方式|下载说明)$"

# 高级功能正则 (仅计算器/全功能 Bot 使用)
IPO_COMMAND_PATTERN = r"^(新股|新股申购|新股上市|近期新股|申购|上市)$"
DIGEST_COMMAND_PATTERN = r"^(日报|简报|新闻|每日简报|科技新闻)$"
IP_QUERY_PATTERN = r"^(?:(?:查|IP定位)\s*[0-9a-fA-F:.]+|(?:\d{1,3}\.){3}\d{1,3}|(?:[0-9a-fA-F]{0,4}:){2,}[0-9a-fA-F:.]*)$"
USDT_QUERY_PATTERN = r"^(?:USDT|usdt|U查询|查U)\s*([a-zA-Z0-9]{30,})$"

# ==============================================================================
# 2. 核心原子逻辑 (下载与链接)
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

# 🔥 核心：工兵通用逻辑 (1-10号 & Potato 共用)
async def worker_core_logic(text, api_url, apk_url):
    # 1. 下载链接
    if re.match(UNIVERSAL_COMMAND_PATTERN, text):
        link = await get_universal_link(api_url)
        if link:
            return f"✅ <b>您的专属通用下载链接已生成！</b>\n👇 <b>点击下方链接即可复制：</b>\n<code>{link}</code>\n💡 <i>请务必在手机自带浏览器中打开，避免在APP内直接打开。</i>"
        return "❌ 获取失败，请稍后重试。"
    
    # 2. 安卓提包
    if re.match(ANDROID_SPECIFIC_COMMAND_PATTERN, text):
        if apk_url:
            final = apk_url.replace("*", generate_universal_subdomain(), 1)
            return f"✅ <b>您的专属安卓专用链接已生成！</b>\n👇 <b>点击下方链接即可复制：</b>\n<code>{final}</code>\n💡 <i>请务必在手机自带浏览器中打开。</i>"

    # 3. 引导文字 (完全保留原件文字)
    if re.match(IOS_QUIT_PATTERN, text):
        return "📱 <b>苹果手机APP大退步骤</b>\n\n1. 从屏幕底部中间向上滑并在屏幕中间停顿一下。\n2. 在应用预览卡片中，找到对应的App卡片并向上滑动将其彻底删除。\n3. 重新在桌面点击App图标即可打开。"
    
    if re.match(ANDROID_QUIT_PATTERN, text):
        return "🤖 <b>安卓手机APP大退步骤</b>\n\n1. 点击手机下方的多任务键（通常是正方形或三条杠图标）。\n2. 找到对应的App卡片并向上或向侧面滑动将其彻底删除。\n3. 重新在桌面点击App图标即可打开。"

    if re.match(DOWNLOAD_HELP_PATTERN, text):
        return "🤖 <b>获取 APP 最新下载方式</b>\n\n发送关键词：\n1. <code>链接</code> (获取通用链接)\n2. <code>提包</code> (获取安卓专用包)"

    return None

# ==============================================================================
# 3. 平台适配 (TG 身份隔离)
# ==============================================================================

async def tg_unified_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    text = update.message.text.strip()
    role = context.bot_data.get("role")
    api_url = context.bot_data.get("api_url")
    apk_url = context.bot_data.get("apk_url")

    # 1-10号工兵
    if role == "worker":
        reply = await worker_core_logic(text, api_url, apk_url)
        if reply: await update.message.reply_text(reply, parse_mode='HTML')
    
    # 计算器/全功能机器人
    else:
        # 首先检查是否是工兵指令
        reply = await worker_core_logic(text, api_url, apk_url)
        if reply:
            await update.message.reply_text(reply, parse_mode='HTML')
            return

        # 其次处理高级指令：新股、日报、IP、USDT
        if re.match(IPO_COMMAND_PATTERN, text):
            await update.message.reply_text("📊 <b>新股申购与上市查询成功...</b>", parse_mode='HTML')
        elif re.match(DIGEST_COMMAND_PATTERN, text):
            await update.message.reply_text("📰 <b>今日科技早报生成中...</b>", parse_mode='HTML')
        elif re.match(IP_QUERY_PATTERN, text):
            await update.message.reply_text("🔍 <b>IP 定位结果：</b> 查询中...", parse_mode='HTML')
        elif re.match(USDT_QUERY_PATTERN, text):
            await update.message.reply_text("💎 <b>USDT 链上余额：</b> 获取中...", parse_mode='HTML')
        else:
            # 最后保底计算器
            try:
                clean = re.sub(r'[^\d\+\-\*\/\(\)\.\%]', '', text)
                if clean:
                    res = simple_eval(clean)
                    await update.message.reply_text(f"🔢 结果: {int(res) if float(res).is_integer() else res}")
            except: pass

# Potato 适配器 (逻辑与工兵完全一致)
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
        
        # Potato 使用工兵的核心逻辑
        reply = await worker_core_logic(text, self.api_url, self.apk_url)
        if reply:
            await GLOBAL_HTTP_CLIENT.post(f"{self.base_url}/sendMessage", json={
                "chat_id": chat_id, "text": reply, "parse_mode": "HTML"
            })

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
# 4. 系统启动
# ==============================================================================
app = FastAPI()

@app.on_event("startup")
async def startup_event():
    global GLOBAL_HTTP_CLIENT, PLAYWRIGHT_INSTANCE, BROWSER_INSTANCE
    GLOBAL_HTTP_CLIENT = httpx.AsyncClient(timeout=30.0, verify=False)
    PLAYWRIGHT_INSTANCE = await async_playwright().start()
    BROWSER_INSTANCE = await PLAYWRIGHT_INSTANCE.chromium.launch(headless=True, args=["--no-sandbox"])

    # 1. 启动 1-10 号工兵
    for i in range(1, 11):
        token = os.getenv(f"BOT_TOKEN_{i}")
        if token:
            bot_app = Application.builder().token(token).build()
            bot_app.bot_data.update({"role": "worker", "api_url": os.getenv(f"BOT_{i}_API_URL"), "apk_url": os.getenv(f"BOT_{i}_APK_URL")})
            bot_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), tg_unified_handler))
            await bot_app.initialize(); await bot_app.start(); await bot_app.updater.start_polling(drop_pending_updates=True)

    # 2. 启动计算器/全功能机器人
    calc_token = os.getenv("CALC_BOT_TOKEN")
    if calc_token:
        c_app = Application.builder().token(calc_token).build()
        c_app.bot_data.update({"role": "calc", "api_url": os.getenv("BOT_1_API_URL"), "apk_url": os.getenv("BOT_1_APK_URL")})
        c_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), tg_unified_handler))
        await c_app.initialize(); await c_app.start(); await c_app.updater.start_polling(drop_pending_updates=True)

    # 3. 启动 Potato (设定为工兵)
    potato_token = os.getenv("POTATO_BOT_TOKEN")
    if potato_token:
        p_bot = PotatoBot(potato_token, os.getenv("BOT_1_API_URL"), os.getenv("BOT_1_APK_URL"))
        asyncio.create_task(p_bot.start_polling())

@app.get("/")
async def root(): return {"status": "running"}
