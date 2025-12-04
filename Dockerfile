# 使用微软官方 Playwright 镜像（自带浏览器和所有依赖，稳！）
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

# 设置工作目录
WORKDIR /app

# 复制依赖清单并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再次确认安装浏览器（虽然镜像里有，但这步能防止某些意外）
RUN playwright install chromium

# 复制你的代码
COPY . .

# 暴露端口
EXPOSE 10000

# 启动命令 (直接复用你 Procfile 里的逻辑，但写在这里更稳)
CMD gunicorn main:app --workers 1 --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT
