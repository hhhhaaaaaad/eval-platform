# 评测平台后端镜像（API / Celery worker / beat / Flower 共用同一镜像，仅启动命令不同）
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 依赖单独一层：只要依赖清单不变，改业务代码时不会重装依赖。
# 从锁定文件安装已解析的版本快照，保证镜像与本地 504 测试、CI 跑的是同一组版本。
# 锁文件是完整 venv 快照，故也包含 pytest/mypy 等 dev 工具。
COPY requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt

# 源码层（.venv / __pycache__ / tests 等由 .dockerignore 排除）
COPY . .

EXPOSE 8093

# 默认以 API 启动；worker / beat / flower 在 compose 中覆盖 command
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8093"]
