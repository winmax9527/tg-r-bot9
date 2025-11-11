import os
import logging
import asyncio
import re
import requests # 用于快速获取域名 A
import random
import string
import datetime # <-- 用于定时任务
from urllib.parse import urlparse, urlunparse
from typing import List, Dict, Any 
from fastapi import FastAPI, Request, Response
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# 引入 Playwright
from playwright.async_api import async_playwright, Playwright, Browser

# --- 1. 配置日志记录 (Logging Setup) ---
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- 2. 全局状态和数据结构 ---
BOT_APPLICATIONS: Dict[str, Application] = {}
BOT_API_URLS: Dict[str, str] = {}
BOT_APK_URLS: Dict[str, str] = {}
BOT_SCHEDULES: Dict[str, Dict[str, Any]] = {} # <-- (保留) 定时任务
BOT_ALLOWED_CHATS: Dict[str, List[str]] = {} # <-- (保留) 安全白名单
PLAYWRIGHT_INSTANCE: Playwright | None = None
BROWSER_INSTANCE: Browser | None = None

# (全局图片功能)
GLOBAL_IMAGE_MAP: Dict[str, str] = {} 
GLOBAL_IMAGE_PATTERN: str = "" 

# --- ⬇️ 新增：全局视频功能 ⬇️ ---
GLOBAL_VIDEO_MAP: Dict[str, str] = {} # e.g. {"视频1": "url1", "教程1": "url1"}
GLOBAL_VIDEO_PATTERN: str = "" # e.g. r"^(视频1|教程1)$"
# --- ⬆️ 新增 ⬆️ ---

# --- 3. 核心功能：获取动态链接 ---
# (您 21:58 版本的所有关键字)
UNIVERSAL_COMMAND_PATTERN = r"^(地址|下载地址|下载链接|最新地址|安卓地址|苹果地址|安卓下载地址|苹果下载地址|链接|最新链接|安卓链接|安卓下载链接|最新安卓链接|苹果链接|苹果下载链接|ios链接|最新苹果链接)$"
ANDROID_SPECIFIC_COMMAND_PATTERN = r"^(提包|安卓专用|安卓专用链接|安卓提包链接|安卓专用地址|安卓提包地址|安卓专用下载|安卓提包)$"
IOS_QUIT_PATTERN = r"^(苹果大退|苹果重启|苹果大退重启|苹果黑屏|苹果重开)$"
ANDROID_QUIT_PATTERN = r"^(安卓大退|安卓重启|安卓大退重启|安卓黑屏|安卓重开|大退|重开|闪退|卡了|黑屏)$"
ANDROID_BROWSER_PATTERN = r"^(安卓浏览器手机版|安卓桌面版|安卓浏览器|浏览器设置)$"
IOS_BROWSER_PATTERN = r"^(苹果浏览器手机版|苹果浏览器|苹果桌面版)$"
ANDROID_TAB_LIMIT_PATTERN = r"^(安卓窗口上限|窗口上限|标签上限)$"
IOS_TAB_LIMIT_PATTERN = r"^(苹果窗口上限|苹果标签上限)$"

# (全局图片/视频关键字现在是动态加载的)

# --- 辅助函数 ---

# --- ⬇️ 智能安全检查 (我们最终的修复版) ⬇️ ---
def is_chat_allowed(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    """
    真正的智能安全检查：
    检查此消息的 Chat ID (及其变体) 是否在当前 Bot 的“白名单”上。
    """
    current_app = context.application
    allowed_list: List[str] = []
    
    # 1. 查找当前 Bot 的白名单
    for path, app_instance in BOT_APPLICATIONS.items():
        if app_instance is current_app:
            allowed_list = BOT_ALLOWED_CHATS.get(path, [])
            break
            
    # 2. 创建所有可能的 ID 变体
    chat_id_str = str(chat_id)
    possible_ids_to_check = {chat_id_str} 

    if chat_id_str.startswith("-100"):
        short_id = f"-{chat_id_str[4:]}"
        possible_ids_to_check.add(short_id)
    elif chat_id_str.startswith("-"):
        long_id = f"-100{chat_id_str[1:]}"
        possible_ids_to_check.add(long_id)

    # 3. 检查任何一个变体是否存在于白名单中
    for check_id in possible_ids_to_check:
        if check_id in allowed_list:
            return True # 匹配成功！

    # 4. 如果所有变体都失败了，则拒绝
    logger.warning(f"Bot (尾号: {current_app.bot.token[-4:]}) 收到来自 [未授权] Chat ID: {chat_id_str} (已检查 {possible_ids_to_check}) 的请求。已忽略。")
    return False
# --- ⬆️ 智能安全检查 ⬆️ ---


# (您修改后的 4-7 位)
def generate_universal_subdomain(min_len: int = 4, max_len: int = 7) -> str:
    """(需求 1) 生成一个 4-7 位随机长度的字符串 (仅小写)"""
    length = random.randint(min_len, max_len)
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))

# (您修改后的 5-9 位)
def generate_android_specific_subdomain(min_len: int = 5, max_len: int = 9) -> str:
    """(需求 2) 生成一个 5-9 位随机长度的字符串 (仅小写)"""
    length = random.randint(min_len, max_len)
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))

