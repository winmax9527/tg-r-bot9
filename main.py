import os
import logging
import asyncio
import re
import random
import string
import datetime
from urllib.parse import urlparse
from typing import List, Dict, Any

# 🔥 核心依赖
import httpx 
from fastapi import FastAPI, Request, Response
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import BadRequest

# 引入 Playwright
from playwright.async_api import async_playwright, Playwright, Browser, TimeoutError as PlaywrightTimeoutError

# 🔥 [新增] 引入安全计算库
from simpleeval import simple_eval

# ==============================================================================
# 1. 日志配置 (优化部分：降噪模式)
# ==============================================================================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
# 获取我们自己的 Logger，起个名字叫 'BotLogic' 方便识别
logger = logging.getLogger("BotLogic")

# 🔥 关键修改：让第三方库闭嘴 (降噪) 🔥
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

# --- 2. 全局状态 ---
BOT_APPLICATIONS: Dict[str, Application] = {}
BOT_API_URLS: Dict[str, str] = {}
BOT_APK_URLS: Dict[str, str] = {}
BOT_SCHEDULES: Dict[str, Dict[str, Any]] = {} 
BOT_ALLOWED_CHATS: Dict[str, List[str]] = {} 
PLAYWRIGHT_INSTANCE: Playwright | None = None
BROWSER_INSTANCE: Browser | None = None

# 全局 HTTP 客户端
GLOBAL_HTTP_CLIENT: httpx.AsyncClient | None = None

# 🔥 核心修复：并发锁 (Semaphore)
# 限制同一时间只能有 1 个浏览器在运行，防止内存炸裂
BROWSER_LOCK = asyncio.Semaphore(1)

# 全局图片/视频
GLOBAL_IMAGE_MAP: Dict[str, str] = {} 
GLOBAL_IMAGE_PATTERN: str = "" 
GLOBAL_VIDEO_MAP: Dict[str, str] = {} 
GLOBAL_VIDEO_PATTERN: str = "" 

# --- 3. 核心正则 (保持不变) ---
UNIVERSAL_COMMAND_PATTERN = r"^(地址|安装地址|安装链接|下载地址|下载链接|最新地址|安卓地址|苹果地址|安卓下载地址|苹果下载地址|链接|最新链接|安卓链接|安卓下载链接|最新安卓链接|苹果链接|苹果下载链接|ios链接|最新苹果链接)$"
ANDROID_SPECIFIC_COMMAND_PATTERN = r"^(提包|安卓专用|安卓专用链接|安卓提包链接|安卓专用地址|安卓提包地址|安卓专用下载|安卓提包)$"
IOS_QUIT_PATTERN = r"^(苹果大退|苹果重启|苹果大退重启|苹果黑屏|苹果重开)$"
ANDROID_QUIT_PATTERN = r"^(安卓大退|安卓重启|安卓大退重启|安卓黑屏|安卓重开|大退|重开|闪退|卡了|黑屏)$"
ANDROID_BROWSER_PATTERN = r"^(安卓浏览器手机版|安卓桌面版|安卓浏览器|浏览器设置)$"
IOS_BROWSER_PATTERN = r"^(苹果浏览器手机版|苹果浏览器|苹果桌面版)$"
ANDROID_TAB_LIMIT_PATTERN = r"^(安卓窗口上限|窗口上限|标签上限)$"
IOS_TAB_LIMIT_PATTERN = r"^(苹果窗口上限|苹果标签上限)$"
# 🔥 [新增] 新股查询正则
IPO_COMMAND_PATTERN = r"^(新股|新股申购|新股上市|近期新股|申购|上市)$"


# --- 辅助函数 ---

