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
# 1. 日志与全局变量 (保留你原来的命名)
# ==============================================================================
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger("BotLogic")

BOT_APPLICATIONS: Dict[str, Application] = {}
PLAYWRIGHT_INSTANCE = None
BROWSER_INSTANCE = None
GLOBAL_HTTP_CLIENT = None
BROWSER_LOCK = asyncio.Semaphore(3)

# ==============================================================================
# 2. 核心正则词库 (完全保留你原来的文字描述)
# ==============================================================================
# 这里还原了你所有的关键词变体，确保用户输入习惯不变
UNIVERSAL_COMMAND_PATTERN = r"^(苹果专属链接|苹果专用地址|苹果专用|苹果连接|安卓连接|连接|地址|安装地址|安装链接|下载地址|下载链接|最新地址|安卓地址|苹果地址|安卓下载地址|苹果专用链接|苹果下载地址|最新链接|安卓下载链接|ios链接)$"
ANDROID_SPECIFIC_COMMAND_PATTERN = r"^(安卓专属链接|安卓专属地址|安卓专属|提包|安卓专用|安卓提包链接|安卓提)$"
IP_QUERY_PATTERN = r"^(?:(?:查|IP定位)\s*[0-9a-fA-F:.]+|(?:\d{1,3}\.){3}\d{1,3})$"

# ==============================================================================
# 3. 核心工具逻辑
# ==============================================================================

def do_calc(text):
    if ':' in text: return None
    try:
        clean = re.sub(r'[^\d\+\-\*\/\(\)\.\%]', '', text)
        if not clean: return None
        res = simple_eval(clean)
        return int(res) if float(res).is_integer() else res
    except: return None

def generate_universal_subdomain() -> str:
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=random.randint(4, 7)))

async def fetch_link_via_playwright(api_url: str):
    """Playwright 核心逻辑"""
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
            
            final_url = page.url
            # 随机子域名处理
            parsed = urlparse(final_url)
            domain_parts = parsed.netloc.split('.')
            if len(domain_parts) >= 2:
                domain_parts[0] = generate_universal_subdomain()
                final_url = parsed._replace(netloc='.'.join(domain_parts)).geturl()
            
            await page.close()
            await context_p.close()
            return final_url
        except Exception as e:
            logger.error(f"Fetch Link Error: {e}")
            return None

# ==============================================================================
# 4. Potato Bot 适配器 (完全并存)
# ==============================================================================
class PotatoBot:
    def __init__(self, token, api_url, apk_url):
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
        msg = update["message"]; chat_id = msg["chat"]["id"]; text = msg.get("text", "")
        if not text: return

        # 1. 计算器
        res = do_calc(text)
        if res is not None:
            await self.send_message(chat_id, f"🔢 <b>计算结果:</b> {res}")
            return

        # 2. 通用链接 (使用原始正则)
        if re.match(UNIVERSAL_COMMAND_PATTERN, text):
            await self.send_message(chat_id, "⏳ 正在生成专属链接...")
            link = await fetch_link_via_playwright(self.api_url)
            if link: await self.send_message(chat_id, f"✅ <b>专属通用链接:</b>\n<code>{link}</code>")
            else: await self.send_message(chat_id, "❌ 获取失败，请稍后重试。")

        # 3. 安卓提包 (使用原始正则)
        elif re.match(ANDROID_SPECIFIC_COMMAND_PATTERN, text):
            if self.apk_url:
                final_url = self.apk_url.replace("*", generate_universal_subdomain(), 1)
                await self.send_message(chat_id, f"📦 <b>安卓专用下载:</b>\n<code>{final_url}</code>")

    async def start_polling(self):
        logger.info("🚀 Potato Bot 轮询启动...")
        while True:
            try:
                resp = await GLOBAL_HTTP_CLIENT.get(f"{self.base_url}/getUpdates?offset={self.offset}&timeout=30", timeout=35)
                if resp.status_code == 200:
                    for update in resp.json().get("result", []):
                        await self.handle_update(update)
                        self.offset = update["update_id"] + 1
            except: await asyncio.sleep(5)

# ==============================================================================
# 5. Telegram Bot 处理逻辑 (完全保留原有 Handler)
# ==============================================================================
async def tg_unified_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text: return
    
    # 原有逻辑：计算器
    res = do_calc(text)
    if res is not None:
        await update.message.reply_text(f"🔢 结果: {res}")
        return

    # 原有逻辑：通用链接
    if re.match(UNIVERSAL_COMMAND_PATTERN, text):
        api_url = context.bot_data.get("api_url")
        if not api_url: return
        await update.message.reply_text("正在为您获取专属链接...")
        link = await fetch_link_via_playwright(api_url)
        if link: await update.message.reply_text(f"✅ <b>专属链接:</b>\n<code>{link}</code>", parse_mode='HTML')
        else: await update.message.reply_text("❌ 获取失败")

    # 原有逻辑：安卓提包
    elif re.match(ANDROID_SPECIFIC_COMMAND_PATTERN, text):
        apk_url = context.bot_data.get("apk_url")
        if apk_url:
            final_url = apk_url.replace("*", generate_universal_subdomain(), 1)
            await update.message.reply_text(f"📦 <b>安卓专用下载:</b>\n<code>{final_url}</code>", parse_mode='HTML')

# ==============================================================================
# 6. FastAPI 启动集成
# ==============================================================================
app = FastAPI()

@app.on_event("startup")
async def startup_event():
    global GLOBAL_HTTP_CLIENT, PLAYWRIGHT_INSTANCE, BROWSER_INSTANCE
    GLOBAL_HTTP_CLIENT = httpx.AsyncClient(timeout=30.0, verify=False)
    PLAYWRIGHT_INSTANCE = await async_playwright().start()
    BROWSER_INSTANCE = await PLAYWRIGHT_INSTANCE.chromium.launch(headless=True, args=["--no-sandbox"])

    # 启动 1-10 号 TG 机器人
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
            logger.info(f"✅ Telegram Bot {i} 已就绪")

    # 启动 Potato 机器人
    potato_token = os.getenv("POTATO_BOT_TOKEN")
    if potato_token:
        p_bot = PotatoBot(
            token=potato_token,
            api_url=os.getenv("POTATO_API_URL", os.getenv("BOT_1_API_URL")),
            apk_url=os.getenv("POTATO_APK_URL", os.getenv("BOT_1_APK_URL"))
        )
        asyncio.create_task(p_bot.start_polling())
        logger.info("✅ Potato Bot 已就绪")

@app.on_event("shutdown")
async def shutdown():
    for b in BOT_APPLICATIONS.values(): await b.stop()
    if BROWSER_INSTANCE: await BROWSER_INSTANCE.close()
    if PLAYWRIGHT_INSTANCE: await PLAYWRIGHT_INSTANCE.stop()
    await GLOBAL_HTTP_CLIENT.aclose()

@app.get("/")
async def root(): return {"status": "all_bots_running"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