def modify_url_subdomain(url_str: str, new_sub: str) -> str:
    """替换 URL 的二级域名"""
    try:
        parsed = urlparse(url_str)
        domain_parts = parsed.netloc.split('.')
        if len(domain_parts) < 2: return url_str
        domain_parts[0] = new_sub
        new_netloc = '.'.join(domain_parts)
        new_parsed = parsed._replace(netloc=new_netloc)
        return new_parsed.geturl()
    except Exception as e:
        logger.error(f"修改子域名失败: {e} - URL: {url_str}")
        return url_str

# --- 核心处理器 1 (Playwright - 通用链接) ---
async def get_universal_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ (需求 1) - Playwright 动态链接 """
    
    # --- ⬇️ 智能安全检查 ⬇️ ---
    if not update.message or not is_chat_allowed(context, update.message.chat_id):
        return # 不在白名单，立即停止
    # --- ⬆️ 智能安全检查 ⬆️ ---

    bot_token_end = context.application.bot.token[-4:]
    logger.info(f"Bot {bot_token_end} 收到 [通用链接] 关键字，开始执行 [Playwright] 链接获取...")

    # 1. 检查浏览器
    fastapi_app = context.bot_data.get("fastapi_app")
    if not fastapi_app or not hasattr(fastapi_app.state, 'browser') or not fastapi_app.state.browser or not fastapi_app.state.browser.is_connected():
        logger.error("全局浏览器实例未运行或未连接！Playwright 无法工作。")
        await update.message.reply_text("❌ 服务内部错误：浏览器未启动。")
        return

    # 2. 查找此 Bot 专属的 API URL
    current_app = context.application
    api_url_for_this_bot = None
    for path, app_instance in BOT_APPLICATIONS.items():
        if app_instance is current_app:
            api_url_for_this_bot = BOT_API_URLS.get(path)
            break
    
    if not api_url_for_this_bot:
        logger.error(f"Bot (尾号: {bot_token_end}) 无法找到其配置的 API URL！")
        await update.message.reply_text("❌ 服务配置错误：未找到此 Bot 的 API 地址。")
        return

    # 3. 发送“处理中”提示 (您修改后的)
    try:
        await update.message.reply_text("正在为您获取专属通用下载链接，请稍候 ...")
    except Exception as e:
        logger.warning(f"发送“处理中”消息失败: {e}")

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    
    page = None 
    
    try:
        # --- 步骤 1: [Requests] 访问 API 获取 域名 A ---
        logger.info(f"步骤 1: (Requests) 正在从 API [{api_url_for_this_bot}] 获取 域名 A...")
        response_api = requests.get(api_url_for_this_bot, headers=headers, timeout=10)
        response_api.raise_for_status() 

        api_data = response_api.json() 
        
        if api_data.get("code") != 0 or "data" not in api_data or not api_data["data"]:
            logger.error(f"API 返回了错误或无效的数据: {api_data}")
            await update.message.reply_text("❌ 链接获取失败：API 未返回有效链接。")
            return

        domain_a = api_data["data"].strip() 

        if not domain_a.startswith(('http://', 'https://')):
            domain_a = 'http://' + domain_a
            
        logger.info(f"步骤 1 成功: 获取到 域名 A -> {domain_a}") 

        # --- 步骤 2: [Playwright] 访问 域名 A 获取 域名 B ---
        logger.info(f"步骤 2: (Playwright) 正在启动新页面访问 {domain_a}...")
        
        page = await fastapi_app.state.browser.new_page()
        page.set_default_timeout(40000) # 40 秒超时

        await page.goto(domain_a, wait_until="networkidle") 
        
        domain_b = page.url 
        logger.info(f"步骤 2 成功: 获取到 域名 B (完整): {domain_b}")

        # --- 步骤 3: 修改 域名 B 的二级域名 (您修改后的 4-7位) ---
        logger.info(f"步骤 3: 正在为 {domain_b} 生成 4-7 位随机二级域名...")
        random_sub = generate_universal_subdomain() # 4-7 位
        final_modified_url = modify_url_subdomain(domain_b, random_sub)
        logger.info(f"步骤 3 成功: 最终 URL -> {final_modified_url}")

        # --- 步骤 4: 发送最终 URL (您修改后的) ---
        await update.message.reply_text(f"✅ 您的专属通用下载链接已生成：\n{final_modified_url}")

    except Exception as e:
        logger.error(f"处理 get_universal_link (Playwright) 时发生错误: {e}")
        if "Timeout" in str(e):
            await update.message.reply_text("❌ 链接获取失败：目标网页加载超时（超过 40 秒）。")
        else:
            await update.message.reply_text(f"❌ 链接获取失败：{type(e).__name__}。")
    finally:
        if page:
            await page.close() 
            logger.info("Playwright 页面已关闭。")

# --- 核心处理器 2 (安卓专用链接) ---
async def get_android_specific_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ (需求 2 - 动态模板) """

    # --- ⬇️ 智能安全检查 ⬇️ ---
    if not update.message or not is_chat_allowed(context, update.message.chat_id):
        return # 不在白名单，立即停止
    # --- ⬆️ 智能安全检查 ⬆️ ---

    bot_token_end = context.application.bot.token[-4:]
    logger.info(f"Bot {bot_token_end} 收到 [安卓专用] 关键字，开始生成 APK 链接...")
    
    # 1. 查找此 Bot 专属的 APK URL 模板
    current_app = context.application
    apk_template = None
    for path, app_instance in BOT_APPLICATIONS.items():
        if app_instance is current_app:
            apk_template = BOT_APK_URLS.get(path) # 从新字典中查找
            break
            
    if not apk_template:
        logger.error(f"Bot (尾号: {bot_token_end}) 无法找到其配置的 BOT_..._APK_URL！")
        await update.message.reply_text("❌ 服务配置错误：未找到此 Bot 的 APK 链接模板。")
        return
        
    try:
        # 2. 生成 4-9 位随机二级域名 (您修改后的 5-9 位)
        random_sub = generate_android_specific_subdomain()
        
        # 3. 格式化 URL (替换模板中的第一个 *)
        final_url = apk_template.replace("*", random_sub, 1)
        
        # 4. 发送 (您修改后的)
        await update.message.reply_text(f"✅ 您的专属安卓专用下载链接已生成：\n{final_url}")
        
    except Exception as e:
        logger.error(f"处理 get_android_specific_link 时发生错误: {e}")
        await update.message.reply_text(f"❌ 处理安卓链接时发生内部错误。")

