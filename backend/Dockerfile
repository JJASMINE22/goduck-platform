# syntax=docker/dockerfile:1
#
# goduck-platform backend 镜像（支持 MCP stdio 调用）。
#
# 关键设计：stdio 传输下，是 *backend 进程自己* spawn MCP server 子进程
# （langchain_mcp_adapters -> mcp SDK stdio_client -> create_subprocess_exec），
# 因此 uvx/uv（Python 类 MCP）与 node/npx（Node 类 MCP）必须装在 *本镜像* 内。
#
# 进一步：uvx/npx 默认在运行时联网拉包（冷启动慢 / 离线即失败 / 不可复现）。
# 为规避此问题，构建期用 PREWARM_* 预热常用 MCP 的包缓存进镜像，
# 运行时命中缓存即可秒起、离线可用。
#
# 构建上下文 = backend/ 目录：
#   docker build -t goduck-backend ./backend

FROM python:3.11-slim-bookworm

# ---- 全局构建/运行参数（便于统一升级）-----------------------------------------
# 想锁 uv 版本可加 ARG UV_VERSION，改用 pinned 安装。
ARG NODE_MAJOR=20
# 预热：空格分隔的 MCP 包名。构建期 uvx/npx 拉取到缓存。
# 命中即加速、离线可用；填错或改名不会中断构建（|| true）。
# 需要新增默认预热的 MCP，扩展这两个列表即可。
ARG UVX_MCP_PACKAGES="mcp-server-fetch mcp-server-time"
ARG NPM_MCP_PACKAGES="@modelcontextprotocol/server-filesystem @modelcontextprotocol/server-memory"

# ---- 环境变量 ----------------------------------------------------------------
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # uv：复制而非软链，预编译字节码
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    # uv/uvx 缓存统一到可写目录（见下方非 root 用户授权）
    UV_CACHE_DIR=/opt/uv-cache \
    # npm/npx 缓存同理
    npm_config_cache=/opt/npm-cache \
    HOME=/home/app \
    # 让 stdio 子进程默认不代理 localhost（与 app/mcps/mcp_client.py 行为一致）
    NO_PROXY=127.0.0.1,localhost \
    APP_HOST=0.0.0.0 \
    APP_PORT=8000

# ---- OS 依赖 -----------------------------------------------------------------
# tini: PID 1，正确转发信号 + 回收 MCP 子进程；git: git 类 MCP 需要；
# build-essential: 兜底少数无 wheel 的包（如全 wheel 可移除以瘦身）。
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates git tini build-essential \
    && rm -rf /var/lib/apt/lists/*

# ---- uv + uvx（Python 类 MCP stdio 运行时）-----------------------------------
# 官方独立安装器，装到全局 /usr/local/bin，所有用户可用，同时提供 uv 与 uvx。
# 注意：此处不锁版本（取最新）。生产可用 UV_VERSION 锁定以保证可复现。
RUN curl -LsSf https://astral.sh/uv/install.sh \
    | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh \
    && uv --version && uvx --version

# ---- Node.js + npm/npx（Node 类 MCP stdio 运行时）----------------------------
RUN curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && node --version && npm --version && npx --version

# ---- 非 root 运行用户 + 可写缓存目录 ------------------------------------------
RUN useradd --create-home --uid 1001 --shell /bin/bash app \
    && mkdir -p "${UV_CACHE_DIR}" "${npm_config_cache}" \
    && chown -R app:app "${UV_CACHE_DIR}" "${npm_config_cache}" /home/app

WORKDIR /app

# ---- 1) 安装运行时依赖（requirements.txt 变化才重装，缓存命中重依赖）---------
# 常规 pip 安装（与本地开发一致）。仅运行依赖，不含 dev 依赖。
# app 包本身不 pip install —— 运行期从 WORKDIR=/app 直接 import app 即可。
COPY requirements.txt ./
RUN pip install -r requirements.txt

# ---- 2) 拷贝应用代码 ----------------------------------------------------------
COPY app/ ./app/
COPY agent_confs/ ./agent_confs/
RUN chown -R app:app /app

# ---- 3) 预热 MCP 包缓存（uvx + npx）-------------------------------------------
# 关键：只"下载入缓存"，绝不执行 MCP server 本体——stdio server 会阻塞在
# stdin 等待握手，直接运行会导致构建挂死。故用：
#   uv tool install : 下载+构建 wheel 入缓存，并注册为具名工具（运行期 uvx 直接复用）
#   npm pack        : 仅拉取 tarball 入 npm 缓存，不安装、不执行任何 bin / 脚本
# 二者都以 app 用户执行，缓存属主与运行期一致；包名缺失/改名仅跳过，不中断构建。
USER app
RUN set -e; \
    for p in ${UVX_MCP_PACKAGES}; do \
        echo "prewarm uv tool: $p"; \
        uv tool install "$p" >/dev/null 2>&1 || echo "  (skip) $p"; \
    done; \
    mkdir -p /tmp/npm-prewarm; \
    for p in ${NPM_MCP_PACKAGES}; do \
        echo "prewarm npm pack: $p"; \
        npm pack "$p" --pack-destination /tmp/npm-prewarm >/dev/null 2>&1 || echo "  (skip) $p"; \
    done; \
    rm -rf /tmp/npm-prewarm; \
    echo "MCP prewarm done"

EXPOSE 8000

# tini 作 PID 1：转发 SIGTERM + 回收 stdio MCP 子进程，避免僵尸进程。
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# 健康检查（/health 见 app/main.py）。
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -fsS "http://127.0.0.1:${APP_PORT}/health" || exit 1