def is_chat_allowed(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    current_app = context.application
    allowed_list: List[str] = []
    
    for path, app_instance in BOT_APPLICATIONS.items():
        if app_instance is current_app:
            allowed_list = BOT_ALLOWED_CHATS.get(path, [])
            break
            
    chat_id_str = str(chat_id)
    possible_ids_to_check = {chat_id_str} 
    if chat_id_str.startswith("-100"):
        short_id = f"-{chat_id_str[4:]}"
        possible_ids_to_check.add(short_id)
    elif chat_id_str.startswith("-"):
        long_id = f"-100{chat_id_str[1:]}"
        possible_ids_to_check.add(long_id)

    for check_id in possible_ids_to_check:
        if check_id in allowed_list:
            return True 

    return False

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
        new_parsed = parsed._replace(netloc=new_netloc)
        return new_parsed.geturl()
    except Exception:
        return url_str

async def safe_reply(update: Update, text: str, parse_mode=None):
    try:
        if parse_mode:
            await update.message.reply_text(text, parse_mode=parse_mode)
        else:
            await update.message.reply_text(text)
    except BadRequest as e:
        if "Message to be replied not found" in str(e):
            try:
                await update.message.chat.send_message(text, parse_mode=parse_mode)
            except Exception:
                pass
        else:
            pass
    except Exception:
        pass


# --- 核心处理器 1 (Playwright - 通用链接) ---
async def get_universal_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_chat_allowed(context, update.message.chat_id):
        return

    # 🔥 获取 Bot ID (身份证)
    bot_id = context.bot_data.get("bot_index", "?")
    chat_id = update.message.chat_id

    # 📝 日志：带上 Bot ID
    logger.info(f"🤖 [Bot #{bot_id}] 📨 收到请求 | 用户 {chat_id}")

    fastapi_app = context.bot_data.get("fastapi_app")
    if not fastapi_app or not hasattr(fastapi_app.state, 'browser'):
        logger.error(f"🤖 [Bot #{bot_id}] ❌ 浏览器实例未找到")
        await safe_reply(update, "❌ 服务内部错误：浏览器未启动。")
        return

    current_app = context.application
    api_url = None
    for path, app_instance in BOT_APPLICATIONS.items():
        if app_instance is current_app:
            api_url = BOT_API_URLS.get(path)
            break
            
    if not api_url:
        logger.error(f"🤖 [Bot #{bot_id}] ❌ 未找到 API 配置")
        await safe_reply(update, "❌ 配置错误：未找到此 Bot 的 API 地址。")
        return

    # 尝试发送“请稍候”
    try:
        await safe_reply(update, "正在为您获取专属通用下载链接，请稍候 ...")
    except Exception:
        pass

    user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    browser_context = None 
    page = None 
    
    # 🔥 核心修改：加锁！排队！
    async with BROWSER_LOCK:
        try:
            # --- 步骤 1: [httpx] 使用全局 Client ---
            if GLOBAL_HTTP_CLIENT is None:
                 raise RuntimeError("Global HTTP Client not initialized")

            logger.info(f"🤖 [Bot #{bot_id}] 🔄 [步骤 1] 正访问 API: {api_url}")
            resp = await GLOBAL_HTTP_CLIENT.get(api_url, headers={'User-Agent': user_agent})
            resp.raise_for_status()
            api_data = resp.json()

            if api_data.get("code") != 0 or "data" not in api_data:
                logger.warning(f"🤖 [Bot #{bot_id}] ❌ API 返回无效: {api_data}")
                await safe_reply(update, "❌ API 未返回有效链接。")
                return

            domain_a = api_data["data"].strip()
            if not domain_a.startswith(('http://', 'https://')):
                domain_a = 'http://' + domain_a
            
            logger.info(f"🤖 [Bot #{bot_id}] ✅ [步骤 1 成功] 获取到入口: {domain_a}")

            # --- 步骤 2: [Playwright] ---
            logger.info(f"🤖 [Bot #{bot_id}] 🚀 [步骤 2] 启动浏览器页面...")
            browser_context = await fastapi_app.state.browser.new_context(
                user_agent=user_agent,
                viewport={'width': 1280, 'height': 800}
            )
            
            page = await browser_context.new_page()
            page.set_default_timeout(30000) # 30秒超时

            try:
                logger.info(f"🤖 [Bot #{bot_id}] 🌍 [步骤 2] 浏览器跳转: {domain_a}")
                await page.goto(domain_a, wait_until="domcontentloaded")
                try:
                    await page.wait_for_timeout(1500)
                except:
                    pass

            except PlaywrightTimeoutError:
                logger.error(f"🤖 [Bot #{bot_id}] ❌ 浏览器访问超时")
                await safe_reply(update, "❌ 源站响应太慢，请重试。")
                return 
            except Exception as e:
                logger.error(f"🤖 [Bot #{bot_id}] ❌ 浏览器访问出错: {e}")
                await safe_reply(update, "❌ 无法连接到源站。")
                return 
            
            domain_b = page.url 
            logger.info(f"🤖 [Bot #{bot_id}] ✅ [步骤 2 成功] 落地页: {domain_b}")

            if "chrome-error://" in domain_b or "chromewebdata" in domain_b:
                await safe_reply(update, "⚠️ 线路维护中，请稍后再试。")
                return

            # --- 步骤 3: 修改域名 ---
            random_sub = generate_universal_subdomain()
            final_url = modify_url_subdomain(domain_b, random_sub)

            msg = (
                "✅ <b>您的专属通用下载链接已生成！</b>\n"
                "👇 <b>点击下方链接即可复制：</b>\n"
                f"<code>{final_url}</code>" 
                "\n💡 <i>请务必在手机自带浏览器中打开</i>"
            )
            await safe_reply(update, msg, parse_mode='HTML')
            logger.info(f"🤖 [Bot #{bot_id}] 🎉 [完成] 已发送最终链接: {final_url}")

        except httpx.TimeoutException:
            logger.error(f"🤖 [Bot #{bot_id}] ❌ HTTP请求超时")
            await safe_reply(update, "❌ 获取链接超时，对方服务器响应太慢，请重试。")
        except Exception as e:
            logger.error(f"🤖 [Bot #{bot_id}] ❌ 未知处理错误: {e}")
            await safe_reply(update, "❌ 系统繁忙，请重试。")
            
        finally:
            if page:
                try: await page.close()
                except: pass
            if browser_context:
                try: await browser_context.close()
                except: pass


# --- 核心处理器 2 (安卓专用) ---
async def get_android_specific_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_chat_allowed(context, update.message.chat_id): return
    
    current_app = context.application
    apk_template = None
    for path, app_instance in BOT_APPLICATIONS.items():
        if app_instance is current_app:
            apk_template = BOT_APK_URLS.get(path)
            break
            
    if not apk_template:
        await safe_reply(update, "❌ 配置错误：未找到 APK 模板。")
        return
        
    try:
        random_sub = generate_android_specific_subdomain()
        final_url = apk_template.replace("*", random_sub, 1)
        msg = (
            "✅ <b>您的专属安卓专用链接已生成！</b>\n"
            "👇 <b>点击下方链接即可复制：</b>\n"
            f"<code>{final_url}</code>"
            "\n💡 <i>请务必在手机自带浏览器中打开</i>"
        )
        await safe_reply(update, msg, parse_mode='HTML')
    except Exception as e:
        logger.error(f"APK 生成错误: {e}")

# --- 其他静态回复处理器 ---
async def send_static_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, log_msg: str, html_msg: str):
    if not update.message or not is_chat_allowed(context, update.message.chat_id): return
    try: 
        await safe_reply(update, html_msg, parse_mode='HTML')
    except Exception: pass

async def send_ios_quit_guide(u, c): await send_static_reply(u, c, "发送苹果大退", "📱 <b>苹果手机APP大退步骤</b>\n\n1. 上滑停留调出后台。\n2. 上滑关闭App卡片。\n3. 重新点击图标打开。")
async def send_android_quit_guide(u, c): await send_static_reply(u, c, "发送安卓大退", "🤖 <b>安卓手机APP大退步骤</b>\n\n1. 上滑或点击多任务键进入后台。\n2. 上滑关闭App卡片。\n3. 重新打开App。")
async def send_android_browser_guide(u, c): await send_static_reply(u, c, "发送安卓浏览器", "🤖 <b>安卓浏览器设置手机版</b>\n\n1. 打开浏览器菜单(≡或⋮)。\n2. 找到“桌面版”或“电脑模式”。\n3. <b>取消勾选</b>它。")
async def send_ios_browser_guide(u, c): await send_static_reply(u, c, "发送苹果浏览器", "📱 <b>苹果浏览器设置手机版</b>\n\n1. 点击地址栏左侧(大小/AA)。\n2. 选择“请求移动网站”。\n(如果显示“请求桌面网站”则无需操作)")
async def send_android_tab_limit_guide(u, c): await send_static_reply(u, c, "发送安卓窗口上限", "🤖 <b>安卓窗口上限解决</b>\n\n1. 点击浏览器标签页图标(数字框)。\n2. 选择“关闭所有标签页”或手动关闭旧标签。")
async def send_ios_tab_limit_guide(u, c): await send_static_reply(u, c, "发送苹果窗口上限", "📱 <b>苹果窗口上限解决</b>\n\n1. 长按右下角标签图标。\n2. 选择“关闭所有标签页”。")

