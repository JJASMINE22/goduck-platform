# syntax=docker/dockerfile:1
#
# goduck-platform backend 镜像（支持 MCP stdio 调用 + docling GPU 推理）。
#
# 关键设计：stdio 传输下，是 *backend 进程自己* spawn MCP server 子进程
# （langchain_mcp_adapters -> mcp SDK stdio_client -> create_subprocess_exec），
# 因此 uvx/uv（Python 类 MCP）与 node/npx（Node 类 MCP）必须装在 *本镜像* 内。
#
# 进一步：uvx/npx 默认在运行时联网拉包（冷启动慢 / 离线即失败 / 不可复现）。
# 为规避此问题，构建期用 PREWARM_* 预热常用 MCP 的包缓存进镜像，
# 运行时命中缓存即可秒起、离线可用。
#
# GPU（docling layout/TableFormer）：见下方 "---- 0)" 层注释。要点：CUDA 版
# torch wheel 自带全套 CUDA 运行时，镜像保持 python:slim 基底；宿主机只需
# NVIDIA 驱动（570 系）+ Container Toolkit，compose 层（docker-compose.yml）
# 已内置 nvidia 设备声明。
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
ARG UVX_MCP_PACKAGES="mcp-server-fetch mcp-server-time arxiv-mcp-server duckduckgo-mcp-server"
ARG NPM_MCP_PACKAGES="@modelcontextprotocol/server-filesystem @modelcontextprotocol/server-memory"
# GPU 推理：CUDA 版 torch 配套矩阵（升级只改这三行，或 --build-arg 覆盖）。
#   torch 2.6.0+cu124 ↔ 宿主机驱动 ≥550（推荐 570 系；RTX 3080 / sm_86 在
#   官方内核列表内）。换 cu126/cu128 时同步换 TORCH_INDEX_URL 并重验驱动门槛。
ARG TORCH_VERSION=2.6.0+cu124
ARG TORCHVISION_VERSION=0.21.0+cu124
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cu124

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

# ---- 0) GPU 推理（docling layout/TableFormer）：CUDA 版 torch -------------------
# docling 的 layout + TableFormer 走 torch（docling-ibm-models 传递依赖），PyPI
# 默认 wheel 为 CPU 版；此处改装 cu124 版后 KB_DOCLING_DEVICE=auto/cuda 即走 GPU。
# 三层免装设计：
#   ① cu124 wheel 自带全套 CUDA 运行时（nvidia-* pip 包 + cuDNN 9.1）——镜像
#      无需 nvidia/cuda 基底，宿主机无需装 CUDA Toolkit；
#   ② 宿主机只需驱动（570 系）+ NVIDIA Container Toolkit（注入 libcuda.so）；
#   ③ compose 层（docker-compose.yml 的 deploy 设备声明）让容器内 GPU 可见。
# 分层顺序：torch 先于 requirements.txt —— docling 对 torch/torchvision 的
# 传递约束（>=2.x）被已装的 cu124 版满足，后续 pip 不会拉 CPU 版覆盖；
# requirements 变更也不触发 ~2.5GB torch 层重下。代价：镜像体积 +4~6GB。
# 末行 import 自检：无 GPU 的构建机也能过（不触发 CUDA 调用），仅验证
# wheel 形态（torch.version.cuda 应为 12.4）。
RUN pip install --index-url "${TORCH_INDEX_URL}" \
        "torch==${TORCH_VERSION}" "torchvision==${TORCHVISION_VERSION}" \
    && python -c "import torch; print('torch', torch.__version__, '/ cuda', torch.version.cuda)"

# ---- 1) 安装运行时依赖（requirements.txt 变化才重装，缓存命中重依赖）---------
# 常规 pip 安装（与本地开发一致）。仅运行依赖，不含 dev 依赖。
# app 包本身不 pip install —— 运行期从 WORKDIR=/app 直接 import app 即可。
# torch/torchvision 已由上方 0) 层以 cu124 版预装，docling 的传递依赖在此
# 被满足，pip 不会拉 CPU 版覆盖（本地开发走本文件则装 PyPI CPU 版，行为一致）。
COPY requirements.txt ./
RUN pip install -r requirements.txt

