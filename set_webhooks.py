import requests
import os
import sys
import logging

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- 配置 ---
# 自动获取当前部署的公共 URL (Render 或其他 PaaS 环境通常提供)
BASE_URL = os.getenv("RENDER_EXTERNAL_HOSTNAME")
# 如果没有找到公共 URL，脚本将无法运行
if not BASE_URL:
    logging.error("无法获取 BASE_URL。请确保在 PaaS 环境中运行此脚本，或者手动设置 PUBLIC_URL 环境变量。")
    # 如果本地测试，可以手动设置 PUBLIC_URL，例如：BASE_URL = "https://your-ngrok-url.ngrok.io"
    sys.exit(1)

# 强制使用 HTTPS
BASE_URL = f"https://{BASE_URL}"
logging.info(f"检测到的公共服务 URL (BASE_URL): {BASE_URL}")

# 定义需要处理的 Bot ID 列表
# 确保这里的 ID (1, 4, 6, 9) 与您的应用程序文件 botX_app.py 匹配
BOT_IDS = [1, 4, 6, 9]

def set_webhook_and_check(bot_id: int, base_url: str):
    """设置并检查单个 Bot 的 Webhook 状态"""
    
    # 1. 获取 Bot Token
    token_env_name = f"TELEGRAM_BOT_TOKEN_{bot_id}"
    bot_token = os.getenv(token_env_name)
    
    if not bot_token:
        logging.warning(f"跳过 Bot {bot_id}：环境变量 {token_env_name} 未设置。")
        return False
    
    # 2. 定义 Webhook URL
    # Webhook URL 格式: https://<您的域名>/bot/<Bot ID>/webhook
    webhook_url = f"{base_url}/bot/{bot_id}/webhook"
    
    # 3. 设置 Webhook
    set_url = f"https://api.telegram.org/bot{bot_token}/setWebhook"
    set_payload = {
        'url': webhook_url,
        # 允许最大 100 个未决更新，提高容错能力
        'max_connections': 100, 
    }
    
    try:
        logging.info(f"正在为 Bot {bot_id} 设置 Webhook 到: {webhook_url}")
        set_response = requests.post(set_url, json=set_payload, timeout=10)
        set_response.raise_for_status() # 检查 HTTP 错误
        
        set_result = set_response.json()
        if set_result.get("ok"):
            logging.info(f"✅ Bot {bot_id} Webhook 设置成功：{set_result.get('description', 'OK')}")
        else:
            logging.error(f"❌ Bot {bot_id} Webhook 设置失败：{set_result.get('description', '未知错误')}")
            return False

        # 4. 检查 Webhook 状态
        get_url = f"https://api.telegram.org/bot{bot_token}/getWebhookInfo"
        get_response = requests.get(get_url, timeout=10)
        get_response.raise_for_status()
        
        info = get_response.json().get("result", {})
        current_url = info.get("url", "N/A")
        
        if current_url == webhook_url:
            logging.info(f"✅ Bot {bot_id} Webhook 状态确认：URL 正确。")
            return True
        else:
            logging.warning(f"⚠️ Bot {bot_id} Webhook 状态异常：API 报告 URL 为 {current_url}，期望值为 {webhook_url}")
            return False
            
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Bot {bot_id} Webhook API 调用失败：{e}")
        return False
    except Exception as e:
        logging.error(f"❌ Bot {bot_id} 发生未知错误：{e}")
        return False


def main():
    """主函数，迭代所有 Bot ID 并设置 Webhook"""
    all_success = True
    logging.info("--- 开始设置 Telegram Bot Webhooks ---")
    
    for bot_id in BOT_IDS:
        success = set_webhook_and_check(bot_id, BASE_URL)
        if not success:
            all_success = False

    logging.info("--- Webhook 设置完成 ---")
    if all_success:
        logging.info("🎉 所有已配置的 Bots Webhook 都设置成功！")
    else:
        logging.warning("⚠️ 部分或全部 Bots 的 Webhook 设置失败，请检查日志和环境变量。")

if __name__ == "__main__":
    main()
