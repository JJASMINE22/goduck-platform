<p align="left">
  <img src="https://img.shields.io/badge/%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87-2EA44F?style=for-the-badge" alt="简体中文">
  <a href="README.en.md"><img src="https://img.shields.io/badge/English-555555?style=for-the-badge" alt="English"></a>
</p>

<p align="center">
  <img src="logo.png" alt="goduck-platform" width="440">
</p>

<p align="center">
  <b>搭建属于你 / 团队的 Personal Agent</b> —— 基于 deepagents + LangGraph + MCP 的全栈开源平台
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=black" alt="React 19">
  <img src="https://img.shields.io/badge/LangGraph-agent-1C3C3C" alt="LangGraph">
  <img src="https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white" alt="PostgreSQL 16">
  <img src="https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white" alt="Docker Compose">
  <a href="./LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>
</p>

## 核心亮点

- **智能体热更新**：新增与替换式更新，工具 / 技能随时增删，重启自动加载
- **会话与智能体解耦**：同一会话逐回合切换智能体，上下文 / 回撤 / 中止 / HITL 不受影响（悬空中断期锁定）；更新智能体不影响既有会话
- **数据可回溯**：删除智能体后其会话与 checkpoint 保留；配置历史 append-only，可一键还原

## 目录

- [功能总览](#overview)
- [架构](#architecture)
- [技术栈](#tech-stack)
- [快速开始](#quickstart)
  - [方式 A：Docker 一体化部署（推荐）](#deploy-docker)
  - [方式 B：本地开发运行](#deploy-local)
- [首次使用向导](#first-run)
- [配置速查](#config)
- [能力开关](#capabilities)
  - [知识库 RAG](#cap-kb)
  - [语音输入](#cap-voice)
  - [JEV 智能审批](#cap-jev)
- [目录结构](#structure)
- [代码质量](#quality)
- [Roadmap](#roadmap)
- [常见问题 FAQ](#faq)
- [详细文档](#docs)
- [许可](#license) / [鸣谢](#ack)

<a id="overview"></a>

## 功能总览

### 智能体与会话

| 能力 | 说明 | 入口 |
|---|---|---|
| 智能体编排 | AgentConfig 树形编辑、实时校验、Mermaid 委派拓扑、配置历史与一键还原、YAML 导出 | `/agents` |
| 流式会话 | SSE 逐 token 输出、HITL（审批 / 编辑 / 拒绝 / 追加回复）、TODO 计划面板、回合时间线 | `/sessions` |
| 排队发送 | 回合运行中 / HITL 悬空中发送的消息自动入队，回合结束后按序自动续发；支持暂停恢复、取消拯救、中止冻结 | 会话输入框（排队按钮）+ 消息流队列条带 |
| 智能体切换 | 同一会话逐回合选用不同智能体，上下文 / 回撤 / 中止 / HITL 机制不受影响 | 会话输入框选择器 |
| 定时任务 | 绑定智能体周期触发、立即补跑、运行历史、失败熔断 | `/cron` |
| 工作区 | 用户隔离文件树、预览 / 下载 / 删除、文件变更徽标（`+3 −2`） | 会话右侧面板 |

### 输入与知识

| 能力 | 说明 | 入口 |
|---|---|---|
| 附件多模态 | 图片 / 文档上传（MinIO 持久化），视觉模型识图、文档解析 | 会话输入框 |
| 知识库 RAG | pdf/docx 入库向量化（ES），会话锚定知识库，以用户提示注入触发 agent 检索作答 | `/settings/knowledge` |
| 语音输入 | 长按说话实时转写，松开自动触发会话回合（AssemblyAI Streaming STT） | 会话输入框 |

### 扩展与管理

| 能力 | 说明 | 入口 |
|---|---|---|
| 用户体系 | 平台自带登录：注册审批、角色（manager / user）、会话令牌即时吊销、多用户工作区隔离 | 登录页 · 个人中心 · 管理页 |
| 模型管理 | OpenAI 兼容多供应商注册（OpenAI / DeepSeek / Qwen / 本地），连通性测试、密钥轮换 | `/settings/models` |
| MCP 工具 | stdio / http MCP server 运行时注册，工具按 `{server}__{tool}` 命名空间化，健康检查 | `/settings/mcp` |
| 技能池 | 跨智能体共享的 `/skills/` 目录，multipart 上传，即传即用 | `/settings/skills` |
| JEV 智能审批 | `allow_shell` 命令概率门控（shadow / enforce 灰度）、rubric 评分快道、KB 检索降噪 | 环境变量开关，见[能力开关](#capabilities) |
| 运行时依赖补齐 | 开启 `allow_shell` 的智能体可在会话中用 `uv / pip / npm` 装包补齐 MCP 前置依赖（逐条人工审批）⚠️ 详见 [backend/README.md](./backend/README.md) | 会话内 |

<a id="architecture"></a>

## 架构

分层模块化单体，请求链路：`API 层 → Service 层 → Runtime(deepagents) → Persistence → Infra`。

```text
浏览器
  │  HTTP / SSE / WebSocket
  ▼
Nginx 边缘网关（容器部署唯一对外入口 :80；本地开发由 Vite proxy 承担）
  ├── /          → Frontend（React 19 SPA，nginx 静态服务）
  └── /api/*     → Backend（FastAPI + LangGraph + deepagents）
                    ├── PostgreSQL 16     业务表(15张) + LangGraph checkpoints / store
                    ├── MinIO             附件与知识库原文字节持久层
                    ├── Elasticsearch     知识库向量 / 全文检索
                    ├── TEI（compose 内置） 向量化（OpenAI 兼容 /v1/embeddings）
                    ├── MCP servers       stdio / http 工具生态（含本仓库 mcp-servers 网关）
                    └── AssemblyAI（外部） 语音输入 Streaming STT（可选）
```

关键设计：

- **数据驱动**：模型（LLM + API Key）与 MCP server 均存于 DB 表，经 API 运行时注册，**不在环境变量里**；重启自动加载。
- **能力开关**：`/health` 下发 `capabilities`，前端据此驱动菜单与入口可见性——未启用的能力整个 UI 无痕隐藏。
- **智能体无版本化**：`id` 全程稳定；更新 = 原地覆写配置 + 追加历史快照；删除智能体不会连带会话（见核心亮点）。

<a id="tech-stack"></a>

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | FastAPI · LangGraph · deepagents · langchain-openai · psycopg3 · MinIO · Elasticsearch · pydantic-settings |
| 前端 | React 19 · TypeScript · Vite · Ant Design · Zustand · TanStack Query · Mermaid · @xyflow/react · react-hook-form |
| MCP 生态 | fastmcp · langchain-mcp-adapters · uv/uvx · node/npx（镜像内预热 MCP 包缓存） |
| 基础设施 | PostgreSQL 16 · MinIO · Elasticsearch 8 · TEI · Nginx（docker-compose 编排 8 服务） |

<a id="quickstart"></a>

## 快速开始

<a id="deploy-docker"></a>

### 方式 A：Docker 一体化部署（推荐）

适用于服务器 / NAS / 内网环境长期运行。前置要求：**Docker + Docker Compose v2**。

```bash
# 1) 凭据与账号（必做一次）
#    nginx basic auth：边缘门禁（文件已 gitignore，缺失时 nginx 起不来属预期防呆）
printf 'admin:$(openssl passwd -apr1 "你的强密码")\n' > nginx/.htpasswd
#    平台登录：引导首个管理员（BOOTSTRAP_USERS，create-if-absent，详见 .env.example）
printf 'BOOTSTRAP_USERS=admin:管理员:manager:你的强密码\n' > .env

# 2) 准备宿主机数据目录 + 预下嵌入模型（TEI 以本地路径加载，运行期零下载）
mkdir -p volumes/backend volumes/docling volumes/tei
cp -r backend/data/skills volumes/backend/skills          # 技能种子（一次性）
sudo chown -R 1001:1001 volumes/backend volumes/docling  # 容器内 app 用户 uid=1001
docker run --rm -e HF_ENDPOINT=https://hf-mirror.com -v "$PWD/volumes/tei:/out" \
  python:3.11-slim bash -c "pip -q install huggingface_hub && huggingface-cli download \
   Qwen/Qwen3-Embedding-0.6B --local-dir /out/Qwen3-Embedding-0.6B"
# （默认走国内镜像 hf-mirror.com，免梯子；海外机器去掉 -e HF_ENDPOINT 即可。
#   或魔搭直下：pip -q install modelscope && modelscope download \
#     --model Qwen/Qwen3-Embedding-0.6B --local_dir /out/Qwen3-Embedding-0.6B）

# 3)（可选）在 .env 追加覆盖项（数据根 VOLUMES_ROOT 等，默认 ./volumes）
vi .env

# 4) 构建并启动全部 8 个服务（postgres / minio / elasticsearch / tei / backend / mcp-servers / frontend / nginx）
docker compose up -d --build

# 5) 就绪检查：日志出现 "goduck-platform started"
docker compose logs -f backend
```

启动完成：

| 访问目标 | 地址 |
|---|---|
| **平台入口（唯一对外）** | `http://<服务器IP>/`（端口由 `EDGE_PORT` 控制，默认 80；双层登录 = nginx basic auth 边缘门禁 → 平台账号，`BOOTSTRAP_USERS` 引导） |
| 后端 API 文档 | `http://<服务器IP>/api/docs` |
| MinIO 控制台（调试） | `http://127.0.0.1:9001`（仅本机/SSH 隧道；`minioadmin / minioadmin123`） |
| Elasticsearch（调试） | `http://127.0.0.1:9200`（仅本机/SSH 隧道） |

> - 仅 nginx 对外发布端口；backend / frontend 走内部网络；postgres / minio / es / tei 的调试端口已全部绑定 `127.0.0.1`（LAN 不可见），缺省即生产安全，无需删改。如需对外/远程暴露，由部署方自行选择网关方案（nginx basic auth 为边缘门禁，平台内另有登录体系）。
> - 数据持久化：pg / minio / es 用命名卷；**backend 数据、docling 模型、TEI 模型用宿主机 bind mount**（默认 `./volumes/`，`.env` 的 `VOLUMES_ROOT` 可整体挪位，已 gitignore），重打镜像 / 重建容器不丢，且模型预下后重启不再联网下载。
> - 嵌入服务（TEI）已内置 compose：默认模型 `Qwen/Qwen3-Embedding-0.6B`，首次启动联网拉模型入卷（一次性），此后离线可用；改用外部 OpenAI 兼容端点：`.env` 覆盖 `EMBEDDING_BASE_URL` 并 `docker compose stop tei`（详见[能力开关 · 知识库](#cap-kb)）。
> - 不需要知识库？设 `KB_ENABLED=false`，可不依赖 TEI / ES。
> - 知识库检索工具（kb_search）：compose 已内置 `mcp-servers` 网关；部署后到 `/settings/mcp` 注册 HTTP server 指向 `http://mcp-servers:8088/mcp/`（一次性，存 DB 重启自动加载），会话锚定知识库后 agent 即可调用。

常用运维命令：

```bash
docker compose logs -f nginx            # 排查网关
docker compose restart backend          # 重启后端（自动重载 DB 中的模型 / MCP / 智能体）
docker compose up -d --build backend    # 改后端代码后重建单个服务
# 版本更新（pull 后按需重建；数据在命名卷 + ./volumes，重建容器不丢）
git pull
docker compose up -d --build backend frontend mcp-servers   # 代码服务按需列
docker image prune -f                  # 清理被替换的旧悬空镜像
docker compose down                     # 停止（保留数据卷）
docker compose down -v                  # 停止并清空命名卷（volumes/ 另见 FAQ 重置条目）
```

<details>
<summary><b>GPU 加速 docling 解析 + TEI 嵌入（默认内置；NVIDIA，RTX 3080 实测配套）</b></summary>

GPU 支持（NVIDIA；RTX 3080 10G 实测）已内置 docker-compose.yml，
无需 override 文件。镜像已预装 CUDA 版 torch（`torch 2.6.0+cu124`，见
`backend/Dockerfile` "0)" 层），docling 的 layout / TableFormer 模型自动走 GPU
（RapidOCR 仍为 CPU，仅扫描页触发）；TEI 直接以 `cuda-1.9` 镜像运行走 GPU 嵌入
（0.6B fp16 ≈1.5GB 显存，与 docling 共用单卡，10GB 卡可并存）。
宿主机三步前置（Ubuntu 示例，一次性）：

```bash
# 1) NVIDIA 驱动 570 系（≥550 即可跑 cu124；RTX 3080 / sm_86）
sudo apt-get update && sudo apt-get install -y nvidia-driver-570
sudo reboot
nvidia-smi                              # 期望: Driver Version: 570.x

# 2) NVIDIA Container Toolkit（容器内可见 GPU 的关键）
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker

# 3) 启动（GPU 已内置 compose，常规单文件命令即可）
docker compose up -d --build
```

验证（torch 直连 GPU 即 驱动→工具包→容器 全链路打通）：

```bash
docker compose exec backend \
  python -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0))"
# 期望: 2.6.0+cu124 True NVIDIA GeForce RTX 3080
```

> - 未装工具包/驱动：backend / tei 起不来（`could not select device driver nvidia`）——装齐后 `docker compose up -d` 即恢复；临时在无 GPU 机器跑：注释 backend/tei 两处 `deploy` 段并 `.env` 置 `KB_DOCLING_DEVICE=auto`、`TEI_IMAGE=cpu-1.9`。
> - GPU 镜像体积比 CPU 大 +4~6GB（CUDA 运行时随 torch wheel 打入）；宿主机**无需**装 CUDA Toolkit。

</details>

<a id="deploy-local"></a>

### 方式 B：本地开发运行

前置要求：**Docker**（起基础设施）、**Python ≥ 3.10**、**Node ≥ 20**。命令为 PowerShell 写法（Linux/macOS 将 `copy` 替换为 `cp`）。

#### 1. 启动基础设施

```powershell
docker compose up -d postgres minio elasticsearch tei
# PostgreSQL → localhost:5433   MinIO → localhost:9000 / 控制台 9001   ES → localhost:9200
# TEI → localhost:8082（backend/.env 的 EMBEDDING_BASE_URL 默认即指向它）
```

#### 2. 启动后端

```powershell
cd backend
copy .env.example .env      # 默认值已与 docker-compose 对齐
pip install -e .
python -m app.main          # 或 uvicorn app.main:app --reload
# 日志出现 "goduck-platform started" 即成功；健康检查 http://localhost:8000/health
```

> - 所有业务表 + LangGraph 原生表由后端启动时自动建表，无需手动迁移。
> - 业务接口均需登录态：本地开发同样经 `BOOTSTRAP_USERS` 引导账号（见 `backend/.env.example`）。
> - 可选能力（知识库 / 语音 / JEV）的开启条件见[能力开关](#capabilities)。

#### 3. 启动前端

```powershell
cd frontend
npm install
npm run dev                 # http://localhost:5173 （/api 自动代理到 8000，含 WS）
```

#### 4.（可选）启动 mcp-servers 网关（附件解析 + kb_search）

```powershell
cd mcp-servers
copy .env.example .env      # 填 MinIO 凭证、智谱 key（与 backend 的 EMBEDDING / ES 配置一致）
pip install -r requirements.txt
python main.py              # 监听 0.0.0.0:8088/mcp
```

然后在 `/settings/mcp` 注册，或：

```powershell
curl -X POST http://localhost:8000/mcp/servers -H "Content-Type: application/json" `
  -d '{\"name\":\"attachment_wrapper\",\"kind\":\"http\",\"config\":{\"url\":\"http://127.0.0.1:8088/mcp\"},\"enabled\":true}'
```

#### 5.（可选）灌入种子智能体

```powershell
cd backend
python -m app.seed agent_confs/default.yaml --name "Default Gateway" --creator admin
```

<a id="first-run"></a>

## 首次使用向导

界面配置存在依赖顺序，登录平台后（管理员由 `BOOTSTRAP_USERS` 引导，新用户注册需管理员审批）按 ①→④ 操作：

```text
① 注册模型 ──▶ ② 创建智能体 ──▶ ③（可选）MCP / 技能 / 知识库 ──▶ ④ 开始会话
```

1. **注册模型**：`/settings/models` → 注册模型 → 填 `model_type` / `api_key` / `base_url`（可先「测试连接」，api_key 永不回显）。任何 OpenAI 兼容端点均可：OpenAI / DeepSeek / Qwen / Ollama / vLLM 等。
2. **创建智能体**：`/agents/new` → 树形编辑 AgentConfig（主智能体 + 子智能体委派）→「绑定模型」选择 ① 注册的模型 → 保存（右侧实时 Mermaid 拓扑预览）。智能体 `model_id` 必填，**模型必须先于智能体注册**。
3. **（可选）扩充能力**：
   - `/settings/mcp`：注册 MCP server（内置网关 `UnifiedGateway` 提供系统状态等工具；`mcp-servers/` 网关提供附件解析与 `kb_search`），注册后在智能体 `tool_names` 中引用 `{server}__{tool}`。
   - `/settings/skills`：上传技能（一个技能 = 一个目录，含 SKILL.md），智能体以 `/skills/<name>/` 引用。
   - `/settings/knowledge`：新建知识库 → 上传 pdf/docx → 等待后台向量化完成。
4. **会话测试**：`/sessions` → 基于智能体创建会话：
   - SSE 流式输出 + TODO 计划面板 + 回合时间线；
   - 挂附件（图片走视觉模型，文档自动解析）；
   - 锚定知识库（输入框左侧选择知识库，agent 先检索后作答）；
   - 长按麦克风说话（启用语音时）；
   - 右侧工作区面板浏览 / 下载 / 删除 agent 产物；
   - 子智能体工具配置 `interrupt_on` 时出现 HITL 审批卡片。

<a id="config"></a>

## 配置速查

配置统一走环境变量 / `.env`，**禁止硬编码**。完整清单见 [`backend/.env.example`](./backend/.env.example)（逐项带注释）。

### 根目录 `.env`（docker compose 层）

| 变量 | 默认 | 说明 |
|---|---|---|
| `EDGE_PORT` | `80` | nginx 唯一对外端口 |
| `POSTGRES_USER / PASSWORD / DB / PORT` | `goduck / changeme / langgraph_db / 5433` | PostgreSQL 凭据与宿主端口 |
| `MINIO_ROOT_USER / PASSWORD` | `minioadmin / minioadmin123` | MinIO 凭据 |
| `KB_*` / `EMBEDDING_*` / `VOICE_*` / `JEV_*` | — | 能力开关，见[能力开关](#capabilities) |
| `LOG_LEVEL` | `INFO` | 后端日志级别 |

### backend/.env（本地开发）

| 变量 | 默认（本地） | 说明 |
|---|---|---|
| `DATABASE_URL` | `postgresql://goduck:changeme@localhost:5433/langgraph_db` | 容器内自动改为 `postgres:5432` |
| `MINIO_ENDPOINT` | `localhost:9000` | 容器内为 `minio:9000` |
| `ES_URL` | `http://localhost:9200` | 容器内为 `http://elasticsearch:9200` |
| `LOCAL_ROOT` | `data` | workspace / memories / skills 三目录的根（容器内 `/data` 命名卷） |
| `ROOT_PATH` | 空 | 经 nginx 剥 `/api` 时设为 `/api`（compose 已配好） |
| `MCP_CONNECT_TIMEOUT` | `45` | 单个 MCP server 连接超时（防 uvx 拉包重试拖死接口） |
| `ATTACHMENT_MAX_SIZE_MB` | `10` | 单附件大小上限 |

### mcp-servers/.env

`DATABASE_URL`（只读）经 `kb → embedding` 注册表解析各库向量模型；未配置/不可达自动回落 `EMBEDDING_*`（与 backend 同值即可，仅为回落一致性）。详见 [mcp-servers/README.md](./mcp-servers/README.md)。

<a id="capabilities"></a>

## 能力开关

可选能力均由后端环境变量驱动：`/health` 下发 `capabilities` 作为单一真相源，前端据此显隐菜单与入口，**无需任何前端配置**。

| 能力 | 总开关 | 默认 | 开启依赖 | 体验提升 |
|---|---|---|---|---|
| [知识库 RAG](#cap-kb) | `KB_ENABLED` | ✅ 开 | TEI（compose 内置）+ ES | 会话锚定私有知识，先检索后作答 |
| [语音输入](#cap-voice) | `VOICE_ENABLED` | ⛔ 关 | AssemblyAI API Key | 长按说话，实时转写直发会话 |
| [JEV 智能审批](#cap-jev) | `JEV_ENABLED` | ⛔ 关 | laya 服务（compose 内置）+ API Key | `allow_shell` 高频只读/依赖补齐命令免审批 |

```bash
curl http://localhost:8000/health
# {"status":"ok","capabilities":{"kb_enabled":true,"voice_enabled":false,
#  "jev_enabled":false,"jev_rubric_enabled":false,"jev_kb_rerank_enabled":false}}
```

> 开关变更后重启后端生效；JEV 阈值类配置随图构建烘焙，改配置需重启后端或等 registry TTL 过期。

**智能体切换**（非能力开关，始终可用）：同一会话逐回合切换智能体，上下文状态、回撤、中止、HITL 等机制以会话线程为锚，不受切换影响；HITL 悬空中断期锁定选择，续跑恒在中断的那张图上。

<a id="cap-kb"></a>

### 知识库 RAG（默认开启）

**能力**：pdf/docx 上传入库向量化（ES 存储 + MinIO 原文留底，支持 reindex），会话中锚定知识库后 agent 先检索后作答。使用：`/settings/knowledge` 建库 → 上传文件 → 会话输入框左侧选择知识库锚定。

**向量模型注册化**：嵌入端点在 `/settings/embeddings` 注册（端点/维度/凭据随行走，保存前经端点实测维度）；建库时下拉绑定，维度不再需要两侧运维对齐。换绑 = 破坏性变更（清索引块 + 文档重置待解析，原始文件保留）；被引用模型的端点/维度锁定不可改（防向量空间静默错配）。

**触发机制（用户提示注入）**：检索不植入图内核——锚定库列表（kb_id）与检索指令前置注入本轮用户消息，由 agent 原生 function-calling 主动调用其持有的检索工具（如 `kb_search`）；未持有检索工具则如实降级作答。

**配置**（`.env`；`EMBEDDING_*` 双用途 = 首启 seed 默认向量模型行 + 未绑定库/检索侧回落）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `KB_ENABLED` | `true` | 总开关；`false` 时知识库路由不挂载、前端入口隐藏，不依赖 TEI / ES |
| `EMBEDDING_BASE_URL` | `http://tei:8082/v1`（compose 层） | seed 默认行端点 + 回落路径；compose 内置 TEI 默认服务名直连 |
| `EMBEDDING_DIMS` | `512` | seed 默认行维度 + 回落维度（注册化后由向量模型行决定，无需两侧对齐） |
| `EMBEDDING_SEED_NAME` | `Qwen3-Embedding-0.6B（TEI 内置）` | seed 默认行名（仅首启建行生效；改名即用户接管，不按名重建） |
| `EMBEDDING_BATCH_SIZE` | `4`（compose 层 `24`） | 须 ≤ TEI `--max-client-batch-size` 且单批 token ≤ `--max-batch-tokens`（内置 tei 已对齐：32 / 32768），否则入库延迟成倍放大 |
| `KB_PARSER_ENGINE` | `legacy` | `legacy` = pymupdf 文本层 + 滑窗切块；`docling` = 结构化解析 + 扫描件 OCR（需先安装 `docling>=2.130,<3`） |
| `KB_DOCLING_DEVICE` | `cuda`（compose 层） | docling 推理设备 `auto/cpu/cuda/mps`；GPU 已内置 compose，默认锁 `cuda`（驱动 570 系 + Container Toolkit，见[快速开始](#deploy-docker)） |
| `KB_TOP_K` | `5` | 会话检索条数 |

<details>
<summary><b>内置 TEI 嵌入服务（本地模型目录，运行期零下载）</b></summary>

compose 已内置 TEI 服务（`ghcr.io/huggingface/text-embeddings-inference`，默认
CUDA 镜像 `cuda-1.9`；CPU 机器可 `.env` 置 `TEI_IMAGE=cpu-1.9`）。`--model-id`
默认指向**容器内本地目录** `/data/Qwen3-Embedding-0.6B`（bind mount 自宿主机
`volumes/tei/`）：TEI 判定路径存在即本地加载，全程不触网。模型原生 1024 维，
经 MRL 截断到 512，与 `EMBEDDING_DIMS` 对齐。目录内容 = HF 快照全量文件：

```text
volumes/tei/
└── Qwen3-Embedding-0.6B/     ← 容器内路径 /data/Qwen3-Embedding-0.6B（--model-id）
    ├── config.json
    ├── model.safetensors     # ≈1.2GB
    ├── tokenizer.json / tokenizer_config.json / ...
```

预下（任一联网机器；默认国内镜像免梯子，海外机器去掉 `HF_ENDPOINT=` 前缀即可）：

```bash
HF_ENDPOINT=https://hf-mirror.com huggingface-cli download Qwen/Qwen3-Embedding-0.6B \
  --local-dir volumes/tei/Qwen3-Embedding-0.6B

# 或魔搭（ModelScope）直下，产物目录同构：
modelscope download --model Qwen/Qwen3-Embedding-0.6B \
  --local_dir volumes/tei/Qwen3-Embedding-0.6B
```

可调项（根目录 `.env`）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `TEI_MODEL_ID` | `/data/Qwen3-Embedding-0.6B` | 本地路径或 HF repo id；填 repo id 则首启经 hub 下载入同卷（之后离线复用） |
| `HF_ENDPOINT` | `https://hf-mirror.com` | HF hub 端点（tei 首启下载 + backend docling 预热/兜底下载共用）；海外机器可覆盖回 `https://huggingface.co` |
| `TEI_IMAGE` | `cuda-1.9` | 版本升级 / CPU 机器（`cpu-1.9`）时覆盖 |
| `TEI_PORT` | `8082` | 宿主调试端口（127.0.0.1 绑定，LAN 不可见） |
| `VOLUMES_ROOT` | `./volumes` | backend / docling / tei 三处 bind mount 的宿主机根目录 |

就绪观测：`docker compose logs -f tei` 出现 Ready、`docker compose ps` 显 healthy。
改用外部嵌入端点：`.env` 覆盖 `EMBEDDING_BASE_URL`，并 `docker compose stop tei`
（backend 懒连接嵌入端点，无需改其他配置）。

</details>

<details>
<summary><b>docling 模型目录与预下（断网可用）</b></summary>

容器 `/models/docling`（bind mount 宿主机 `volumes/docling/`）双布局：
`artifacts/` = 生产路径（`KB_DOCLING_ARTIFACTS_PATH` 指向它，缺件显式失败、
绝不运行时下载）；`hf-cache/` = 简化模式兜底（`HF_HOME`，artifacts 置空时经
hub 加载）。

**预下推荐用容器内 docling-tools**（自动配套镜像内 docling 版本的 revision；
layout / tableformer 取自 HF，**RapidOCR 取自 ModelScope 而非 HF**，且 `org--repo`
目录名随 docling 版本漂移——手动搬运易错）。HF 下载默认经 compose 注入的
`HF_ENDPOINT=https://hf-mirror.com`（免梯子；`.env` 可覆盖回官方源），RapidOCR
本就直连 ModelScope——无梯子服务器可原样执行：

```bash
mkdir -p volumes/docling && sudo chown -R 1001:1001 volumes/docling
docker compose run --rm --no-deps -e HF_HUB_OFFLINE=0 backend \
  docling-tools models download layout tableformer rapidocr -o /models/docling/artifacts
ls volumes/docling/artifacts   # 产物目录名即权威（org 前缀随版本可能变化）
```

以当前 docling `<3` 最新版（HF org 已迁至 `docling-project`；旧版为 `ds4sd`）为例：

```mermaid
graph TD
    V["volumes/docling/<br/>（容器 /models/docling）"]
    V --> A["artifacts/<br/>KB_DOCLING_ARTIFACTS_PATH"]
    V --> H["hf-cache/<br/>HF_HOME（简化模式兜底）"]
    A --> L["docling-project--docling-layout-heron/<br/>HF: docling-project/docling-layout-heron<br/>仓库根即模型文件（config/权重）"]
    A --> T["docling-project--docling-models/<br/>HF: docling-project/docling-models（pin revision）"]
    T --> T2["model_artifacts/tableformer/"]
    T2 --> T3["accurate/ ← 本项目 TableFormerMode.ACCURATE<br/>tm_config.json + 权重"]
    T2 --> T4["fast/"]
    A --> R["RapidOcr/ ← 非 HF！源自 ModelScope（RapidAI/RapidOCR）<br/>onnx/PP-OCRv6/…、resources/fonts/"]
```

纯手动（不推荐；layout / tableformer 走 HF 镜像，RapidOCR 可经魔搭 CLI 精准拉取）：

- **layout / tableformer（HF）**：
  `HF_ENDPOINT=https://hf-mirror.com huggingface-cli download <repo_id> --revision <rev> --local-dir volumes/docling/artifacts/<org>--<repo>`，
  目录名与 revision 必须与容器内 docling 版本一致（当前 2.135.0：
  `docling-project/docling-layout-heron` 与 `docling-project/docling-layout-heron-onnx`
  均为 `main`，`docling-project/docling-models` 为 `v2.3.0`）；缺件按报错提示补
  `docling-tools models download-hf-repo <repo_id>`。
- **RapidOCR（魔搭 `RapidAI/RapidOCR`，免梯子）**：本管线实际只需 3 个 onnx 权重
  （onnxruntime 后端 / `lang=["ch"]` → PP-OCRv6 det+rec `small` + PP-OCRv4 cls
  `mobile`），且 docling 按 `RapidOcr/` 内**平铺文件名**校验——下载后须摊平归位：

  ```bash
  pip install -U modelscope
  modelscope download --model RapidAI/RapidOCR --revision v3.9.1 \
    --include "onnx/PP-OCRv6/det/PP-OCRv6_det_small.onnx" \
              "onnx/PP-OCRv6/rec/PP-OCRv6_rec_small.onnx" \
              "onnx/PP-OCRv4/cls/ch_ppocr_mobile_v2.0_cls_mobile.onnx" \
    --local_dir /tmp/rapidocr-src
  mkdir -p volumes/docling/artifacts/RapidOcr
  find /tmp/rapidocr-src -name "*.onnx" -exec cp {} volumes/docling/artifacts/RapidOcr/ \;
  # 产物应为三个平铺文件：PP-OCRv6_det_small.onnx / PP-OCRv6_rec_small.onnx /
  #                       ch_ppocr_mobile_v2.0_cls_mobile.onnx
  ```

  （`--revision v3.9.1` 对应 requirements 锁定的 rapidocr 版本，其 URL 表随版本
  变化——升级需同步核对。）

docling 升级（requirements 版本变更）后需重新预热并断网验证（上传真实 PDF 以
`KB_PARSER_ENGINE=docling` 重解析成功）。

</details>

<a id="cap-voice"></a>

### 语音输入（默认关闭）

**能力**：长按麦克风说话 → 实时转写直显输入框 → 松开自动触发会话回合。单 WebSocket 全管线（浏览器 ⇄ 后端 ⇄ AssemblyAI Streaming STT），nginx / Vite 代理已配置 WS 透传。

**开启步骤**：

```bash
# .env
VOICE_ENABLED=true
ASSEMBLYAI_API_KEY=<你的 key>     # https://www.assemblyai.com/
```

重启后端，会话输入框出现麦克风入口。key 缺失时自动降级：路由不挂载 + 启动警告（不崩）。

| 变量 | 默认 | 说明 |
|---|---|---|
| `STT_SPEECH_MODEL` | `universal-3-5-pro` | 旗舰模型，18 语种含中文；`universal-streaming-english`（仅英语，低成本）/ `universal-streaming-multilingual`（不含中文）可切换 |
| `STT_FLUSH_TIMEOUT` | `5` | 松开后等待供应商终稿的兜底秒数 |
| `VOICE_HEARTBEAT_INTERVAL` / `VOICE_IDLE_TIMEOUT` | `30` / `600` | 常驻 WS 保活 ping 与空闲回收（秒） |

<a id="cap-jev"></a>

### JEV 智能审批（默认关闭）

**能力**：为开启 `allow_shell` 的智能体减轻 HITL 负担——判决后端为 compose 内置的 [laya](https://github.com/conversationai/laya)-serve（Apache 2.0 开源 System 1 判决模型，Jev wire 协议兼容端点），基于概率门控决定「是否跳过人工审批」，全程 fail-closed（不可用 / 超时 / 解析失败一律问人，绝不放行）：

```text
shell 命令
  ├─ L1 只读白名单（ls / cat / git status…）           → 静默放行，不调 Jev
  ├─ L1 允许项扩展（JEV_SHELL_L1_ALLOW，默认开）
  │    ├─ 在册 MCP 前置依赖补齐（uvx --from / pip install，
  │    │  包名以 mcp_server 表登记为准 + JEV_SHELL_ALLOW_PKGS） → 静默放行
  │    └─ 工作区常规操作（mkdir/cp/mv/touch/python 脚本，
  │       仅相对路径、拒绝 `..` 与绝对路径）               → 静默放行
  │    （`&&` 复合链逐段判定，任一段不属允许面 → 整链落 L2）
  ├─ L1 高危黑名单（rm -rf / format / dd…）            → 跳过 Jev，强制人工审批
  └─ L2 Jev 灰区判决（送判 当前命令 + 近期命令历史）
       ├─ shadow：仅概率标注 + 落审计，行为与现状一致（灰度观察期）
       ├─ enforce：高置信"常规命令" → 自动执行；其余 → 人工审批
       └─ 不可用 / 超时 / 解析失败 → 人工审批（fail-closed）
```

**开启步骤**：

```bash
# .env
JEV_ENABLED=true
TYPESAFE_API_KEY=<key>             # 与 compose laya 段的 LAYA_API_KEY 同值（一把钥匙两端）
JEV_MODE=enforce                   # shadow=只判只落审计（校准观察态）
```

| 变量 | 默认 | 说明 |
|---|---|---|
| `JEV_MODE` | `enforce` | `shadow` = 只判只落审计；`enforce` = 灰区自动放行生效 |
| `JEV_SAFE_THRESHOLD` | `0.6` | 自动放行阈值（概率与置信度双轴全过才放行），按 shadow 期分歧样本标定 |
| `JEV_TIMEOUT_SECONDS` | `5.0` | 判决硬超时（秒），超时即回落人工 |
| `JEV_MODEL` | `multilingual` | laya 侧注册名（与 `LAYA_EXTRA_MODELS` 同名注册） |
| `JEV_BASE_URL` | `http://laya:8000` | Jev wire 端点根（compose 内置 laya-serve） |
| `JEV_SHELL_L1_ALLOW` | `true` | L1 允许项扩展总开关；`false` = 一键回退扩展前行为 |
| `JEV_SHELL_ALLOW_PKGS` | 空 | 额外放行包名（逗号分隔；与 `mcp_server` 表登记包并集） |

**子门控**（均前置 `JEV_ENABLED=true` + key 就绪）：

| 变量 | 默认 | 作用 |
|---|---|---|
| `JEV_RUBRIC_ENABLED` | `false` | rubric 评分快道：全部准则 P / conf 达标时合成 satisfied、跳过 LLM grader；`shadow` / `enforce` 两态 |
| `JEV_KB_RERANK_ENABLED` | `false` | KB 检索降噪：高置信无关 chunk 才滤（`keep_floor` 保底不斩首）；`JEV_KB_MODE` 按 `shadow → annotate → enforce` 灰度 |

> 完整变量与灰度纪律见 [`backend/.env.example`](./backend/.env.example) 注释；所有 JEV 判决均落审计表，供 shadow 期标定与后验分析。

<a id="structure"></a>

## 目录结构

```text
goduck-platform/
├── backend/                  # FastAPI + LangGraph 后端（详见 backend/README.md）
│   ├── app/
│   │   ├── api/              # 路由：auth·agents·sessions·stream·models·mcp·skills·uploads·knowledge·embeddings·workspace·voice·cron
│   │   ├── services/         # 业务：认证·模型·会话·MCP·存储·RAG(向量化/向量库/入库池)·STT·JEV 客户端…
│   │   ├── core/             # deepagents 内核（图构建 / 用户隔离文件·shell 后端 / HITL / JEV 门控 / 变更追踪）
│   │   ├── models/           # 持久化：15 张业务表 DDL+CRUD + pydantic 边界模型
│   │   ├── streaming/        # SSE 事件契约
│   │   └── mcps/             # 内置 UnifiedGateway MCP 网关
│   ├── agent_confs/          # 种子智能体 YAML
│   └── Dockerfile            # 含 uv/uvx/node + MCP 包缓存预热
├── frontend/                 # React 19 前端（详见 frontend/README.md）
│   ├── src/api/              # API 域客户端
│   ├── src/features/         # agent-editor / chat / knowledge / topology / workspace
│   ├── src/pages/            # agents / sessions / cron / settings(models·mcp·skills·knowledge)
│   └── Dockerfile            # 多阶段构建 → nginx 静态服务
├── mcp-servers/              # 本地 MCP 网关集群：附件解析(image/docx) + kb_search 检索
├── nginx/                    # 边缘反代配置（SSE 关缓冲 + WebSocket 透传）
├── docker-compose.yml        # 一键全栈编排（8 服务）
├── LICENSE                   # MIT 许可证
├── logo.png                  # 项目图标（README 引用）
└── README.md
```

<a id="quality"></a>

## 代码质量

```powershell
# 后端
cd backend
ruff check app/               # 代码风格（line-length 110）

# 前端
cd frontend
npm run typecheck             # tsc 类型检查
npm run lint                  # eslint
```

<a id="roadmap"></a>

## Roadmap

- [ ] **知识调用双通道**：构建自定义 Middleware，引入 Jev | Laya 判决，赋予智能体**被动**调用知识的能力与自动化调用门控；与现行**主动**方案（会话锚定 + 提示注入，agent 自主调用 `kb_search`）并存
- [ ] **智能体级知识范围**：AgentConfig 支持配置智能体可访问的知识库集合（现为会话级锚定）
- [ ] **模型端点扩展**：兼容更多本地 / 第三方模型商的服务端点（现为 OpenAI 兼容端点注册制）

<a id="faq"></a>

## 常见问题 FAQ

<details>
<summary><b>知识库页 404 / 503？</b></summary>

`KB_ENABLED=false` 或 ES 未就绪。检查：`docker compose ps` 看 elasticsearch 是否 healthy；`curl http://localhost:8000/health` 看 `capabilities.kb_enabled`；本地开发确认 `.env` 中 `ES_URL` 可达。
</details>

<details>
<summary><b>知识库入库慢 / 嵌入超时？</b></summary>

`EMBEDDING_BATCH_SIZE` 须 ≤ TEI 的 `--max-client-batch-size`（内置 tei 为 32，超则 422）；单请求总 token 量超过 `--max-batch-tokens` 时 TEI 拆多轮前向推理，延迟成倍放大（内置 tei 已设 32768 ≥ 24 条 × ~1k token，自建端点须自行对齐）。同时确认 `EMBEDDING_TIMEOUT` 大于单批实际耗时（CPU 部署建议调大）。
</details>

<details>
<summary><b>会话里没有麦克风图标？</b></summary>

语音默认关闭。`.env` 设 `VOICE_ENABLED=true` 且填入 `ASSEMBLYAI_API_KEY` 后重启后端。
</details>

<details>
<summary><b>MCP 注册后工具列表不出现？</b></summary>

界面点「刷新」或 `POST /mcp/refresh`。stdio server（uvx/npx）首次启动需拉包：内网环境可在 server 配置的 `env` 里加 `UV_DEFAULT_INDEX` 指向 pypi 镜像；连接超时上限由 `MCP_CONNECT_TIMEOUT` 控制。
</details>

<details>
<summary><b>JEV 何时可以切 enforce？</b></summary>

`enforce` 下自动放行由 L1 确定性层（白名单 + 允许项扩展）与 L2 概率判决共同产生：L1 零误判可直接信任；L2 阈值未经 shadow 期标定时，灰区判决自然回落人工（宁多问不误放）。要进一步收紧减负率，用 `JEV_MODE=shadow` 观察审计表中「建议放行 vs 人工实际同意」的分歧样本，重标定 `JEV_SAFE_THRESHOLD` 后再切回 `enforce`。任何时刻 Jev 不可用都会 fail-closed 回落全人工审批。
</details>

<details>
<summary><b>如何彻底重置数据？</b></summary>

`docker compose down -v` 清空命名卷（业务库 + 附件 + 知识库索引），再 `sudo rm -rf volumes/` 清 bind mount 数据（backend 数据 + docling/TEI 模型），之后 `up -d` 重建。
</details>

<details>
<summary><b>Windows 本地开发注意？</b></summary>

命令示例均为 PowerShell（`copy`、反引号续行）。`python -m app.main` 自带 `--reload`；`LOCAL_ROOT` 用相对值 `data` 即可，后端会锚定到 `backend/` 目录，与启动 CWD 无关。
</details>

<a id="docs"></a>

## 详细文档

| 文档 | 内容 |
|---|---|
| [backend/README.md](./backend/README.md) | 后端架构、目录、数据模型（8 张表）、API 速览、allow_shell 运行时补依赖、文件变更徽标 |
| [frontend/README.md](./frontend/README.md) | 前端技术栈、功能、脚本、目录结构、开发约定 |
| [mcp-servers/README.md](./mcp-servers/README.md) | MCP 网关集群：附件解析 wrapper、kb_search、配置对齐要求 |
| [frontend/DESIGN.md](./frontend/DESIGN.md) | 前端设计准则与页面结构设计 |

<a id="license"></a>

## 许可

本项目代码以 [MIT License](./LICENSE) 开源；docker-compose 编排的第三方组件
（MinIO · AGPL-3.0，Elasticsearch · Elastic License v2 等）保留各自原许可。

<a id="ack"></a>

## 鸣谢

- [deepagents](https://github.com/langchain-ai/deepagents)（MIT）— 智能体内核，`backend/app/core/` 迁移自其用法原型
- [LangGraph](https://github.com/langchain-ai/langgraph)（MIT）— 图运行时与 checkpoint
- [FastAPI](https://github.com/fastapi/fastapi) / [React](https://github.com/facebook/react) / [Ant Design](https://github.com/ant-design/ant-design) 等开源项目

## 贡献

欢迎 Issue / PR 共建；参与开发前建议先阅读对应模块 README。
