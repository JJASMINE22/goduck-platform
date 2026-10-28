# syntax=docker/dockerfile:1
#
# goduck mcp-servers 镜像（FastMCP http 传输，默认 0.0.0.0:8088，端点 /mcp/）。
# 知识库检索网关：ES 直连 + TEI 向量化（与 backend 同源配置），无 PG/MinIO 依赖。
# 不 spawn stdio 子进程 ⇒ 无需 uvx/npx/tini（区别于 backend 镜像）。
# 配置经容器环境变量注入：config.py 的 load_dotenv 不覆盖已存在的环境变量，
# 故 compose environment 优先于镜像内 .env（后者已被 .dockerignore 排除）。
#
# 构建上下文 = mcp-servers/ 目录。

FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOST=0.0.0.0 \
    PORT=8088

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1001 --shell /bin/bash app

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY config.py main.py ./
COPY core/ ./core/
COPY routers/ ./routers/
COPY services/ ./services/

USER app
EXPOSE 8088

CMD ["python", "main.py"]

# FastMCP streamable-http 仅服务 /mcp 端点（无 /health 路由，探它会 404 假死）：
# 此处仅探存活——任意 HTTP 响应（含 4xx）即存活，连接失败才非 0；
# 就绪由 backend 注册后的工具调用自然验证。
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
    CMD curl -s -o /dev/null http://127.0.0.1:8088/mcp/ || exit 1