# --- (指南 处理器 3, 4, 5, 6, 7, 8) ---
# ... (所有 6 个指南处理器: send_ios_quit_guide, send_android_quit_guide, 等等... 保持不变)
# ... (为节省篇幅，我在这里省略了它们，但它们在下面的完整代码中)

# --- 核心处理器 3 (苹果重启指南) ---
async def send_ios_quit_guide(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ (需求 3 - 静态回复 iOS) """
    if not update.message or not is_chat_allowed(context, update.message.chat_id): return
    bot_token_end = context.application.bot.token[-4:]
    logger.info(f"Bot {bot_token_end} 收到 [苹果大退] 关键字，发送 iOS 重启指南...")
    message = """📱 <b>苹果手机大退重启步骤</b>

<b>1. 关闭App:</b> 在主屏幕上，从屏幕底部向上轻扫并在中间稍作停留，调出后台多任务界面。

<b>2. 找到并关闭:</b> 向左或向右滑动卡片找到要关闭的App，然后在该App的卡片上向上轻扫。

<b>3. 重新打开:</b> 返回主屏幕，点击该App图标重新打开。"""
    try:
        await update.message.reply_html(message)
    except Exception as e:
        logger.error(f"发送 [苹果大退] 指南时失败: {e}")

# --- 核心处理器 4 (安卓重启指南) ---
async def send_android_quit_guide(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ (需求 4 - 静态回复 Android) """
    if not update.message or not is_chat_allowed(context, update.message.chat_id): return
    bot_token_end = context.application.bot.token[-4:]
    logger.info(f"Bot {bot_token_end} 收到 [安卓大退] 关键字，发送 Android 重启指南...")
    message = """🤖 <b>安卓手机大退重启步骤</b>

<b>1. 关闭App:</b>
   • <b>方法一:</b> 从屏幕底部向上滑动并保持，即可进入后台多任务界面。
   • <b>方法二:</b> 点击屏幕底部的多任务/最近应用按钮 (通常是<code>□</code>或<code>≡</code>图标)。

<b>2. 找到并关闭:</b> 在后台列表中，向上滑动要关闭的App卡片。

<b>3. 重新打开:</b> 返回主屏幕或应用抽屉，点击该App图标重新打开。"""
    try:
        await update.message.reply_html(message)
    except Exception as e:
        logger.error(f"发送 [安卓大退] 指南时失败: {e}")

# --- 核心处理器 5 (安卓浏览器指南) ---
async def send_android_browser_guide(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ (需求 5 - 静态回复 Android 浏览器) """
    if not update.message or not is_chat_allowed(context, update.message.chat_id): return
    bot_token_end = context.application.bot.token[-4:]
    logger.info(f"Bot {bot_token_end} 收到 [安卓浏览器] 关键字，发送浏览器指南...")
    message = """🤖 <b>安卓手机浏览器设置为手机版模式步骤</b>

核心操作就是找到并关闭“桌面版”模式。

<b>1. 打开浏览器:</b> 启动您手机自带的浏览器 App (如“华为浏览器”、“小米浏览器”)。

<b>2. 进入菜单:</b> 点击浏览器界面右下角或右上角的三条横线(<code>≡</code>)或三个点图标(<code>⋮</code>)。

<b>3. 关闭“桌面模式”:</b> 在弹出的菜单列表中，找到“桌面版”、“桌面网站”或“电脑版”选项。

<b>4. 取消勾选:</b> 确保该选项<b>没有</b>被勾选 (开关处于关闭状态)。

<b>5. 刷新页面:</b> 页面会自动刷新，恢复为手机版的 UA 标识和显示界面。"""
    try:
        await update.message.reply_html(message)
    except Exception as e:
        logger.error(f"发送 [安卓浏览器] 指南时失败: {e}")

# --- 核心处理器 6 (苹果浏览器指南) ---
async def send_ios_browser_guide(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ (需求 6 - 静态回复 Apple 浏览器) """
    if not update.message or not is_chat_allowed(context, update.message.chat_id): return
    bot_token_end = context.application.bot.token[-4:]
    logger.info(f"Bot {bot_token_end} 收到 [苹果浏览器] 关键字，发送浏览器指南...")
    message = """📱 <b>苹果手机浏览器设置为手机版移动网站步骤</b>

在苹果设备上，使用 Safari 或其他浏览器时：

<b>1. 打开浏览器:</b> (例如 Safari)。

<b>2. 点击地址栏:</b> 点击屏幕顶部或底部的网址栏。

<b>3. 选择“网站设置”:</b> 在弹出的选项中，找到并点击“网站设置”或“大小” (如果显示 <code>AA</code> 图标)。

<b>4. 查找“请求桌面网站”:</b> 在菜单中，找到“请求桌面网站”选项。

<b>5. 取消勾选/关闭:</b> 确保该选项处于<b>未勾选</b>或<b>关闭</b>状态。

<b>6. 刷新页面:</b> 页面会自动加载手机版界面。"""
    try:
        await update.message.reply_html(message)
    except Exception as e:
        logger.error(f"发送 [苹果浏览器] 指南时失败: {e}")
        
# --- 核心处理器 7 (安卓窗口上限指南) ---
async def send_android_tab_limit_guide(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ (需求 7 - 静态回复 Android 窗口上限) """
    if not update.message or not is_chat_allowed(context, update.message.chat_id): return
    bot_token_end = context.application.bot.token[-4:]
    logger.info(f"Bot {bot_token_end} 收到 [安卓窗口上限] 关键字，发送窗口指南...")
    message = """🤖 <b>安卓/平板浏览器窗口上限解决步骤</b>

<b>1. 打开浏览器:</b> 启动您使用的浏览器 App (如 Chrome、华为浏览器、小米浏览器等)。

<b>2. 点击标签页图标:</b> 通常在地址栏旁边，会有一个显示数字的小方块图标 (例如 <code>100+</code> 或一个数字)，表示当前打开的标签页数量。

<b>3. 管理标签页:</b> 进入标签页管理界面。

<b>4. 批量关闭:</b> 寻找“关闭所有标签页”或类似的选项。多数浏览器在右上角或菜单中提供此功能。

<b>5. 或手动关闭:</b> 您也可以通过向上滑动或点击每个标签页的“x”按钮逐个关闭。"""
    try:
        await update.message.reply_html(message)
    except Exception as e:
        logger.error(f"发送 [安卓窗口上限] 指南时失败: {e}")

# --- 核心处理器 8 (苹果窗口上限指南) ---
async def send_ios_tab_limit_guide(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ (需求 8 - 静态回复 Apple 窗口上限) """
    if not update.message or not is_chat_allowed(context, update.message.chat_id): return
    bot_token_end = context.application.bot.token[-4:]
    logger.info(f"Bot {bot_token_end} 收到 [苹果窗口上限] 关键字，发送窗口指南...")
    message = """📱 <b>苹果/平板浏览器窗口上限解决步骤</b>

<b>1. 打开 Safari 浏览器。</b>

<b>2. 点击标签页图标:</b> 在屏幕底部 (iPhone 横屏或 iPad) 或右下角 (iPhone 竖屏底部) 找到两个重叠方块的图标。

<b>3. 批量关闭:</b> <b>长按</b>该标签页图标，会弹出一个菜单。选择“关闭[数字]个标签页”或“关闭所有标签页”。

<b>4. 或手动关闭:</b> 进入标签页管理界面后，向左滑动每个标签页，或者点击左上角的“X”来关闭。"""
    try:
        await update.message.reply_html(message)
    except Exception as e:
        logger.error(f"发送 [苹果窗口上限] 指南时失败: {e}")


# --- ⬇️ 新增：核心处理器 9 (全局图片) ⬇️ ---
async def send_global_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ (需求 9 - 静态回复 全局图片) """
    
    # --- ⬇️ 智能安全检查 ⬇️ ---
    if not update.message or not is_chat_allowed(context, update.message.chat_id):
        return # 不在白名单，立即停止
    # --- ⬆️ 智能安全检查 ⬆️ ---

    bot_token_end = context.application.bot.token[-4:]
    keyword = update.message.text
    logger.info(f"Bot {bot_token_end} 收到 [全局图片] 关键字: {keyword}，发送图片...")

    # 1. 查找此关键字对应的全局 URL
    image_url = GLOBAL_IMAGE_MAP.get(keyword)
            
    if not image_url:
        # 这种情况不应该发生，因为 Regex 已经匹配了
        logger.error(f"Bot (尾号: {bot_token_end}) 匹配了关键字 {keyword}，但在全局图片 MAP 中未找到 URL！")
        return
        
    try:
        # 2. 发送图片
        await update.message.reply_photo(photo=image_url)
        
    except Exception as e:
        logger.error(f"发送 [全局图片] ({keyword}) 时失败: {e}")
        await update.message.reply_text(f"❌ 发送图片时发生内部错误。")
# --- ⬆️ 新增 ⬆️ ---

# --- ⬇️ 新增：核心处理器 10 (全局视频) ⬇️ ---
async def send_global_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ (需求 10 - 静态回复 全局视频) """
    
    # --- ⬇️ 智能安全检查 ⬇️ ---
    if not update.message or not is_chat_allowed(context, update.message.chat_id):
        return # 不在白名单，立即停止
    # --- ⬆️ 智能安全检查 ⬆️ ---

    bot_token_end = context.application.bot.token[-4:]
    keyword = update.message.text
    logger.info(f"Bot {bot_token_end} 收到 [全局视频] 关键字: {keyword}，发送视频...")

    # 1. 查找此关键字对应的全局 URL
    video_url = GLOBAL_VIDEO_MAP.get(keyword)
            
    if not video_url:
        logger.error(f"Bot (尾号: {bot_token_end}) 匹配了关键字 {keyword}，但在全局视频 MAP 中未找到 URL！")
        return
        
    try:
        # 2. 发送视频
        await update.message.reply_video(video=video_url)
        
    except Exception as e:
        logger.error(f"发送 [全局视频] ({keyword}) 时失败: {e}")
        await update.message.reply_text(f"❌ 发送视频时发生内部错误。")
# --- ⬆️ 新增 ⬆️ ---


# --- 4. Bot 启动与停止逻辑 ---
def setup_bot(app_instance: Application, bot_index: int) -> None:
    """配置 Bot 的所有处理器 (Handlers)。"""
    token_end = app_instance.bot.token[-4:]
    logger.info(f"Bot Application 实例 (#{bot_index}, 尾号: {token_end}) 正在配置 Handlers。")

    # (需求 1) 处理器
    app_instance.add_handler( MessageHandler( filters.TEXT & filters.Regex(UNIVERSAL_COMMAND_PATTERN), get_universal_link ))
    # (需求 2) 处理器
    app_instance.add_handler( MessageHandler( filters.TEXT & filters.Regex(ANDROID_SPECIFIC_COMMAND_PATTERN), get_android_specific_link ))
    # (需求 3) 处理器
    app_instance.add_handler( MessageHandler( filters.TEXT & filters.Regex(IOS_QUIT_PATTERN), send_ios_quit_guide ))
    # (需求 4) 处理器
    app_instance.add_handler( MessageHandler( filters.TEXT & filters.Regex(ANDROID_QUIT_PATTERN), send_android_quit_guide ))
    # (需求 5) 处理器
    app_instance.add_handler( MessageHandler( filters.TEXT & filters.Regex(ANDROID_BROWSER_PATTERN), send_android_browser_guide ))
    # (需求 6) 处理器
    app_instance.add_handler( MessageHandler( filters.TEXT & filters.Regex(IOS_BROWSER_PATTERN), send_ios_browser_guide ))
    # (需求 7) 处理器
    app_instance.add_handler( MessageHandler( filters.TEXT & filters.Regex(ANDROID_TAB_LIMIT_PATTERN), send_android_tab_limit_guide ))
    # (需求 8) 处理器
    app_instance.add_handler( MessageHandler( filters.TEXT & filters.Regex(IOS_TAB_LIMIT_PATTERN), send_ios_tab_limit_guide ))
    
    # --- ⬇️ 新增：(需求 9) 全局图片处理器 ⬇️ ---
    if GLOBAL_IMAGE_PATTERN:
        app_instance.add_handler( MessageHandler( filters.TEXT & filters.Regex(GLOBAL_IMAGE_PATTERN), send_global_image ))
    # --- ⬆️ 新增 ⬆️ ---

    # --- ⬇️ 新增：(需求 10) 全局视频处理器 ⬇️ ---
    if GLOBAL_VIDEO_PATTERN:
        app_instance.add_handler( MessageHandler( filters.TEXT & filters.Regex(GLOBAL_VIDEO_PATTERN), send_global_video ))
    # --- ⬆️ 新增 ⬆️ ---
    
    
    async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        
        # --- ⬇️ 智能安全检查 ⬇️ ---
        if not update.message or not is_chat_allowed(context, update.message.chat_id):
            return # 不在白名单，立即停止
        # --- ⬆️ 智能安全检查 ⬆️ ---

        # (您修改后的 /start 消息)
        start_message = (f"🤖 Bot #{bot_index} (尾号: {token_end}) 已准备就绪。\n"
                       f"- 发送 `链接`、`地址` 等获取通用链接。\n"
                       f"- 发送 `安卓专用` 等获取 APK 链接。\n"
                       f"- 发送 `苹果大退` 获取 iOS 重启指南。\n"
                       f"- 发送 `安卓大退` 获取 Android 重启指南。\n"
                       f"- 发送 `安卓浏览器手机版` 获取安卓浏览器设置指南。\n"
                       f"- 发送 `苹果浏览器手机版` 获取苹果浏览器设置指南。\n"
                       f"- 发送 `安卓窗口上限` 获取安卓窗口管理指南。\n"
                       f"- 发送 `苹果窗口上限` 获取苹果窗口管理指南。")
        
        # --- ⬇️ 新增：动态添加图片/视频关键字到 /start ⬇️ ---
        if GLOBAL_IMAGE_MAP:
            start_message += "\n\n<b>--- 快捷图片 ---</b>"
            for key in list(GLOBAL_IMAGE_MAP.keys())[:3]: # (只显示前 3 个)
                start_message += f"\n- 发送 `{key}` 获取图片"
        
        if GLOBAL_VIDEO_MAP:
            start_message += "\n\n<b>--- 快捷视频 ---</b>"
            for key in list(GLOBAL_VIDEO_MAP.keys())[:3]: # (只显示前 3 个)
                start_message += f"\n- 发送 `{key}` 获取视频"
        # --- ⬆️ 新增 ⬆️ ---

        await update.message.reply_html(start_message)
    
    app_instance.add_handler(CommandHandler("start", start_command))
    

# --- 5. FastAPI 应用实例 ---
app = FastAPI(title="Multi-Bot Playwright Service")

# --- 6. 应用启动/关闭事件 ---

# --- ⬇️ 后台调度器 (已修复 <br> Bug) ⬇️ ---
async def background_scheduler():
    """每60秒检查一次是否有到期的定时任务"""
    logger.info("后台调度器已启动... (每 60 秒检查一次)")
    await asyncio.sleep(10) 

    while True:
        try:
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            current_utc_hm = now_utc.strftime("%H:%M") 
            
            for webhook_path, schedule in BOT_SCHEDULES.items():
                
                if current_utc_hm in schedule["times"]:
                    
                    last_sent_time = schedule.get("last_sent")
                    should_send = False
                    
                    if last_sent_time is None: 
                        should_send = True
                    else:
                        delta = now_utc - last_sent_time
                        if delta.total_seconds() > 3540: 
                            should_send = True
                            
                    if should_send:
                        application = BOT_APPLICATIONS.get(webhook_path)
                        if application:
                            chat_ids_list = schedule["chat_ids"] 
                            message_raw = schedule["message"] # <-- (来自 Env Var, 可能包含 <br>)
                            
                            # --- ⬇️ 关键修复：替换 <br> 标签 ⬇️ ---
                            message_formatted = message_raw.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
                            # --- ⬆️ 关键修复 ⬆️ ---
                            
                            logger.info(f"Bot (路径: {webhook_path}) 正在发送定时消息到 {len(chat_ids_list)} 个 Chats...")
                            
                            for chat_id in chat_ids_list: 
                                try:
                                    # --- ⬇️ 关键修复：发送格式化后的消息 ⬇️ ---
                                    await application.bot.send_message(chat_id=chat_id, text=message_formatted, parse_mode='HTML') 
                                    # --- ⬆️ 关键修复 ⬆️ ---
                                    logger.info(f"Bot (路径: {webhook_path}) 定时消息 -> {chat_id} 发送成功。")
                                except Exception as e:
                                    logger.error(f"Bot (路径: {webhook_path}) 发送定时消息 -> {chat_id} 失败: {e}")
                            
                            schedule["last_sent"] = now_utc 
                        else:
                            logger.warning(f"调度器：找不到 Bot Application 实例 (路径: {webhook_path})")

        except Exception as e:
            logger.error(f"后台调度器发生严重错误: {e}")
            
        await asyncio.sleep(60) # 休息 60 秒
# --- ⬆️ 后台调度器 ⬆️ ---


@app.on_event("startup")
async def startup_event():
    """在 FastAPI 启动时：1. 初始化 Bot 2. 启动 Playwright 3. 启动调度器"""
    
    global BOT_APPLICATIONS, BOT_API_URLS, BOT_APK_URLS, BOT_SCHEDULES, BOT_ALLOWED_CHATS, PLAYWRIGHT_INSTANCE, BROWSER_INSTANCE
    # --- ⬇️ 新增：初始化全局字典 ⬇️ ---
    global GLOBAL_IMAGE_MAP, GLOBAL_IMAGE_PATTERN, GLOBAL_VIDEO_MAP, GLOBAL_VIDEO_PATTERN
    # --- ⬆️ 新增 ⬆️ ---

    BOT_APPLICATIONS = {}
    BOT_API_URLS = {}
    BOT_APK_URLS = {}
    BOT_SCHEDULES = {} 
    BOT_ALLOWED_CHATS = {} # <-- 智能安全白名单
    # --- ⬇️ 新增：初始化全局字典 ⬇️ ---
    GLOBAL_IMAGE_MAP = {}
    GLOBAL_IMAGE_PATTERN = ""
    GLOBAL_VIDEO_MAP = {}
    GLOBAL_VIDEO_PATTERN = ""
    # --- ⬆️ 新增 ⬆️ ---

    logger.info("应用启动中... 正在查找所有 Bot 和全局配置。")

    # --- ⬇️ 新增：首先加载全局图片配置 ⬇️ ---
    all_global_image_keys = []
    for i in range(1, 11): # 最多支持 10 个全局图片 (IMAGE_1 ... IMAGE_10)
        keys_name = f"IMAGE_{i}_KEYS"
        url_name = f"IMAGE_{i}_URL"
        
        keys_str = os.getenv(keys_name)
        url_value = os.getenv(url_name)
        
        if keys_str and url_value:
            keys_list = [k.strip() for k in keys_str.split(',') if k.strip()]
            if keys_list:
                logger.info(f"DIAGNOSTIC: 已加载 [全局图片 {i}]: 关键字 {keys_list} -> {url_value}")
                for key in keys_list:
                    GLOBAL_IMAGE_MAP[key] = url_value
                all_global_image_keys.extend(keys_list)
            else:
                logger.warning(f"DIAGNOSTIC: {keys_name} 已设置，但关键字列表为空。")
        elif (keys_str or url_value) and not (keys_str and url_value): # (只设置了其中一个)
             logger.warning(f"DIAGNOSTIC: 必须同时提供 {keys_name} 和 {url_name} 才能加载图片 {i}。")

    if all_global_image_keys:
        escaped_keys = [re.escape(k) for k in all_global_image_keys]
        GLOBAL_IMAGE_PATTERN = r"^(" + "|".join(escaped_keys) + r")$"
        logger.info(f"✅ 成功构建 [全局图片 Regex 模式]: {GLOBAL_IMAGE_PATTERN}")
    else:
        logger.info("DIAGNOSTIC: 未配置任何全局图片。")
    # --- ⬆️ 新增 ⬆️ ---

    # --- ⬇️ 新增：加载全局视频配置 ⬇️ ---
    all_global_video_keys = []
    for i in range(1, 11): # 最多支持 10 个全局视频 (VIDEO_1 ... VIDEO_10)
        keys_name = f"VIDEO_{i}_KEYS"
        url_name = f"VIDEO_{i}_URL"
        
        keys_str = os.getenv(keys_name)
        url_value = os.getenv(url_name)
        
        if keys_str and url_value:
            keys_list = [k.strip() for k in keys_str.split(',') if k.strip()]
            if keys_list:
                logger.info(f"DIAGNOSTIC: 已加载 [全局视频 {i}]: 关键字 {keys_list} -> {url_value}")
                for key in keys_list:
                    GLOBAL_VIDEO_MAP[key] = url_value
                all_global_video_keys.extend(keys_list)
            else:
                logger.warning(f"DIAGNOSTIC: {keys_name} 已设置，但关键字列表为空。")
        elif (keys_str or url_value) and not (keys_str and url_value): # (只设置了其中一个)
             logger.warning(f"DIAGNOSTIC: 必须同时提供 {keys_name} 和 {url_name} 才能加载视频 {i}。")

    if all_global_video_keys:
        escaped_keys = [re.escape(k) for k in all_global_video_keys]
        GLOBAL_VIDEO_PATTERN = r"^(" + "|".join(escaped_keys) + r")$"
        logger.info(f"✅ 成功构建 [全局视频 Regex 模式]: {GLOBAL_VIDEO_PATTERN}")
    else:
        logger.info("DIAGNOSTIC: 未配置任何全局视频。")
    # --- ⬆️ 新增 ⬆️ ---


    # --- ⬇️ 接下来，加载所有 Bot (和之前一样) ⬇️ ---
    for i in range(1, 10): 
        token_name = f"BOT_TOKEN_{i}"
        token_value = os.getenv(token_name)
        
        # 只要有 Token，就加载 Bot
        if token_value:
            logger.info(f"DIAGNOSTIC: D 发现 Bot #{i}: Token (尾号: {token_value[-4:]})")
            
            application = Application.builder().token(token_value).build()
            application.bot_data["fastapi_app"] = app
            
            await application.initialize()
            
            # (setup_bot 现在会*自动*添加全局图片/视频处理器)
            setup_bot(application, i)
            
            webhook_path = f"bot{i}_webhook"
            BOT_APPLICATIONS[webhook_path] = application
            
            # 1. 加载 API URL (用于通用链接)
            api_url_name = f"BOT_{i}_API_URL"
            api_url_value = os.getenv(api_url_name)
            if api_url_value:
                BOT_API_URLS[webhook_path] = api_url_value 
                logger.info(f"Bot #{i} (尾号: {token_value[-4:]}) 已加载 [通用链接 API]: {api_url_value}")
            else:
                 logger.warning(f"DIAGNOSTIC: Bot #{i} 未找到 {api_url_name}。[通用链接] 功能将无法工作。")

            # 2. 加载 APK URL (用于安卓专用链接)
            apk_url_name = f"BOT_{i}_APK_URL"
            apk_url_value = os.getenv(apk_url_name)
            if apk_url_value:
                BOT_APK_URLS[webhook_path] = apk_url_value
                logger.info(f"Bot #{i} (尾号: {token_value[-4:]}) 已加载 [安卓专用模板]: {apk_url_value}")
            else:
                logger.warning(f"DIAGNOSTIC: Bot #{i} 未找到 {apk_url_name}。[安卓专用链接] 功能将无法工作。")

            # 3. 加载固定时间点配置
            schedule_chat_ids_str = os.getenv(f"BOT_{i}_SCHEDULE_CHAT_ID") 
            schedule_times_str = os.getenv(f"BOT_{i}_SCHEDULE_TIMES_UTC")
            schedule_message = os.getenv(f"BOT_{i}_SCHEDULE_MESSAGE")

            if schedule_chat_ids_str and schedule_times_str and schedule_message:
                try:
                    times_list = [t.strip() for t in schedule_times_str.split(',') if t.strip()]
                    if not times_list: raise ValueError("时间列表为空")
                    chat_ids_list = [cid.strip() for cid in schedule_chat_ids_str.split(',') if cid.strip()]
                    if not chat_ids_list: raise ValueError("Chat ID 列表为空")

                    BOT_SCHEDULES[webhook_path] = {
                        "chat_ids": chat_ids_list, 
                        "times": times_list, 
                        "message": schedule_message,
                        "last_sent": None 
                    }
                    logger.info(f"Bot #{i} (尾号: {token_value[-4:]}) 已加载 [定时任务]: 在 UTC {times_list} 发送到 {len(chat_ids_list)} 个 Chat(s)")
                except Exception as e:
                    logger.error(f"Bot #{i} 的定时任务配置错误: {e}")
            else:
                logger.info(f"Bot #{i} (尾号: {token_value[-4:]}) 未配置定时任务。")

            # 4. 加载安全白名单
            allowed_chats_name = f"BOT_{i}_ALLOWED_CHAT_IDS"
            allowed_chats_str = os.getenv(allowed_chats_name)
            if allowed_chats_str:
                chat_ids_list = [cid.strip() for cid in allowed_chats_str.split(',') if cid.strip()]
                BOT_ALLOWED_CHATS[webhook_path] = chat_ids_list
                logger.info(f"Bot #{i} (尾号: {token_value[-4:]}) 已加载 [安全白名单]: 允许 {len(chat_ids_list)} 个 Chat(s)")
            else:
                logger.warning(f"DIAGNOSTIC: Bot #{i} 未找到 {allowed_chats_name}。此 Bot 将 [不会] 响应任何群组或私聊的指令。")
                
            # (删除了每-Bot-图片加载)
                
            logger.info(f"Bot #{i} (尾号: {token_value[-4:]}) 已创建并初始化。监听路径: /{webhook_path}")

    if not BOT_APPLICATIONS:
        logger.error("❌ 未找到任何有效的 Bot Token。")
    else:
        logger.info(f"✅ 成功初始化 {len(BOT_APPLICATIONS)} 个 Bot 实例。")

    # 6.2 启动 Playwright
    logger.info("正在启动全局 Playwright 实例...")
    try:
        PLAYWRIGHT_INSTANCE = await async_playwright().start()
        BROWSER_INSTANCE = await PLAYWRIGHT_INSTANCE.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        app.state.browser = BROWSER_INSTANCE 
        logger.info("🎉 全局 Playwright Chromium 浏览器启动成功！")
    except Exception as e:
        logger.error(f"❌ 启动 Playwright 失败: {e}")
        logger.error("服务将启动，但 Playwright 功能将无法工作！")

    # 启动后台调度器
    logger.info("正在启动后台定时任务调度器...")
    asyncio.create_task(background_scheduler())

    logger.info("🎉 核心服务启动完成。等待 Telegram 的 Webhook 消息...")

@app.on_event("shutdown")
async def shutdown_event():
    """在 FastAPI 关闭时，优雅地关闭浏览器和 Playwright"""
    logger.info("应用关闭中...")
    if BROWSER_INSTANCE:
        await BROWSER_INSTANCE.close()
        logger.info("全局浏览器已关闭。")
    if PLAYWRIGHT_INSTANCE:
        await PLAYWRIGHT_INSTANCE.stop()
        logger.info("Playwright 实例已停止。")
    logger.info("应用关闭完成。")

# --- 7. 动态 Webhook 路由 (与之前相同, 100% 正确) ---
@app.post("/{webhook_path}")
async def handle_webhook(webhook_path: str, request: Request):
    if webhook_path not in BOT_APPLICATIONS:
        logger.warning(f"收到未知路径的请求: /{webhook_path}")
        return Response(status_code=404) 
    application = BOT_APPLICATIONS[webhook_path]
    try:
        update_data = await request.json()
        update = Update.de_json(update_data, application.bot)
        await application.process_update(update)
        return Response(status_code=200) # OK
    except Exception as e:
        logger.error(f"处理 Webhook 请求失败 (路径: /{webhook_path})：{e}")
        return Response(status_code=500) 

# --- 8. 健康检查路由 (与之前相同, 100% 正确) ---
@app.get("/")
async def root():
    browser_status = "未运行"
    if BROWSER_INSTANCE and BROWSER_INSTANCE.is_connected():
        browser_status = f"运行中 (Version: {BROWSER_INSTANCE.version})"

    active_bots_info = {}
    for path, app in BOT_APPLICATIONS.items():
        schedule_info = "未配置"
        if BOT_SCHEDULES.get(path):
            schedule_info = f"配置于 UTC {BOT_SCHEDULES[path]['times']} -> {len(BOT_SCHEDULES[path]['chat_ids'])} 个 Chat(s)" 
        
        allowed_info = "未配置 (不响应任何指令)"
        if BOT_ALLOWED_CHATS.get(path):
            allowed_info = f"已配置 (允许 {len(BOT_ALLOWED_CHATS[path])} 个 Chat(s))"
        
        active_bots_info[path] = {
            "token_end": app.bot.token[-4:],
            "api_url_universal": BOT_API_URLS.get(path, "未设置!"),
            "api_url_android_apk": BOT_APK_URLS.get(path, "未设置!"),
            "schedule_info": schedule_info,
            "security_allowlist": allowed_info,
        }
    status = {
        "status": "OK",
        "message": "Telegram Multi-Bot (Playwright JS + Scheduler + Security) service is running.",
        "browser_status": browser_status,
        "active_bots_count": len(BOT_APPLICATIONS),
        "global_images_loaded": len(GLOBAL_IMAGE_MAP), # <-- 新增
        "global_videos_loaded": len(GLOBAL_VIDEO_MAP), # <-- 新增
        "active_bots_info": active_bots_info
    }
    return status
