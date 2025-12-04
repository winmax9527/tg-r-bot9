# 1. 底座：使用 v1.56.0 官方镜像
FROM mcr.microsoft.com/playwright/python:v1.56.0-jammy

# 2. 设置目录
WORKDIR /app

# 🔥 3. 【核心绝杀】强制指定浏览器路径到系统目录！
# 这行代码是“圣旨”：告诉 Playwright 必须去 /ms-playwright 找浏览器
# 彻底忽略掉你项目里可能存在的 /app/pw-browsers 垃圾文件
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# 4. 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# 双重保险：强制确认安装的是 1.56.0
RUN pip install --no-cache-dir --upgrade playwright==1.56.0

# 5. 安装浏览器
# 这一步会把浏览器装到上面指定的 /ms-playwright 安全目录下
RUN playwright install --with-deps

# 6. 复制你的代码 (这时候就算拷进来了垃圾文件夹，Bot 也不会去看它了)
COPY . .

# 7. 端口
EXPOSE 10000

# 8. 启动
CMD gunicorn main:app --workers 1 --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT
