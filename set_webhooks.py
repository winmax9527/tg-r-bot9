import os
import requests
import json
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- 配置 ---
# ⚠️ 请将 BASE_URL 替换为您的实际部署域名
BASE_URL = "https://tg-r-bot9.onrender.com" 

# 假设所有 Bot Token 都已作为环境变量设置
BOT_CONFIGS = {
    "1": os.environ.get("BOT_TOKEN_1"),
    "4": os.environ.get("BOT_TOKEN_4"),
    "6": os.environ.get("BOT_TOKEN_6"),
    "9": os.environ.get("BOT_TOKEN_9"),
}

def set_webhook_for_bot(bot_id, token, base_url):
    """为单个 Bot 设置 Webhook，使用正确的 /webhook 路径。"""
    if not token:
        logger.warning(f"Bot {bot_id} Token 未设置，跳过。")
        return

    # 完整的 Webhook URL，现在包含 /webhook 后缀
    # 这是关键的修改：确保路径末尾有 /webhook
    webhook_url = f"{base_url}/bot/{token}/webhook"
    
    # Telegram API URL
    api_url = f"https://api.telegram.org/bot{token}/setWebhook"
    
    params = {
        'url': webhook_url,
        # 推荐设置允许的更新类型
        'allowed_updates': json.dumps(["message", "edited_message", "callback_query"])
    }

    try:
        logger.info(f"正在为 Bot {bot_id} 设置 Webhook 到: {webhook_url}")
        
        response = requests.post(api_url, data=params, timeout=10)
        response_data = response.json()
        
        if response.status_code == 200 and response_data.get('ok'):
            logger.info(f"✅ Bot {bot_id} Webhook 设置成功。")
        else:
            logger.error(f"❌ Bot {bot_id} Webhook 设置失败，状态码: {response.status_code}, 错误: {response_data.get('description', '无描述')}")
            
    except requests.RequestException as e:
        logger.error(f"❌ Bot {bot_id} Webhook 设置请求异常: {e}")

if __name__ == "__main__":
    logger.info(f"开始批量设置 Webhook，Base URL: {BASE_URL}")
    for bot_id, token in BOT_CONFIGS.items():
        set_webhook_for_bot(bot_id, token, BASE_URL)
    logger.info("所有 Bot 的 Webhook 设置完成。")