# --- 图片/视频处理器 ---
async def send_global_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_chat_allowed(context, update.message.chat_id): return
    keyword = update.message.text
    url = GLOBAL_IMAGE_MAP.get(keyword)
    if url: 
        try: await update.message.reply_photo(photo=url)
        except Exception: pass

async def send_global_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_chat_allowed(context, update.message.chat_id): return
    keyword = update.message.text
    url = GLOBAL_VIDEO_MAP.get(keyword)
    if url: 
        try: await update.message.reply_video(video=url)
        except Exception: pass


# --- 🔥 计算器逻辑 ---
def safe_calculate(expression: str):
    """安全计算逻辑 - 支持文本混合模式"""
    try:
        # --- 1. 预处理：清洗数据 (关键修改) ---
        cleaned_expr = re.sub(r'[^\d\+\-\*\/\(\)\.\%\^]', '', expression)

        # --- 2. 检查清洗后的算式是否有效 ---
        if not cleaned_expr:
            return None 
        
        # 边缘情况防止：如果清洗完只剩一个小数点或运算符
        if len(cleaned_expr) == 1 and cleaned_expr in '+-*/.^%':
            return None

        # --- 3. 符号标准化 ---
        final_expr = cleaned_expr.replace('^', '**')
        
        # --- 4. 长度限制 ---
        if len(final_expr) > 100:
            return "❌ 算式太长了。"

        # --- 5. 执行计算 ---
        result = simple_eval(final_expr)
        
        # 格式化输出：如果是整数，去掉 .0
        if isinstance(result, float) and result.is_integer():
            result = int(result)

        return f"🔢 结果: {result}"

    except SyntaxError:
        return "❌ 格式错误 (请检查运算符)"
    except ZeroDivisionError:
        return "❌ 不能除以零"
    except Exception:
        return None

# --- 🔥 [最终方案] 浏览器去广告截图版 (无需注册/无需Token) ---
async def get_stock_ipo_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 引用全局浏览器实例
    global BROWSER_INSTANCE

    bot_id = context.bot_data.get("bot_index", "?")
    
    # 1. 检查浏览器是否就绪
    if not BROWSER_INSTANCE:
        if hasattr(app.state, 'browser') and app.state.browser:
            BROWSER_INSTANCE = app.state.browser
        else:
            logger.error(f"🤖 [Bot #{bot_id}] ❌ 浏览器未启动")
            await safe_reply(update, "❌ 浏览器服务未就绪，请联系管理员。")
            return

    await safe_reply(update, "🔍 正在启动浏览器访问 (自动去广告模式)...")
    
    target_url = "https://data.eastmoney.com/xg/xg/default.html"
    page = None
    browser_context = None

    # 加锁
    async with BROWSER_LOCK:
        try:
            logger.info(f"🤖 [Bot #{bot_id}] 🚀 访问东财页面: {target_url}")
            
            # 创建新页面 (1920x1200)
            browser_context = await BROWSER_INSTANCE.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                viewport={'width': 1920, 'height': 1200},
                device_scale_factor=1.5
            )
            page = await browser_context.new_page()
            
            # 访问页面 (domcontentloaded 即可，不等广告图片加载完)
            await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            
            # ⏳ 稍微等一下，让弹窗弹出来，这样我们才能关掉它
            await asyncio.sleep(2)

            # 🔥🔥🔥 [核心] 暴力去广告/关弹窗脚本 🔥🔥🔥
            logger.info("🔪 执行去广告脚本...")
            await page.evaluate('''() => {
                // 1. 尝试点击常见的关闭按钮 (class 包含 close)
                const closers = document.querySelectorAll('.u-mask-close, .frame-close, [class*="close"]');
                closers.forEach(el => {
                    console.log("点击关闭按钮:", el);
                    el.click();
                });

                // 2. 暴力删除所有遮罩层 (mask) 和 弹窗 (popup)
                // 东财的广告通常在 .u-mask 或 .activity-modal 里
                const trashes = document.querySelectorAll('.u-mask, .activity-modal, [class*="popup"], [class*="modal"]');
                trashes.forEach(el => el.remove());
                
                // 3. 强制把表格区域显示出来 (防止被隐藏)
                const table = document.querySelector('table');
                if(table) table.style.visibility = 'visible';
            }''')
            
            # 再睡 1 秒等页面刷新
            await asyncio.sleep(1)

            # 📸 步骤 1: 截图 (这时候应该是干净的表格了)
            try:
                screenshot_bytes = await page.screenshot(full_page=False)
                await update.message.reply_photo(photo=screenshot_bytes, caption="📸 实时申购日历 (已去广告)")
            except Exception as e:
                logger.error(f"截图失败: {e}")

            # 📝 步骤 2: 提取文字 (双重保险)
            stocks_data = await page.evaluate('''() => {
                const rows = document.querySelectorAll('table tbody tr');
                const data = [];
                rows.forEach(row => {
                    const cells = row.querySelectorAll('td');
                    if (cells.length < 5) return;
                    
                    const getText = (idx) => cells[idx] ? cells[idx].innerText.trim() : "";
                    
                    // 东财表格结构：Code(1), Name(2), DateInfo(整行)
                    data.push({
                        code: getText(1),
                        name: getText(2),
                        full_text: row.innerText
                    });
                });
                return data;
            }''')

            # --- Python 端数据清洗 ---
            today_str = datetime.datetime.now().strftime("%m-%d")
            msg_list = []
            
            for item in stocks_data:
                full_text = item['full_text']
                code = item['code']
                name = item['name']
                
                if not code or not code.isdigit(): continue

                # 提取日期 (格式如 12-06)
                dates = re.findall(r'\d{2}-\d{2}', full_text)
                
                # 筛选今天及以后的
                is_future = False
                found_date = ""
                for d in dates:
                    if d >= today_str:
                        is_future = True
                        found_date = d
                        break
                
                if is_future:
                   msg_list.append(f"• <code>{code}</code> <b>{name}</b> ({found_date})")

            if msg_list:
                msg_list = sorted(list(set(msg_list)))
                final_msg = "📅 <b>近期新股数据</b>\n" + "\n".join(msg_list)
                await safe_reply(update, final_msg, parse_mode='HTML')
            else:
                await safe_reply(update, "📭 截图如上。文字提取暂无数据 (可能是近期确实无新股)。")

        except Exception as e:
            logger.error(f"浏览器操作报错: {e}")
            await safe_reply(update, f"❌ 访问出错: {e}")
        finally:
            if page: 
                try: await page.close()
                except: pass
            if browser_context: 
                try: await browser_context.close()
                except: pass
        
