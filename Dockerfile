# 1. 底座
FROM mcr.microsoft.com/playwright/python:v1.56.0-jammy

# 2. 设置目录
WORKDIR /app

# 3. 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 🔥 4. 核心修复在这里！！！
# 我们显式地安装 chromium，这会自动包含 headless-shell
# 并且加上 --with-deps 确保系统库齐全
# 强力安装模式：不指定浏览器名，让它自动补全当前版本所需的一切
RUN playwright install --with-deps

# 5. 复制其余代码
COPY . .

# 6. 端口
EXPOSE 10000

# 7. 启动
CMD gunicorn main:app --workers 1 --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT
