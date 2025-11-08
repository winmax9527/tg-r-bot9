import os
import requests
from dotenv import load_dotenv

# 为了在本地运行时获取环境变量，如果已经在 Render 上配置，这一行可以忽略
load_dotenv()

# --- 配置信息 ---
# 替换为您的 Render 服务的主 URL (这是您部署日志中的 URL)
BASE_URL = "https://tg-r-bot9.onrender.com"

# 您的 Bot ID 列表
BOT_IDS = [1, 4, 6, 9]

# 从环境中读取令牌。确保您的 .env 文件或 Render 环境变量中设置了这些键。
BOT_TOKENS = {
    1: os.getenv("BOT_TOKEN_1"),
    4: os.getenv("BOT_TOKEN_4"),
    6: os.getenv("BOT_TOKEN_6"),
    9: os.getenv("BOT_TOKEN_9"),
}

def set_webhook_for_bot(bot_id: int, token: str, base_url: str):
    """设置单个 Telegram Bot 的 Webhook"""
    if not token:
        print(f"❌ Bot {bot_id}: 令牌未找到。请检查 BOT_TOKEN_{bot_id} 环境变量。")
        return

    # Webhook 地址格式: https://YOUR_RENDER_URL/bot{ID}/webhook
    webhook_url = f"{base_url}/bot{bot_id}/webhook"
    api_url = f"https://api.telegram.org/bot{token}/setWebhook"
    
    # 额外设置 drop_pending_updates=True 以清空在部署期间积压的消息
    params = {
        "url": webhook_url,
        "drop_pending_updates": "true" 
    }
    
    print(f"➡️ Bot {bot_id}: 正在设置 Webhook 到 {webhook_url}")
    
    try:
        # 使用 requests 库发送 POST 请求
        response = requests.post(api_url, params=params)
        response.raise_for_status() # 检查 HTTP 错误
        
        result = response.json()
        
        if result.get("ok"):
            print(f"✅ Bot {bot_id} Webhook 设置成功: {result.get('description')}")
        else:
            print(f"❌ Bot {bot_id} Webhook 设置失败: {result.get('description')}")
            
    except requests.exceptions.RequestException as e:
        print(f"🔴 Bot {bot_id} 设置 Webhook 时发生请求错误: {e}")
    except Exception as e:
        print(f"🔴 Bot {bot_id} 发生未知错误: {e}")


def main():
    """主函数：遍历所有 Bot 并设置 Webhook"""
    print("--- 开始设置 Telegram Webhooks ---")
    
    success_count = 0
    for bot_id in BOT_IDS:
        token = BOT_TOKENS.get(bot_id)
        if token:
            set_webhook_for_bot(bot_id, token, BASE_URL)
            success_count += 1
        
    print("--- Webhook 设置完成 ---")
    if success_count == len(BOT_IDS):
        print("🎉 所有 Webhook 都已尝试设置。如果全部成功，您的 Bot 现已激活！")
    else:
        print("⚠️ 某些 Bot 由于缺少令牌而跳过。请检查配置。")

if __name__ == "__main__":
    main()
