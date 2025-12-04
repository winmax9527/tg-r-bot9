# 1. 依然使用官方镜像，底子好
FROM mcr.microsoft.com/playwright/python:v1.56.0-jammy

# 2. 设置工作目录
WORKDIR /app

# 3. 先复制依赖清单并安装 Python 库
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 🔥 4. 关键修改在这里！
# 不要只写 install chromium，直接不带参数运行 install
# 这样它会自动读取 playwright==1.56.0 需要的所有东西（包括 headless-shell）
# 并强制安装到代码能找到的地方
RUN playwright install --with-deps

# 5. 复制你的代码
COPY . .

# 6. 暴露端口
EXPOSE 10000

# 7. 启动命令
CMD gunicorn main:app --workers 1 --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT
