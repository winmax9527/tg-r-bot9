# 1. 底座
FROM mcr.microsoft.com/playwright/python:v1.56.0-jammy

# 2. 设置目录
WORKDIR /app

# 3. 环境变量：告诉 Playwright 去系统目录找浏览器
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# 4. 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir --upgrade playwright==1.56.0

# 5. 安装浏览器 (装到 /ms-playwright)
RUN playwright install --with-deps

# 6. 复制所有代码 (这里会把那个讨厌的 pw-browsers 也拷进去)
COPY . .

# 🔥🔥🔥 7. 绝杀修正：强行删除复制进来的本地缓存！🔥🔥🔥
# 这行命令会把项目里的旧浏览器文件夹直接删掉，逼迫 Bot 去用系统自带的
RUN rm -rf /app/pw-browsers

# 8. 端口
EXPOSE 10000

# 9. 启动
CMD gunicorn main:app --workers 1 --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT
