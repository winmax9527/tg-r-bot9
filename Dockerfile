# 1. 底座：使用 v1.56.0 官方镜像
FROM mcr.microsoft.com/playwright/python:v1.56.0-jammy

# 2. 设置目录
WORKDIR /app

# 🔥 3. 核心修复：强制指定浏览器路径！
# 这行代码告诉 Playwright："别管项目里有没有旧文件夹，去系统目录找浏览器！"
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# 4. 安装 Python 依赖
COPY requirements.txt .
# 强制升级 playwright 确保版本对齐
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir --upgrade playwright==1.56.0

# 5. 安装浏览器
# 这一步会把浏览器装到 /ms-playwright 目录下
RUN playwright install --with-deps

# 6. 复制你的代码
COPY . .

# 7. 端口
EXPOSE 10000

# 8. 启动
CMD gunicorn main:app --workers 1 --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT
