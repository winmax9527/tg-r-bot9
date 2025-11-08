import os
import requests
from dotenv import load_dotenv

# 为了在本地运行时获取环境变量，如果已经在 Render 上配置，这一行可以忽略
load_dotenv()

# --- 配置信息 ---
# 替换为您的 Render 服务的主 URL 
BASE_URL = "https://tg-r-bot9.onrender.com"

# 您的 Bot ID 列表
BOT_IDS = [1, 4, 6, 9]

# 从环境中读取令牌。
BOT_TOKENS = {
    1: os.getenv("BOT_TOKEN_1"),
    4: os.getenv("BOT_TOKEN_4"),
    6: os.getenv("BOT_TOKEN_6"),
    9: os.getenv("BOT_TOKEN_9"),
}

# --- Telegram API URL 模板 ---
def get_api_url(token, method):
    """构建 Telegram API 请求 URL"""
    return f"https://api.telegram.org/bot{token}/{method}"

# --- Webhook 操作函数 ---

def get_webhook_info(bot_id: int, token: str):
    """获取当前 Webhook 地址（您最初想要的 API）"""
    api_url = get_api_url(token, "getWebhookInfo")
    
    try:
        response = requests.get(api_url)
        response.raise_for_status() 
        result = response.json()
        
        if result.get("ok"):
            current_url = result.get("result", {}).get("url", "无")
            print(f"👀 Bot {bot_id}: 当前 Webhook URL: {current_url}")
            return current_url
        else:
            print(f"❌ Bot {bot_id}: 获取信息失败: {result.get('description')}")
            return None
    except requests.exceptions.RequestException as e:
        print(f"🔴 Bot {bot_id} 获取信息时发生错误: {e}")
        return None

def delete_webhook(bot_id: int, token: str):
    """删除当前 Webhook 设置"""
    api_url = get_api_url(token, "deleteWebhook")
    
    print(f"🗑️ Bot {bot_id}: 尝试删除旧 Webhook...")
    try:
        response = requests.post(api_url)
        response.raise_for_status() 
        result = response.json()
        
        if result.get("ok"):
            print(f"✅ Bot {bot_id}: 删除成功: {result.get('description')}")
            return True
        else:
            print(f"❌ Bot {bot_id}: 删除失败: {result.get('description')}")
            return False
    except requests.exceptions.RequestException as e:
        print(f"🔴 Bot {bot_id} 删除 Webhook 时发生错误: {e}")
        return False

def set_new_webhook(bot_id: int, token: str, base_url: str):
    """设置新的 Webhook 地址到 Render 服务"""
    # Webhook 地址格式: https://YOUR_RENDER_URL/bot{ID}/webhook
    webhook_url = f"{base_url}/bot{bot_id}/webhook"
    api_url = get_api_url(token, "setWebhook")
    
    # 额外设置 drop_pending_updates=True 以清空在部署期间积压的消息
    params = {
        "url": webhook_url,
        "drop_pending_updates": "true" 
    }
    
    print(f"➡️ Bot {bot_id}: 正在设置 Webhook 到 {webhook_url}")
    
    try:
        response = requests.post(api_url, params=params)
        response.raise_for_status() 
        result = response.json()
        
        if result.get("ok"):
            print(f"✅ Bot {bot_id}: 设置成功: {result.get('description')}")
            return True
        else:
            print(f"❌ Bot {bot_id}: 设置失败: {result.get('description')}")
            return False
    except requests.exceptions.RequestException as e:
        print(f"🔴 Bot {bot_id} 设置 Webhook 时发生请求错误: {e}")
        return False


def main():
    """主函数：遍历所有 Bot，检查、删除、然后设置 Webhook"""
    print("--- 开始 Webhook 三步走 (检查 -> 删除 -> 设置) ---")
    
    for bot_id in BOT_IDS:
        token = BOT_TOKENS.get(bot_id)
        
        if not token:
            print(f"❌ Bot {bot_id}: 令牌未找到。跳过。")
            continue
            
        print(f"\n--- 处理 Bot {bot_id} ---")
        
        # 1. 检查当前状态
        get_webhook_info(bot_id, token)
        
        # 2. 删除 Webhook
        delete_webhook(bot_id, token)
        
        # 3. 设置新的 Webhook
        set_new_webhook(bot_id, token, BASE_URL)
        
    print("\n--- Webhook 流程完成 ---")

if __name__ == "__main__":
    main()
