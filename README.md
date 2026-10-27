<p align="center">
  <img src="logo.png" alt="goduck-platform" width="440">
</p>

<p align="center">
  <b>个人智能体搭建 / 编排 / 会话平台</b> —— 基于 deepagents + LangGraph + MCP 的全栈开源项目
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
                    ├── PostgreSQL 16     业务表(8张) + LangGraph checkpoints / store
                    ├── MinIO             附件与知识库原文字节持久层
                    ├── Elasticsearch     知识库向量 / 全文检索
                    ├── TEI（外部）        向量化（OpenAI 兼容 /v1/embeddings）
                    ├── MCP servers       stdio / http 工具生态（含本仓库 mcp-servers 网关）
                    └── AssemblyAI（外部） 语音输入 Streaming STT（可选）
```

关键设计：

- **数据驱动**：模型（LLM + API Key）与 MCP server 均存于 DB 表，经 API 运行时注册，**不在环境变量里**；重启自动加载。
- **能力开关**：`/health` 下发 `capabilities`，前端据此驱动菜单与入口可见性——未启用的能力整个 UI 无痕隐藏。
- **智能体无版本化**：`id` 全程稳定；更新 = 原地覆写配置 + 追加历史快照 + 级联删旧会话。

<a id="tech-stack"></a>

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | FastAPI · LangGraph · deepagents · langchain-openai · psycopg3 · MinIO · Elasticsearch · pydantic-settings |
| 前端 | React 19 · TypeScript · Vite · Ant Design · Zustand · TanStack Query · Mermaid · @xyflow/react · react-hook-form |
| MCP 生态 | fastmcp · langchain-mcp-adapters · uv/uvx · node/npx（镜像内预热 MCP 包缓存） |
| 基础设施 | PostgreSQL 16 · MinIO · Elasticsearch 8 · Nginx（docker-compose 编排 6 服务） |

<a id="quickstart"></a>

## 快速开始

<a id="deploy-docker"></a>

### 方式 A：Docker 一体化部署（推荐）

适用于服务器 / NAS / 内网环境长期运行。前置要求：**Docker + Docker Compose v2**。

```bash
# 1)（可选）根目录创建 .env 覆盖默认值；唯一必改项：EMBEDDING_BASE_URL（见"能力开关 · 知识库"）
vi .env

# 2) 构建并启动全部 6 个服务（postgres / minio / elasticsearch / backend / frontend / nginx）
docker compose up -d --build

# 3) 就绪检查：日志出现 "goduck-platform started"
docker compose logs -f backend
```

启动完成：

| 访问目标 | 地址 |
|---|---|
| **平台入口（唯一对外）** | `http://<服务器IP>/`（端口由 `EDGE_PORT` 控制，默认 80） |
| 后端 API 文档 | `http://<服务器IP>/api/docs` |
| MinIO 控制台（调试） | `http://<服务器IP>:9001`（`minioadmin / minioadmin123`） |
| Elasticsearch（调试） | `http://<服务器IP>:9200` |

> - 仅 nginx 对外发布端口；backend / frontend 走内部网络，postgres / minio / es 的宿主端口仅供本机调试，生产建议删除对应 `ports` 映射。
> - 数据全部落在命名卷：`goduck_pgdata` / `goduck_miniodata` / `goduck_esdata` / `goduck_backend_data`，重打镜像不丢数据。
> - 嵌入服务（TEI）默认不在 compose 内：将 `EMBEDDING_BASE_URL` 指向任意 OpenAI 兼容嵌入端点即可，自建示例见[能力开关 · 知识库](#cap-kb)。
> - 不需要知识库？设 `KB_ENABLED=false`，可不依赖 TEI / ES。

常用运维命令：

```bash
docker compose logs -f nginx            # 排查网关
docker compose restart backend          # 重启后端（自动重载 DB 中的模型 / MCP / 智能体）
docker compose up -d --build backend    # 改后端代码后重建单个服务
docker compose down                     # 停止（保留数据卷）
docker compose down -v                  # 停止并清空全部数据（谨慎）
```

<details>
<summary><b>可选：GPU 加速 docling 解析（NVIDIA，RTX 3080 实测配套）</b></summary>

镜像已预装 CUDA 版 torch（`torch 2.6.0+cu124`，见 `backend/Dockerfile` "0)" 层），
docling 的 layout / TableFormer 模型自动走 GPU（RapidOCR 仍为 CPU，仅扫描页触发）。
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

# 3) 叠加 GPU override 启动（此后所有 compose 命令带双 -f）
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

验证（torch 直连 GPU 即 驱动→工具包→容器 全链路打通）：

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml exec backend \
  python -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0))"
# 期望: 2.6.0+cu124 True NVIDIA GeForce RTX 3080
```

> - 未装工具包/驱动就叠加 override：backend 起不来（`could not select device driver nvidia`），去掉 `-f docker-compose.gpu.yml` 即回退 CPU 部署（`KB_DOCLING_DEVICE=auto` 自动回落 cpu）。
> - GPU 镜像体积比 CPU 大 +4~6GB（CUDA 运行时随 torch wheel 打入）；宿主机**无需**装 CUDA Toolkit。

</details>

<a id="deploy-local"></a>

### 方式 B：本地开发运行

前置要求：**Docker**（起基础设施）、**Python ≥ 3.10**、**Node ≥ 20**。命令为 PowerShell 写法（Linux/macOS 将 `copy` 替换为 `cp`）。

#### 1. 启动基础设施

```powershell
docker compose up -d postgres minio elasticsearch
# PostgreSQL → localhost:5433   MinIO → localhost:9000 / 控制台 9001   ES → localhost:9200
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

界面配置存在依赖顺序，按 ①→④ 操作：

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

MinIO / ES / EMBEDDING 系列须与 backend 侧**严格一致**（尤其 `EMBEDDING_DIMS`，维度不对称会导致检索失败）。详见 [mcp-servers/README.md](./mcp-servers/README.md)。

<a id="capabilities"></a>

## 能力开关

可选能力均由后端环境变量驱动：`/health` 下发 `capabilities` 作为单一真相源，前端据此显隐菜单与入口，**无需任何前端配置**。

