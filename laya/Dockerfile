# syntax=docker/dockerfile:1
#
# goduck-platform laya-serve 镜像（Jev 协议兼容判决端点：POST /v1/systemone）。
#
# 安装矩阵沿用 backend/Dockerfile 的已验证组合：同一 python:3.11-slim 基底 +
# cu124 torch wheel（自带全套 CUDA 运行时，宿主机只需驱动 ≥550 + NVIDIA
# Container Toolkit）+ 阿里云 PyPI 回退源。laya 0.4.6 依赖下限 torch>=2.0 /
# transformers>=4.48（pyproject 核对），2.6.0+cu124 满足。
# 注意：laya 官方 Dockerfile pin torch 2.14.0（cu126/cu130；cu130 需宿主驱动
# ≥580 系）。若冒烟发现推理异常，覆盖下方 TORCH_VERSION / TORCH_INDEX_URL
# 两个 ARG 即切回官方矩阵。
#
# 构建上下文 = laya/ 目录：
#   docker build -t goduck-laya ./laya

FROM python:3.11-slim-bookworm

# ---- 构建参数（与 backend 同款语义；CPU 机器构建时覆盖为 cpu 索引）------------
ARG TORCH_VERSION=2.6.0+cu124
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cu124
# 普通 PyPI 依赖（fastapi/transformers 等）的回退索引：torch 索引不携带它们，
# 官方源国内直连易断流。带 local 版本号（+cu124）的 torch 只存在于 torch 索引，
# 不会被 extra-index 的 CPU 版覆盖。
ARG PIP_EXTRA_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
# serve extra = laya-serve CLI（fastapi + uvicorn）；上界防 0.5 API 漂移。
ARG LAYA_SERVE_SPEC="laya[serve]>=0.4.6,<0.5"

# ---- 环境变量 ----------------------------------------------------------------
# TORCH_DISABLE_NATIVE_JIT：torch 2.14 会把部分 eager CUDA 算子换成首次推理
# 需 C 编译的 Triton kernel（slim 无编译器 → 容器健康但每请求必挂，官方
# issue #365）；2.6.0 下为无害 no-op，切官方矩阵时必需。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_EXTRA_INDEX_URL=${PIP_EXTRA_INDEX_URL} \
    TORCH_DISABLE_NATIVE_JIT=1 \
    OMP_NUM_THREADS=4 \
    HF_HOME=/home/laya/.cache/huggingface

# ---- 非 root 运行用户 ---------------------------------------------------------
# UID 10001 = 官方 laya 镜像约定。bind mount 的模型目录须 chown 10001:10001
# （loader 首次加载可能改写 tokenizer 配置，目录须可写）。
RUN useradd --create-home --uid 10001 laya

WORKDIR /home/laya

# ---- torch 先装（分层缓存：laya 版本变更不触发 torch 大层重下）----------------
# 末行 import 自检：无 GPU 的构建机也能过（不触发 CUDA 调用），仅验证 wheel
# 形态（torch.version.cuda 应为 12.4）。
RUN pip install --index-url "${TORCH_INDEX_URL}" "torch==${TORCH_VERSION}" \
    && python -c "import torch; print('torch', torch.__version__, '/ cuda', torch.version.cuda)"

# ---- laya[serve] ----------------------------------------------------------------
# torch>=2.0 传递约束已被上方 cu124 版满足，pip 不会拉 CPU 版覆盖。
# 自检：模块可导入 + laya-serve 入口存在。
RUN pip install "${LAYA_SERVE_SPEC}" \
    && python -c "from laya import serve; print('laya[serve] import ok')" \
    && command -v laya-serve >/dev/null && echo "laya-serve entrypoint ok"

USER 10001
EXPOSE 8000

# LAYA_MODEL_PATH / LAYA_DEVICE / LAYA_API_KEY 等运行期配置由 compose 注入。
CMD ["laya-serve"]
