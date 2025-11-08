import re
import random
import string
import asyncio
import logging
import requests

logger = logging.getLogger(__name__)

# 模拟 API 调用的固定域名 B (用于生成短链接的基地址)
BASE_DOMAIN_B = "https://shortlink.example.com"

async def fetch_domain_a_from_api():
    """
    模拟从外部 API 获取域名 A 的函数。
    在实际生产环境中，这里会使用 aiohttp 或 httpx 进行异步 API 调用。
    """
    logger.info("正在模拟获取域名 A...")
    # 模拟网络延迟
    await asyncio.sleep(0.5) 
    # 假设 API 返回的域名 A 是最终跳转目标
    return "https://api.source-domain-a.net"

def generate_random_code(length=6):
    """生成指定长度的随机字母数字组合代码。"""
    chars = string.ascii_letters + string.digits
    return ''.join(random.choice(chars) for _ in range(length))

async def generate_short_link(custom_code: str | None = None) -> str:
    """
    生成短链接的核心函数。
    
    :param custom_code: 用户提供的 3-8 位自定义代码。
    :return: 最终的短链接字符串或错误信息。
    """
    
    # 1. 模拟获取域名 A (虽然最终链接不直接包含 A，但这是业务要求的一部分)
    # domain_a = await fetch_domain_a_from_api()
    # 在实际应用中，您会在这里将 domain_a 存入数据库，作为 custom_code 的目标地址
    
    # 2. 验证或生成自定义代码
    final_code = ""
    
    if custom_code:
        # 验证自定义代码是否符合 3-8 位字母数字的规则
        if 3 <= len(custom_code) <= 8 and re.fullmatch(r"^[a-zA-Z0-9]+$", custom_code):
            final_code = custom_code
        else:
            return "❌ 错误：自定义代码必须是 3-8 位的字母或数字组合。"
    else:
        # 自动生成一个 6 位的代码
        final_code = generate_random_code(6)

    # 3. 构造最终短链接 (使用域名 B 作为基址)
    final_link = f"{BASE_DOMAIN_B}/{final_code}"
    
    # 4. 模拟将短链接-目标域名 A 的映射关系存入数据库 (此处省略)
    logger.info(f"映射关系已创建: {final_code} -> (Domain A)")
    
    return f"✅ 成功生成短链接: {final_link}\n(基础域名 B: {BASE_DOMAIN_B})"