# --- 🔥 [修改后] 计算器 Bot 设置 (支持连续计算 + 新股查询) ---
def setup_calculator_bot(app_instance: Application) -> None:
    """初始化计算器 Bot 的 Handler"""
    
    async def calc_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await safe_reply(update, "👋 我是智能计算器。\n\n1️⃣ 发送算式 (如 `100 * 5`)\n2️⃣ 回复结果 `/2` 可继续计算。\n3️⃣ 发送 <b>新股</b> 查询A股申购/上市列表。", parse_mode='HTML')

    async def calc_handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.message or not update.message.text: return
        
        user_text = update.message.text.strip()
        
        # ⚠️ 忽略掉真正的 /start 命令，防止报错
        if user_text.startswith("/start"):
            return

        final_expression = user_text

        # --- 连续计算逻辑 ---
        if update.message.reply_to_message and update.message.reply_to_message.text:
            # 匹配运算符开头 (+ - * / ^)
            if re.match(r'^[\+\-\*\/\^]', user_text):
                reply_text = update.message.reply_to_message.text
                
                # 1. 尝试提取 "🔢 结果: 123"
                match = re.search(r"结果:\s*(-?\d+(\.\d+)?)", reply_text)
                
                previous_num = None
                if match:
                    previous_num = match.group(1)
                # 2. 尝试提取纯数字
                elif re.match(r'^-?\d+(\.\d+)?$', reply_text.strip()):
                    previous_num = reply_text.strip()

                if previous_num:
                    final_expression = f"{previous_num}{user_text}"
                    logger.info(f"🔗 触发连续计算: {final_expression}")

        # --- 计算 ---
        result = safe_calculate(final_expression)
        if result:
            await safe_reply(update, result)

    # 1. 先注册 /start 命令
    app_instance.add_handler(CommandHandler("start", calc_start))
    
    # 2. 🔥 优先注册新股查询 (防止 "新股" 两个字进入计算逻辑报错)
    app_instance.add_handler(MessageHandler(filters.TEXT & filters.Regex(IPO_COMMAND_PATTERN), get_stock_ipo_info))

    # 3. 最后注册通用文本计算
    app_instance.add_handler(MessageHandler(filters.TEXT, calc_handle_message))

# --- Setup Bot (原有) ---
def setup_bot(app_instance: Application, bot_index: int) -> None:
    token_end = app_instance.bot.token[-4:]
    
    app_instance.add_handler(MessageHandler(filters.TEXT & filters.Regex(UNIVERSAL_COMMAND_PATTERN), get_universal_link))
    app_instance.add_handler(MessageHandler(filters.TEXT & filters.Regex(ANDROID_SPECIFIC_COMMAND_PATTERN), get_android_specific_link))
    app_instance.add_handler(MessageHandler(filters.TEXT & filters.Regex(IOS_QUIT_PATTERN), send_ios_quit_guide))
    app_instance.add_handler(MessageHandler(filters.TEXT & filters.Regex(ANDROID_QUIT_PATTERN), send_android_quit_guide))
    app_instance.add_handler(MessageHandler(filters.TEXT & filters.Regex(ANDROID_BROWSER_PATTERN), send_android_browser_guide))
    app_instance.add_handler(MessageHandler(filters.TEXT & filters.Regex(IOS_BROWSER_PATTERN), send_ios_browser_guide))
    app_instance.add_handler(MessageHandler(filters.TEXT & filters.Regex(ANDROID_TAB_LIMIT_PATTERN), send_android_tab_limit_guide))
    app_instance.add_handler(MessageHandler(filters.TEXT & filters.Regex(IOS_TAB_LIMIT_PATTERN), send_ios_tab_limit_guide))

    if GLOBAL_IMAGE_PATTERN:
        app_instance.add_handler(MessageHandler(filters.TEXT & filters.Regex(GLOBAL_IMAGE_PATTERN), send_global_image))
    if GLOBAL_VIDEO_PATTERN:
        app_instance.add_handler(MessageHandler(filters.TEXT & filters.Regex(GLOBAL_VIDEO_PATTERN), send_global_video))
    
    async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not is_chat_allowed(context, update.message.chat_id): return
        msg = f"🤖 Bot #{bot_index} ({token_end}) 就绪。"
        await safe_reply(update, msg, parse_mode='HTML')
    
    app_instance.add_handler(CommandHandler("start", start_command))

# --- FastAPI & Startup ---
app = FastAPI(title="Multi-Bot Service")

async def background_scheduler():
    logger.info("后台调度器启动...")
    await asyncio.sleep(10)
    while True:
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            curr_hm = now.strftime("%H:%M")
            for path, sched in BOT_SCHEDULES.items():
                if curr_hm in sched["times"]:
                    last = sched.get("last_sent")
                    if last is None or (now - last).total_seconds() > 3500:
                        app_inst = BOT_APPLICATIONS.get(path)
                        if app_inst:
                            msg = sched["message"].replace("<br>", "\n").replace("<br/>", "\n")
                            for cid in sched["chat_ids"]:
                                try: await app_inst.bot.send_message(chat_id=cid, text=msg, parse_mode='HTML')
                                except: pass
                            sched["last_sent"] = now
        except Exception:
            pass
            
        await asyncio.sleep(60)

@app.on_event("startup")
async def startup_event():
    global BOT_APPLICATIONS, BOT_API_URLS, BOT_APK_URLS, BOT_SCHEDULES, BOT_ALLOWED_CHATS, PLAYWRIGHT_INSTANCE, BROWSER_INSTANCE
    global GLOBAL_IMAGE_MAP, GLOBAL_IMAGE_PATTERN, GLOBAL_VIDEO_MAP, GLOBAL_VIDEO_PATTERN
    global GLOBAL_HTTP_CLIENT

    GLOBAL_HTTP_CLIENT = httpx.AsyncClient(timeout=30.0, verify=False, limits=httpx.Limits(max_keepalive_connections=20, max_connections=50))

    BOT_APPLICATIONS = {}
    BOT_API_URLS = {}
    BOT_APK_URLS = {}
    BOT_SCHEDULES = {} 
    BOT_ALLOWED_CHATS = {} 
    GLOBAL_IMAGE_MAP = {}
    GLOBAL_VIDEO_MAP = {}

    for i in range(1, 11):
        k, v = os.getenv(f"IMAGE_{i}_KEYS"), os.getenv(f"IMAGE_{i}_URL")
        if k and v: 
            for key in k.split(','): 
                if key.strip(): GLOBAL_IMAGE_MAP[key.strip()] = v
        
        k, v = os.getenv(f"VIDEO_{i}_KEYS"), os.getenv(f"VIDEO_{i}_URL")
        if k and v: 
            for key in k.split(','): 
                if key.strip(): GLOBAL_VIDEO_MAP[key.strip()] = v

    if GLOBAL_IMAGE_MAP:
        GLOBAL_IMAGE_PATTERN = r"^(" + "|".join([re.escape(k) for k in GLOBAL_IMAGE_MAP.keys()]) + r")$"
    if GLOBAL_VIDEO_MAP:
        GLOBAL_VIDEO_PATTERN = r"^(" + "|".join([re.escape(k) for k in GLOBAL_VIDEO_MAP.keys()]) + r")$"

    # --- 原有 Bots (1-9) 初始化 ---
    for i in range(1, 10):
        token = os.getenv(f"BOT_TOKEN_{i}")
        if token:
            application = Application.builder().token(token).build()
            application.bot_data["fastapi_app"] = app
            application.bot_data["bot_index"] = i 
            await application.initialize()
            setup_bot(application, i)
            
            path = f"bot{i}_webhook"
            BOT_APPLICATIONS[path] = application
            
            if url := os.getenv(f"BOT_{i}_API_URL"): BOT_API_URLS[path] = url
            if url := os.getenv(f"BOT_{i}_APK_URL"): BOT_APK_URLS[path] = url
            
            if al := os.getenv(f"BOT_{i}_ALLOWED_CHAT_IDS"):
                BOT_ALLOWED_CHATS[path] = [cid.strip() for cid in al.split(',') if cid.strip()]
            
            s_cids = os.getenv(f"BOT_{i}_SCHEDULE_CHAT_ID")
            s_times = os.getenv(f"BOT_{i}_SCHEDULE_TIMES_UTC")
            s_msg = os.getenv(f"BOT_{i}_SCHEDULE_MESSAGE")
            if s_cids and s_times and s_msg:
                BOT_SCHEDULES[path] = {
                    "chat_ids": [c.strip() for c in s_cids.split(',')],
                    "times": [t.strip() for t in s_times.split(',')],
                    "message": s_msg,
                    "last_sent": None
                }
            logger.info(f"Bot #{i} ({token[-4:]}) 加载完成")

    # --- 🔥 计算器 Bot 初始化 ---
    calc_token = os.getenv("CALC_BOT_TOKEN")
    if calc_token:
        try:
            calc_app = Application.builder().token(calc_token).build()
            await calc_app.initialize()
            
            # 设置计算器专用的 Handler
            setup_calculator_bot(calc_app)
            
            # 注册到 Webhook 路由中
            BOT_APPLICATIONS["calc_bot_webhook"] = calc_app
            logger.info(f"🧮 计算器 Bot ({calc_token[-4:]}) 加载完成")
        except Exception as e:
            logger.error(f"❌ 计算器 Bot 启动失败: {e}")
    else:
        logger.info("⚠️ 未检测到 CALC_BOT_TOKEN，计算器 Bot 跳过启动。")

    try:
        PLAYWRIGHT_INSTANCE = await async_playwright().start()
        launch_args = [
            "--no-sandbox", 
            "--disable-setuid-sandbox", 
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-software-rasterizer"
        ]
        BROWSER_INSTANCE = await PLAYWRIGHT_INSTANCE.chromium.launch(
            headless=True, 
            args=launch_args
        )
        app.state.browser = BROWSER_INSTANCE
        logger.info("✅ Playwright 启动成功 (优化模式)")
    except Exception as e:
        logger.error(f"❌ Playwright 启动失败: {e}")

    asyncio.create_task(background_scheduler())

@app.on_event("shutdown")
async def shutdown_event():
    if GLOBAL_HTTP_CLIENT: 
        await GLOBAL_HTTP_CLIENT.aclose()
    if BROWSER_INSTANCE: 
        await BROWSER_INSTANCE.close()
    if PLAYWRIGHT_INSTANCE: 
        await PLAYWRIGHT_INSTANCE.stop()

@app.post("/{webhook_path}")
async def handle_webhook(webhook_path: str, request: Request):
    if webhook_path not in BOT_APPLICATIONS: return Response(status_code=404)
    try:
        update = Update.de_json(await request.json(), BOT_APPLICATIONS[webhook_path].bot)
        # 用 asyncio.create_task 异步处理，尽快返回 200 给 TG，防止 TG 认为服务器挂了
        asyncio.create_task(BOT_APPLICATIONS[webhook_path].process_update(update))
        return Response(status_code=200)
    except Exception:
        return Response(status_code=500)

@app.get("/")
async def root():
    return {"status": "OK", "bots": len(BOT_APPLICATIONS)}