| 能力 | 总开关 | 默认 | 开启依赖 | 体验提升 |
|---|---|---|---|---|
| [知识库 RAG](#cap-kb) | `KB_ENABLED` | ✅ 开 | 外部嵌入端点（TEI）+ ES | 会话锚定私有知识，先检索后作答 |
| [语音输入](#cap-voice) | `VOICE_ENABLED` | ⛔ 关 | AssemblyAI API Key | 长按说话，实时转写直发会话 |
| [JEV 智能审批](#cap-jev) | `JEV_ENABLED` | ⛔ 关 | TypeSafe API Key | `allow_shell` 高频只读命令免审批 |

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

**触发机制（用户提示注入）**：检索不植入图内核——锚定库列表（kb_id）与检索指令前置注入本轮用户消息，由 agent 原生 function-calling 主动调用其持有的检索工具（如 `kb_search`）；未持有检索工具则如实降级作答。

**配置**（`.env`）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `KB_ENABLED` | `true` | 总开关；`false` 时知识库路由不挂载、前端入口隐藏，不依赖 TEI / ES |
| `EMBEDDING_BASE_URL` | 占位地址 | **必改**：OpenAI 兼容嵌入端点（TEI 等） |
| `EMBEDDING_DIMS` | `512` | 全局向量维度，backend 与 mcp-servers **必须一致** |
| `EMBEDDING_BATCH_SIZE` | `4`（compose 层 `24`） | 须对齐 TEI `--max-batch-size`，否则入库延迟成倍放大 |
| `KB_PARSER_ENGINE` | `legacy` | `legacy` = pymupdf 文本层 + 滑窗切块；`docling` = 结构化解析 + 扫描件 OCR（需先安装 `docling>=2.130,<3`） |
| `KB_DOCLING_DEVICE` | `auto` | docling 推理设备 `auto/cpu/cuda/mps`；GPU 主机叠加 `docker-compose.gpu.yml` 后锁 `cuda`（驱动 570 系 + Container Toolkit，见[快速开始](#deploy-docker)） |
| `KB_TOP_K` | `5` | 会话检索条数 |

<details>
<summary><b>可选：自建 TEI 嵌入服务（与 .env 默认值对齐的示例）</b></summary>

```bash
# Qwen3-Embedding，MRL 截断到 512 维；--max-batch-size 与 EMBEDDING_BATCH_SIZE 对齐（默认 4）
docker run -d --gpus all -p 8082:80 \
  ghcr.io/huggingface/text-embeddings-inference:latest \
  --model-id Qwen/Qwen3-Embedding-0.6B \
  --max-batch-size 4 --max-client-batch-size 32
# 然后 .env: EMBEDDING_BASE_URL=http://<TEI机器IP>:8082/v1
```

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

**能力**：为开启 `allow_shell` 的智能体减轻 HITL 负担——基于 [TypeSafe](https://docs.typesafe.ai) System One 的概率门控决定「是否跳过人工审批」，全程 fail-closed（不可用 / 超时 / 解析失败一律问人，绝不放行）：

```text
shell 命令
  ├─ L1 只读白名单（ls / cat / git status…）   → 静默放行，不调 Jev
  ├─ L1 高危黑名单（rm -rf / format / dd…）    → 跳过 Jev，强制人工审批
  └─ L2 Jev 灰区判决（送判 当前命令 + 近期命令历史）
       ├─ shadow：仅概率标注 + 落审计，行为与现状一致（灰度观察期）
       ├─ enforce：高置信"常规命令" → 自动执行；其余 → 人工审批
       └─ 不可用 / 超时 / 解析失败 → 人工审批（fail-closed）
```

**开启步骤**：

```bash
# .env
JEV_ENABLED=true
TYPESAFE_API_KEY=<你的 key>       # https://docs.typesafe.ai
JEV_MODE=shadow                   # 先 shadow 观察审计，标定阈值后再切 enforce
```

| 变量 | 默认 | 说明 |
|---|---|---|
| `JEV_MODE` | `shadow` | `shadow` = 只判只落审计；`enforce` = 灰区自动放行生效 |
| `JEV_SAFE_THRESHOLD` | `0.6` | 自动放行阈值（概率与置信度双轴全过才放行），按 shadow 期分歧样本标定 |
| `JEV_TIMEOUT_SECONDS` | `5.0` | 判决硬超时（秒），超时即回落人工 |
| `JEV_MODEL` | `jev-1.13.0` | 锁定具体版本号，不用 `jev-latest` 别名（防行为漂移） |
| `JEV_BASE_URL` | `https://api.typesafe.ai` | TypeSafe REST 端点根 |

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
│   │   ├── api/              # 路由：agents·sessions·stream·models·mcp·skills·uploads·knowledge·workspace·voice·cron
│   │   ├── services/         # 业务：模型·会话·MCP·存储·RAG(向量化/向量库/入库池)·STT·JEV 客户端…
│   │   ├── core/             # deepagents 内核（图构建 / 用户隔离文件·shell 后端 / HITL / JEV 门控 / 变更追踪）
│   │   ├── models/           # 持久化：8 张业务表 DDL+CRUD + pydantic 边界模型
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
├── docker-compose.yml        # 一键全栈编排（6 服务）
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

<a id="faq"></a>

## 常见问题 FAQ

<details>
<summary><b>知识库页 404 / 503？</b></summary>

`KB_ENABLED=false` 或 ES 未就绪。检查：`docker compose ps` 看 elasticsearch 是否 healthy；`curl http://localhost:8000/health` 看 `capabilities.kb_enabled`；本地开发确认 `.env` 中 `ES_URL` 可达。
</details>

<details>
<summary><b>知识库入库慢 / 嵌入超时？</b></summary>

`EMBEDDING_BATCH_SIZE` 必须对齐 TEI 的 `--max-batch-size`（默认 4）。客户端 batch 大于它时单请求内多轮前向推理，延迟成倍放大（实测 batch=24 → 53s vs batch=4 → 6.5s）。同时确认 `EMBEDDING_TIMEOUT` 大于单批实际耗时。
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

先用 `JEV_MODE=shadow` 运行一段时间，按审计表中「建议放行 vs 人工实际同意」的分歧样本重标定 `JEV_SAFE_THRESHOLD`，再切 `enforce`。任何时刻 Jev 不可用都会 fail-closed 回落全人工审批。
</details>

<details>
<summary><b>如何彻底重置数据？</b></summary>

`docker compose down -v` 清空全部命名卷（业务数据 + 附件 + 知识库 + workspace），之后 `up -d` 重建。
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
