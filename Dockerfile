# 1. 底座
FROM mcr.microsoft.com/playwright/python:v1.56.0-jammy

# 2. 设置目录
WORKDIR /app

# 3. 环境变量：强制去系统目录找浏览器
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# 4. 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir --upgrade playwright==1.56.0

# 5. 安装浏览器 (装到 /ms-playwright)
RUN playwright install --with-deps

# 6. 复制所有代码 (垃圾文件也被拷进来了)
COPY . .

# 🔥🔥🔥 7. 核弹级修正：统统删掉！🔥🔥🔥
# 这一步会删掉所有本地残留的浏览器数据、Python虚拟环境、以及环境变量文件
# 逼迫 Bot 只能用 Docker 里刚装好的全新环境
RUN rm -rf /app/pw-browsers /app/venv /app/.venv /app/.env /app/__pycache__

# 8. 端口
EXPOSE 10000

# 9. 启动
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000"]
