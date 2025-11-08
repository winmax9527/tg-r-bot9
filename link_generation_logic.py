import logging
import asyncio
from playwright.async_api import async_playwright
import httpx # 确保 httpx 已经安装

# --- 日志配置 ---
logger = logging.getLogger(__name__)

async def resolve_url_logic(api_url: str, bot_id: str) -> tuple[str | None, str]:
    """
    使用 Playwright 访问 API URL，抓取最新下载链接，并返回结果。
    
    Args:
        api_url: 机器人需要访问的最新地址 API (域名 A)。
        bot_id: 当前机器人的 ID (用于日志)。

    Returns:
        (final_url, reply_message)
    """
    
    final_url = None
    reply_message = ""

    # 使用 asyncio.wait_for 确保 Playwright 操作不会无限期挂起
    try:
        async with async_playwright() as p:
            # 在 Render 上，必须使用 headless=True 运行浏览器
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()

            logger.info(f"Bot {bot_id}: Navigating to API URL (Domain A): {api_url}")
            
            # 访问 API 页面，超时设为 15 秒
            # Playwright 会处理 JS 跳转
            await page.goto(api_url, timeout=15000, wait_until="networkidle")

            # 假设页面执行 JS 后，跳转到了最终链接 (域名 B)
            # 我们直接获取跳转后的当前 URL
            
            # 等待 2 秒，给 JS 充足的跳转时间
            await asyncio.sleep(2) 
            
            # 获取最终跳转后的 URL
            current_url = page.url
            
            # 如果 URL 与初始 API URL 不同，则认为是成功跳转
            if current_url != api_url:
                final_url = current_url
            else:
                # 如果没有跳转，可能需要从页面内容中解析链接 (这是最复杂的部分)
                # 假设您的 API 域名 A 直接返回了 JSON 而不是 HTML (这是我们上一轮讨论的复杂情况)
                
                # --- 更正：Playwright 处理的是 HTML 页面，如果返回 JSON，需要用 httpx ---
                
                # 为了简化并遵守您的“域名 A (JSON) -> 域名 B (JS)”逻辑，
                # 我们假设 API_URL (域名 A) 返回一个 JSON 字符串，其中包含一个 JS 跳转页的URL。
                
                # 步骤 1: 使用 httpx 获取 API (域名 A) 返回的 JSON 数据
                async with httpx.AsyncClient(timeout=10) as client:
                    response = await client.get(api_url)
                    response.raise_for_status() # 如果请求失败则抛出异常
                    data = response.json()
                    
                    # !!! 假设 JSON 结构为 {"redirect_url": "跳转URL"} !!!
                    # !!! 请根据您的实际 JSON 结构修改这里的键名 !!!
                    intermediate_url = data.get("redirect_url")
                    
                    if not intermediate_url:
                         reply_message = f"❌ API (域名 A) 响应成功，但 JSON 中未找到 'redirect_url' 键。"
                         await browser.close()
                         return final_url, reply_message

                    logger.info(f"Bot {bot_id}: JSON received. Intermediate URL: {intermediate_url}")

                    # 步骤 2: 使用 Playwright 访问中间页并等待 JS 跳转
                    await page.goto(intermediate_url, timeout=15000, wait_until="networkidle")
                    
                    # 等待 JS 跳转完成
                    await asyncio.sleep(3) 
                    
                    final_url = page.url
                    
                    if final_url == intermediate_url:
                         reply_message = f"⚠️ 页面未发生 JS 跳转。请访问：{intermediate_url}"
                         final_url = None
                    
                
            # 关闭浏览器
            await browser.close()

    except TimeoutError:
        logger.error(f"Bot {bot_id}: Playwright operation timed out.")
        reply_message = f"❌ 机器人连接超时。API 或跳转页面响应慢。请稍后再试或访问：{api_url}"
    except httpx.HTTPStatusError as e:
        logger.error(f"Bot {bot_id}: HTTP Status Error: {e}")
        reply_message = f"❌ API (域名 A) 访问失败，状态码: {e.response.status_code}。"
    except Exception as e:
        logger.error(f"Bot {bot_id}: 核心逻辑发生未知错误: {e}")
        reply_message = f"❌ 机器人运行时发生未知错误：{e}. 请联系管理员。"
    
    
    # 构造最终回复
    if final_url:
        reply_message = f"🎉 **Bot {bot_id} 找到最新链接！**\n\n"
        reply_message += f"🔗 最新下载地址: {final_url}\n\n"
        reply_message += f"➡️ 备用访问地址: {api_url}"
    elif not reply_message:
        reply_message = "❌ 机器人未能找到最新链接，但未发生崩溃。请检查 API 配置或手动访问。"

    return final_url, reply_message
