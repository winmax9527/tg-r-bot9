import os
import requests
import logging
from typing import Dict, Optional

# --- 配置 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/{method}"

# 从环境变量获取公共服务 URL 和 Bot Tokens
BASE_URL: Optional[str] = os.environ.get("BASE_URL")

BOT_TOKENS: Dict[str, Optional[str]] = {
    "1": os.environ.get("BOT_TOKEN_1"),
    "4": os.environ.get("BOT_TOKEN_4"),
    "6": os.environ.get("BOT_TOKEN_6"),
    "9": os.environ.get("BOT_TOKEN_9"),
}

# 过滤掉未设置 token 的 Bot
ACTIVE_BOTS: Dict[str, str] = {bot_id: token for bot_id, token in BOT_TOKENS.items() if token}

def api_call(token: str, method: str, data: Optional[Dict] = None) -> Optional[Dict]:
    """向 Telegram API 发送请求"""
    url = TELEGRAM_API_URL.format(token=token, method=method)
    try:
        response = requests.post(url, json=data)
        response.raise_for_status() # 对 4xx/5xx 响应抛出异常
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ API 调用失败 ({method}): {e}")
        return None

def delete_webhook(bot_id: str, token: str) -> bool:
    """删除当前 Bot 的 Webhook"""
    logger.info(f"正在为 Bot {bot_id} 删除 Webhook...")
    result = api_call(token, "deleteWebhook")
    if result and result.get("ok"):
        logger.info(f"🗑️ Bot {bot_id} Webhook 已清除。")
        return True
    else:
        logger.warning(f"⚠️ Bot {bot_id} Webhook 清除失败或无需清除: {result}")
        return False

def set_webhook(bot_id: str, token: str, webhook_url: str) -> bool:
    """设置 Bot 的 Webhook"""
    
    # 1. 尝试删除旧 Webhook
    delete_webhook(bot_id, token)

    # 2. 设置新的 Webhook
    logger.info(f"正在为 Bot {bot_id} 设置 Webhook 到: {webhook_url}")
    payload = {"url": webhook_url}
    result = api_call(token, "setWebhook", payload)

    if result and result.get("ok"):
        description = result.get("description", "设置成功")
        logger.info(f"✅ Bot {bot_id} Webhook 设置成功：{description}")
        return True
    else:
        logger.error(f"❌ Bot {bot_id} Webhook 设置失败：{result}")
        return False

def get_webhook_info(bot_id: str, token: str, expected_url: str) -> bool:
    """获取并确认 Webhook 状态"""
    result = api_call(token, "getWebhookInfo")
    
    if result and result.get("ok"):
        info = result.get("result", {})
        current_url = info.get("url")
        
        if current_url == expected_url:
            logger.info(f"✅ Bot {bot_id} Webhook 状态确认：URL 正确。")
            return True
        else:
            logger.warning(f"⚠️ Bot {bot_id} Webhook 状态不匹配：期望 {expected_url}，实际 {current_url}。")
            return False
    else:
        logger.error(f"❌ Bot {bot_id} 无法获取 Webhook 状态。")
        return False

# --- 主执行逻辑 ---
def main():
    """主函数：遍历所有 Bot 并设置 Webhook"""
    if not BASE_URL:
        logger.error("❌ 环境变量 BASE_URL 未设置。请确保 BASE_URL 已配置。")
        return

    if not ACTIVE_BOTS:
        logger.error("❌ 环境变量 BOT_TOKEN_* 未设置。请至少设置一个有效的 Bot Token。")
        return

    logger.info(f"检测到的公共服务 URL (BASE_URL): {BASE_URL}")
    logger.info("--- 开始设置 Telegram Bot Webhooks ---")

    all_success = True
    
    for bot_id, token in ACTIVE_BOTS.items():
        # 完整的 Webhook 路径，必须与 main.py 中的路由匹配
        webhook_path = f"/bot/{token}/webhook"
        full_webhook_url = f"{BASE_URL}{webhook_path}"

        # 设置 Webhook
        if set_webhook(bot_id, token, full_webhook_url):
            # 确认 Webhook 状态
            if not get_webhook_info(bot_id, token, full_webhook_url):
                all_success = False
        else:
            all_success = False

    logger.info("--- Webhook 设置完成 ---")
    if all_success:
        logger.info("🎉 所有已配置的 Bots Webhook 都设置成功！")
    else:
        logger.error("🚨 某些 Bots 的 Webhook 设置或状态确认失败，请检查日志。")

if __name__ == "__main__":
    main()
