# 评测平台后端镜像（API / Celery worker / beat / Flower 共用同一镜像，仅启动命令不同）
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 依赖单独一层：只要依赖清单不变，改业务代码时不会重装依赖。
# 注意：此列表必须与 pyproject.toml 的 [project].dependencies 保持一致——
# Dockerfile 无法直接消费 pyproject 的依赖段而不安装本项目包，故此处显式列出。
RUN pip install --no-cache-dir \
    "fastapi>=0.115" \
    "uvicorn[standard]>=0.32" \
    "pydantic>=2.9" \
    "pydantic-settings>=2.6" \
    "sqlalchemy>=2.0.36" \
    "alembic>=1.14" \
    "psycopg[binary]>=3.2" \
    "redis>=5.2" \
    "celery[redis]>=5.4" \
    "httpx>=0.28" \
    "pyjwt>=2.10" \
    "bcrypt>=4.2"

# 源码层（.venv / __pycache__ / tests 等由 .dockerignore 排除）
COPY . .

EXPOSE 8093

# 默认以 API 启动；worker / beat / flower 在 compose 中覆盖 command
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8093"]
