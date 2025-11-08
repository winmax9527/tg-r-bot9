import logging
import re
from playwright.async_api import async_playwright
from telegram import Update
from telegram.ext import ContextTypes, Application
from datetime import datetime, timedelta

# 配置日志
logger = logging.getLogger(__name__)

# 定义一个简单的内存缓存，用于存储生成的短链结果，避免重复抓取
# 结构: { (bot_id, link_type): (result_text, expiry_time) }
link_cache = {}
CACHE_LIFETIME_MINUTES = 10

def _extract_link_type(text: str) -> str:
    """从用户消息中提取链接类型（如：安卓, 苹果, 最新, 默认）"""
    text_lower = text.lower()
    
    # 定义匹配模式和对应的类型
    if '安卓' in text_lower or 'android' in text_lower:
        return 'android'
    elif '苹果' in text_lower or 'ios' in text_lower:
        return 'ios'
    elif '最新' in text_lower:
        return 'latest'
    else:
        return 'default' # 默认地址或链接

async def _fetch_links_from_api_url(api_url: str) -> str | None:
    """使用 Playwright 访问 API_URL，并尝试抓取所有链接作为内容"""
    try:
        # 使用 Chromium 启动 Playwright
        async with async_playwright() as p:
            # 启动一个无头浏览器实例
            browser = await p.chromium.launch(headless=True)
            # 创建一个新页面
            page = await browser.new_page()

            logger.info(f"Playwright 正在访问目标 URL: {api_url}")
            
            # 导航到目标 URL，等待网络空闲 (networkidle)，提高抓取成功率
            response = await page.goto(api_url, wait_until="networkidle")
            
            if response and response.status != 200:
                logger.error(f"访问 URL 失败，HTTP 状态码: {response.status}")
                await browser.close()
                return None

            # 等待 3 秒，确保所有动态内容加载完成
            await page.wait_for_timeout(3000)

            # --- 核心抓取逻辑 ---
            # 1. 抓取页面上的所有链接 (href 属性)
            links = await page.evaluate('''() => {
                const anchors = Array.from(document.querySelectorAll('a'));
                return anchors.map(a => a.href).filter(href => href && href !== '#');
            }''')

            # 2. 抓取页面上的所有可见文本 (作为上下文，防止页面只展示链接)
            all_text = await page.locator('body').inner_text()
            
            await browser.close()

            # 整理结果文本
            result_text = "--- 页面链接信息 ---\n"
            
            if all_text.strip():
                # 提取重要的文本内容，例如去除多余空白行
                result_text += "抓取的关键文本:\n"
                key_text = '\n'.join(
                    line.strip() for line in all_text.split('\n') if line.strip()
                )[:1000] # 限制长度，避免过长
                result_text += key_text + "\n"

            if links:
                result_text += "\n抓取到的所有有效链接:\n"
                # 对链接进行去重和排序
                unique_links = sorted(list(set(links)))
                result_text += '\n'.join(unique_links)
            
            if not all_text.strip() and not links:
                 return "未能从目标页面抓取到有效内容或链接。"
                 
            return result_text

    except Exception as e:
        logger.error(f"Playwright 抓取链接时发生错误: {e}")
        return None


def _format_link_response(fetch_result: str | None, bot_id: int, api_url: str) -> str:
    """根据抓取结果格式化回复消息"""
    if fetch_result:
        # 成功抓取，返回抓取到的内容
        return (
            f"✅ Bot {bot_id} 最新地址/链接已抓取成功:\n\n"
            f"{fetch_result}\n\n"
            f"数据缓存 {CACHE_LIFETIME_MINUTES} 分钟，请稍后再试以获取更新。"
        )
    else:
        # 抓取失败
        return (
            f"❌ Bot {bot_id} 抱歉，未能成功获取最新地址或链接。\n"
            f"请检查目标网站 ({api_url}) 是否可访问，或稍后再试。"
        )

# --- Telegram Handler 函数 ---
async def generate_short_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    处理用户发送的 '地址', '链接', '最新地址' 等消息，并回复抓取到的链接信息。
    """
    if not update.message:
        return

    chat_id = update.effective_chat.id
    user_message = update.message.text
    
    # 从 bot_data 中获取配置信息
    api_url = context.application.bot_data.get('API_URL')
    bot_id = context.application.bot_data.get('BOT_ID')

    if not api_url or not bot_id:
        await update.message.reply_text("系统配置错误：缺少 API_URL 或 BOT_ID。")
        return

    # 1. 提取链接类型 (虽然目前逻辑不区分类型，但保留结构以备扩展)
    link_type = _extract_link_type(user_message)
    cache_key = (bot_id, link_type)
    
    logger.info(f"Bot {bot_id} 收到消息: '{user_message}' (类型: {link_type})")

    # 2. 检查缓存
    now = datetime.now()
    if cache_key in link_cache:
        result, expiry_time = link_cache[cache_key]
        if now < expiry_time:
            logger.info(f"Bot {bot_id} 命中缓存，直接回复。")
            await update.message.reply_text(
                f"ℹ️ Bot {bot_id} 链接 (缓存) 信息:\n\n{result}"
            )
            return

    # 3. 抓取链接
    await update.message.reply_text(f"⏳ Bot {bot_id} 正在访问目标网站 ({api_url})，请稍候...")
    
    fetch_result = await _fetch_links_from_api_url(api_url)
    
    # 4. 格式化回复
    response_text = _format_link_response(fetch_result, bot_id, api_url)
    
    # 5. 更新缓存 (只有成功抓取到内容才缓存)
    if fetch_result:
        expiry_time = now + timedelta(minutes=CACHE_LIFETIME_MINUTES)
        link_cache[cache_key] = (fetch_result, expiry_time)

    # 6. 发送回复
    await update.message.reply_text(response_text)