# ---- 1b) opencv 运行时系统库（独立层，紧随 pip 层之后）--------------------------
# docling RapidOCR 链路 → rapidocr/rapidocr-onnxruntime 传递依赖全量 opencv-python，
# 其 cv2 .so 需要 libxcb/libGL/glib；slim 基底缺失时 import cv2 报
# "libxcb.so.1: cannot open shared object file"，经 docling 包装成误导性的
# "RapidOCR is not installed"。单列一层（而非并入上方 2) 的 apt）是为保住
# torch/requirements 两层重缓存——本层变更只重建其后廉价层。
# APT_MIRROR：可选 deb.debian.org 替换源（CN 网络传 mirrors.aliyun.com 等；
# 缺省空 = 官方源，不绑架上游仓库）。经 build args / override 注入。
ARG APT_MIRROR=
RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get update && apt-get install -y --no-install-recommends \
        libxcb1 libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# ---- 2) 拷贝应用代码 ----------------------------------------------------------
COPY app/ ./app/
COPY agent_confs/ ./agent_confs/
RUN chown -R app:app /app

# ---- 2b) 数据卷挂载点 /data（workspace/memories/skills 持久化）----------------
# LOCAL_ROOT=/data（见 docker-compose.yml），三目录均派生自 local_dir。
# 技能种子在此烘焙：若以命名卷挂 /data，Docker 首次挂载会把镜像内 /data 的
# 内容（含属主）拷入卷，卷即真相源；生产 compose 现用宿主机 bind mount
# （volumes/backend），不会自动拷种子 —— 首次部署需手动
#   cp -r backend/data/skills volumes/backend/skills（见 compose 注释 / README）。
# 源在 backend/data/skills/（与本地 dev 同一布局，统一镜像与本地目录）。
COPY data/skills/ /data/skills/
RUN mkdir -p /data/workspace /data/memories && chown -R app:app /data

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

# ---- 3b) mcp-jobs（stdio MCP server，Playwright 运行时）-----------------------
# 该包运行期需要无头浏览器，三样前置固化进镜像（容器可写层的运行期安装
# 扛不住重建）：
#   ① npm 全局安装本体；PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 阻断 playwright
#      postinstall 联网拉浏览器（改由 ③ 显式安装，下载源可控）；--cache 指向
#      构建期临时目录，避免 root 污染运行期缓存目录属主。
#   ② Chromium 系统依赖（apt：nss/gbm/atk/xvfb/CJK 字体等）。
#   ③ Chromium 二进制装入 app 用户缓存目录（与运行期 HOME 一致）。
# PLAYWRIGHT_DOWNLOAD_HOST 默认国内镜像加速，海外构建 --build-arg 覆盖回官方源。
# 置于镜像末层：本层变更不触发前面 torch / requirements 大层重下。
ARG PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright
USER root
RUN PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm install -g --cache /tmp/npm-cache-build mcp-jobs \
    && /usr/lib/node_modules/mcp-jobs/node_modules/.bin/playwright install-deps chromium \
    && rm -rf /var/lib/apt/lists/* /tmp/npm-cache-build
USER app
RUN PLAYWRIGHT_DOWNLOAD_HOST=${PLAYWRIGHT_DOWNLOAD_HOST} \
    /usr/lib/node_modules/mcp-jobs/node_modules/.bin/playwright install chromium

EXPOSE 8000

# tini 作 PID 1：转发 SIGTERM + 回收 stdio MCP 子进程，避免僵尸进程。
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# 健康检查（/health 见 app/main.py）。
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -fsS "http://127.0.0.1:${APP_PORT}/health" || exit 1
