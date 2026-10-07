# goduck-platform

![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![React](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=black)
![LangGraph](https://img.shields.io/badge/LangGraph-agent-1C3C3C)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)

**个人智能体搭建 / 编排 / 会话平台** —— 基于 deepagents + LangGraph + MCP，开箱即用的全栈开源项目。

> 定位：让个人用户搭建自己的智能体。支持新增 / 更新（更新即替换）、随时增删工具与技能，
> 重启自动加载已建智能体。同一会话内可灵活切换智能体（`last_agent_id` 记录最近使用，
> HITL 悬空期锁定不可切换）；更新智能体不再影响既有会话，删除智能体会话与其
> checkpoint 保留，配置历史 append-only 保留可还原。

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
- [目录结构](#structure)
- [测试](#testing)
- [常见问题 FAQ](#faq)
- [详细文档](#docs)

<a id="overview"></a>

## 功能总览

| 能力 | 说明 | 界面入口 |
|---|---|---|
| 智能体编排 | AgentConfig 树形编辑、实时校验、Mermaid 委派拓扑预览、配置历史与一键还原、YAML 导出 | `/agents` |
| 流式会话 | SSE 逐 token 输出、HITL（人工审批 / 编辑 / 拒绝 / 追加回复）、TODO 计划面板、回合时间线 | `/sessions` |
| 附件多模态 | 图片 / 文档上传（MinIO 持久），视觉模型识图、文档解析 | 会话输入框 📎 |
| 知识库 RAG | 建库 → pdf/docx 入库向量化（ES）→ 会话中锚定知识库检索作答 | `/settings/knowledge` |
| 语音输入 | 长按说话，实时转写直显输入框，松开自动触发会话回合（AssemblyAI Streaming STT） | 会话输入框 🎤 |
| 工作区 | 每用户隔离的文件树、文本预览 / office 转 Markdown / 下载 / 删除、文件变更徽标（`+3 −2`） | 会话页右侧面板 |
| 模型管理 | OpenAI 兼容多供应商注册（通吃 OpenAI / DeepSeek / Qwen / 本地），保存前连通性测试、密钥轮换 | `/settings/models` |
| MCP 工具 | stdio / http MCP server 运行时注册，工具按 `{server}__{tool}` 命名空间化防冲突，健康检查 | `/settings/mcp` |
| 技能池 | 跨智能体共享的 `/skills/` 目录，multipart 上传，内置 6 个技能种子 | `/settings/skills` |
| 运行时依赖补齐 | 开启 `allow_shell` 的智能体可在会话中用 `uv / pip / npm` 即时装包补齐 MCP 前置依赖（每条命令人工审批）⚠️ 详见 [backend/README.md](./backend/README.md) | 会话内 |

<a id="architecture"></a>

## 架构

分层模块化单体，请求链路：`API 层 → Service 层 → Runtime(deepagents) → Persistence → Infra`。

```
 浏览器
   │  HTTP / SSE / WebSocket
   ▼
 Nginx 边缘网关（容器部署时唯一对外入口 :80；本地开发由 Vite proxy 承担同款职责）
   ├── /          → Frontend（React 19 SPA，nginx 静态服务）
   └── /api/*     → Backend（FastAPI + LangGraph + deepagents）
                      ├── PostgreSQL 16   业务表(8张) + LangGraph checkpoints/store（共享连接池）
                      ├── MinIO           附件与知识库原文字节持久层
                      ├── Elasticsearch   知识库向量 / 全文检索
                      ├── TEI             向量化（OpenAI 兼容 /v1/embeddings，可换任意兼容端点）
                      ├── MCP servers     stdio / http 工具生态（含本仓库 mcp-servers 网关）
                      └── AssemblyAI      语音输入 Streaming STT（可选）
```

关键设计：

- **数据驱动**：模型（LLM + API Key）与 MCP server 均存于 DB 表，通过 API 运行时注册，**不在环境变量里**；重启自动加载。
- **能力开关**：`/health` 下发 `capabilities`（kb_enabled / voice_enabled），前端据此驱动菜单与入口可见性——未启用的能力整个 UI 无痕隐藏。
- **无版本化（方案 B）**：智能体 `id` 全程稳定；更新 = in-place 覆写配置 + 追加历史快照 + 级联删旧会话。

<a id="tech-stack"></a>

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | FastAPI · LangGraph · deepagents · langchain-openai · psycopg3 · MinIO · Elasticsearch · pydantic-settings |
| 前端 | React 19 · TypeScript · Vite · Ant Design · Zustand · TanStack Query · Mermaid · @xyflow/react · react-hook-form |
| MCP 生态 | fastmcp · langchain-mcp-adapters · uv/uvx · node/npx（镜像内预热 MCP 包缓存） |
| 基础设施 | PostgreSQL 16 · MinIO · Elasticsearch 8 · Nginx（docker-compose 一键编排 6 服务） |

<a id="quickstart"></a>

## 快速开始

<a id="deploy-docker"></a>

### 方式 A：Docker 一体化部署（推荐，一条命令起全栈）

适用于服务器 / NAS / 内网环境长期运行。前置要求：**Docker + Docker Compose v2**。

```bash
# 1)（可选）自定义配置：在仓库根目录创建 .env 覆盖默认值
#    必改项只有一个：EMBEDDING_BASE_URL（默认是占位地址 192.168.x.x，见下方"配置速查"）
vi .env

# 2) 构建并启动全部 6 个服务（postgres / minio / elasticsearch / backend / frontend / nginx）
docker compose up -d --build

# 3) 观察启动状态
docker compose ps
docker compose logs -f backend        # 看到 "goduck-platform started" 即就绪
```

启动完成：

| 访问目标 | 地址 |
|---|---|
| **平台入口（唯一对外）** | `http://<服务器IP>/`（端口由 `EDGE_PORT` 控制，默认 80） |
| 后端 API 文档（容器内） | `http://<服务器IP>/api/docs` |
| MinIO 控制台（调试） | `http://<服务器IP>:9001`（`minioadmin / minioadmin123`） |
| Elasticsearch（调试） | `http://<服务器IP>:9200` |

> **说明**
> - 仅 nginx 对外发布端口；backend / frontend 走内部网络，postgres / minio / es 的宿主端口仅供本机调试，生产建议删除对应 `ports` 映射。
> - 数据全部落在命名卷：`goduck_pgdata` / `goduck_miniodata` / `goduck_esdata` / `goduck_backend_data`（workspace / memories / skills），重打镜像不丢数据。
> - 嵌入服务（TEI）默认不在 compose 内：把 `EMBEDDING_BASE_URL` 指向局域网内任意 OpenAI 兼容嵌入端点即可；跨机部署可用 `extra_hosts` 把服务名映射到 GPU 机器（compose 内有注释示例）。
> - 不需要知识库？在 `.env` 里设 `KB_ENABLED=false`，可完全不依赖 TEI / ES。

常用运维命令：

```bash
docker compose logs -f nginx            # 排查网关
docker compose restart backend          # 重启后端（自动重载 DB 中的模型/MCP/智能体）
docker compose up -d --build backend    # 改了后端代码后重建单个服务
docker compose down                     # 停止（保留数据卷）
docker compose down -v                  # 停止并清空全部数据（谨慎）
```

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

<a id="deploy-local"></a>

### 方式 B：本地开发运行

前置要求：**Docker**（起基础设施）、**Python ≥ 3.10**、**Node ≥ 20**。以下为 PowerShell 写法（Linux/macOS 自行替换 `copy`→`cp`）。

#### 1. 启动基础设施（PostgreSQL + MinIO + Elasticsearch）

```powershell
# 只起基础设施三个服务（不需要 build backend/frontend）
docker compose up -d postgres minio elasticsearch
# PostgreSQL -> localhost:5433   MinIO -> localhost:9000 / 控制台 9001   ES -> localhost:9200
```

#### 2. 配置并启动后端

```powershell
cd backend
copy .env.example .env      # 默认值已与 docker-compose 对齐
pip install -e .            # 安装依赖
python -m app.main          # 或 uvicorn app.main:app --reload
# 看到 "goduck-platform started" 即成功；健康检查 http://localhost:8000/health
```

> - 所有业务表 + LangGraph 原生表均由后端启动时自动建表，**无需手动跑迁移**。
> - 知识库需要可达的向量化端点（`EMBEDDING_BASE_URL`）；没有的话在 `.env` 设 `KB_ENABLED=false`，其余功能不受影响。
> - 语音输入需在 `.env` 设 `VOICE_ENABLED=true` 并填 `ASSEMBLYAI_API_KEY`。

#### 3. 启动前端

```powershell
cd frontend
npm install
npm run dev                 # http://localhost:5173 （/api 自动代理到 8000，含 WS）
```

#### 4.（可选）启动 mcp-servers 网关（附件解析 + kb_search 检索）

```powershell
cd mcp-servers
copy .env.example .env      # 填 MinIO 凭证、智谱 key（与 backend 的 EMBEDDING/ES 配置保持一致）
pip install -r requirements.txt
python main.py              # 监听 0.0.0.0:8088/mcp
```

然后在界面 `/settings/mcp` 注册（或 curl）：

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

界面配置有依赖顺序，按下面 1→4 操作：

```
① 注册模型 ──▶ ② 创建智能体 ──▶ ③（可选）MCP / 技能 / 知识库 ──▶ ④ 开聊
```

1. **注册模型**：`/settings/models` → 注册模型 → 填 `model_type` / `api_key` / `base_url`（可先点「测试连接」，api_key 永不回显）。
   任何 OpenAI 兼容端点均可：OpenAI / DeepSeek / Qwen（DashScope compatible-mode）/ Ollama / vLLM 等。
2. **创建智能体**：`/agents/new` → 树形编辑 AgentConfig（主智能体 + 子智能体委派）→「绑定模型」下拉选择 ① 注册的模型 → 保存（右侧实时 Mermaid 拓扑预览）。
   > 智能体 `model_id` 必填，因此**模型必须先于智能体注册**。
3. **（可选）扩充能力**：
   - `/settings/mcp`：注册 MCP server（内置网关 `UnifiedGateway` 提供系统状态等工具；`mcp-servers/` 网关提供附件解析与 `kb_search`），注册后可在智能体 `tool_names` 中引用 `{server}__{tool}`。
   - `/settings/skills`：上传技能（一个技能 = 一个目录，含 SKILL.md），智能体以 `/skills/<name>/` 引用。
   - `/settings/knowledge`：新建知识库 → 上传 pdf/docx → 等待入库完成（后台向量化）。
4. **会话测试**：`/sessions` → 基于智能体创建会话 → 发消息体验：
   - SSE 流式输出 + TODO 计划面板 + 回合时间线；
   - 挂附件（图片走视觉模型，文档自动解析）；
   - 锚定知识库（输入框左侧 📚 选择库，agent 会先检索后作答）；
   - 长按 🎤 说话（启用语音时）；
   - 右侧工作区面板浏览 / 下载 / 删除 agent 产物；
   - 子智能体工具配置了 `interrupt_on` 时出现 HITL 审批卡片。

<a id="config"></a>

## 配置速查

配置全部走环境变量 / `.env`，**禁止硬编码**。完整清单见 [`backend/.env.example`](./backend/.env.example)（逐项带中文注释）。高频项：

### 根目录 `.env`（docker compose 层，覆盖 `docker-compose.yml` 内 `${VAR:-默认值}`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `EDGE_PORT` | `80` | nginx 唯一对外端口 |
| `POSTGRES_USER / PASSWORD / DB / PORT` | `goduck / changeme / langgraph_db / 5433` | PostgreSQL 凭据与宿主端口 |
| `MINIO_ROOT_USER / PASSWORD` | `minioadmin / minioadmin123` | MinIO 凭据 |
| `EMBEDDING_BASE_URL` | `http://192.168.x.x:8082/v1` | **必改**：OpenAI 兼容嵌入端点（TEI 等） |
| `EMBEDDING_DIMS` | `512` | 全局向量维度，backend 与 mcp-servers **必须一致** |
| `EMBEDDING_BATCH_SIZE` | `4` | 须对齐 TEI `--max-batch-size`，否则入库易超时 |
| `KB_ENABLED` | `true` | 知识库总开关 |
| `VOICE_ENABLED` / `ASSEMBLYAI_API_KEY` | `false` / 空 | 语音输入开关与 AssemblyAI key |
| `LOG_LEVEL` | `INFO` | 后端日志级别 |

### backend/.env（本地开发时与 compose 层同名变量等价）

| 变量 | 默认（本地） | 说明 |
|---|---|---|
| `DATABASE_URL` | `postgresql://goduck:changeme@localhost:5433/langgraph_db` | 容器内自动改为 `postgres:5432` |
| `MINIO_ENDPOINT` | `localhost:9000` | 容器内为 `minio:9000` |
| `ES_URL` | `http://localhost:9200` | 容器内为 `http://elasticsearch:9200` |
| `LOCAL_ROOT` | `data` | workspace/memories/skills 三目录的根（容器内 `/data` 命名卷） |
| `ROOT_PATH` | 空 | 经 nginx 剥 `/api` 时设为 `/api`（修正 docs 链接；compose 已配好） |
| `MCP_CONNECT_TIMEOUT` | `45` | 单个 MCP server 连接超时（防 uvx 拉包重试拖死接口） |
| `ATTACHMENT_MAX_SIZE_MB` | `10` | 单附件大小上限 |

### mcp-servers/.env

MinIO / ES / EMBEDDING 系列须与 backend 侧**严格一致**（尤其 `EMBEDDING_DIMS`，维度不对称会导致检索失败）。详见 [mcp-servers/README.md](./mcp-servers/README.md)。

<a id="capabilities"></a>

## 能力开关

`/health` 返回 `capabilities`，前端据此零配置感知全局能力（单一真相源 = 后端 settings）：

| 开关 | 默认 | 关闭时的表现 |
|---|---|---|
| `KB_ENABLED` | `true` | `/knowledge-bases` 路由不挂载（404 / 503），前端隐藏知识库菜单与会话 KB 入口 |
| `VOICE_ENABLED` | `false` | 不挂载 `/voice` WS 路由，无麦克风入口；置 `true` 必须同时配 `ASSEMBLYAI_API_KEY`，否则降级为不挂载 + 启动警告 |

```powershell
curl http://localhost:8000/health
# {"status":"ok","capabilities":{"kb_enabled":true,"voice_enabled":false}}
```

<a id="structure"></a>

## 目录结构

```
goduck-platform/
├── backend/                  # FastAPI + LangGraph 后端（详见 backend/README.md）
│   ├── app/
│   │   ├── api/              # 路由：agents·sessions·stream·models·mcp·skills·uploads·knowledge·workspace·voice
│   │   ├── services/         # 业务：模型·会话·MCP·存储·RAG(向量化/向量库/入库池)·STT·内容组装…
│   │   ├── core/             # deepagents 内核（图构建 / 用户隔离文件·shell 后端 / HITL / 变更追踪）
│   │   ├── models/           # 持久化：8 张业务表 DDL+CRUD + pydantic 边界模型
│   │   ├── streaming/        # SSE 事件契约
│   │   └── mcps/             # 内置 UnifiedGateway MCP 网关
│   ├── agent_confs/          # 种子智能体 YAML
│   ├── data/skills/          # 技能池种子（6 个内置技能，随镜像烘焙进数据卷）
│   ├── scripts/              # KB 批量导入 / RAG 冒烟测试
│   ├── tests/                # 纯单测（无需 DB / MCP）
│   └── Dockerfile            # 含 uv/uvx/node + MCP 包缓存预热
├── frontend/                 # React 19 前端（详见 frontend/README.md）
│   ├── src/api/              # 13 个 API 域客户端
│   ├── src/features/         # agent-editor / chat / knowledge / topology / workspace
│   ├── src/pages/            # agents / sessions / settings(models·mcp·skills·knowledge)
│   └── Dockerfile            # 多阶段构建 → nginx 静态服务
├── mcp-servers/              # 本地 MCP 网关集群：附件解析(image/docx) + kb_search 检索
├── nginx/                    # 边缘反代配置（SSE 关缓冲 + WebSocket 透传）
├── docker-compose.yml        # 一键全栈编排（6 服务）
├── demo/                     # 早期原型存档（不参与部署，仅供参考）
└── README.md
```

<a id="testing"></a>

## 测试

```powershell
# 后端（纯单测：STT 协议解析 / 文件变更追踪，无需 DB / MCP）
cd backend
python -m pytest tests/ -q

# 代码风格
ruff check app/

# 前端
cd frontend
npm run typecheck            # tsc 类型检查
npm run lint                 # eslint
```

<a id="faq"></a>

## 常见问题 FAQ

<details>
<summary><b>80 端口被占用 / 想换端口？</b></summary>

根目录 `.env` 设置 `EDGE_PORT=8080` 后 `docker compose up -d`，访问 `http://<IP>:8080`。
</details>

<details>
<summary><b>知识库页 404 / 503？</b></summary>

`KB_ENABLED=false` 或 ES 未就绪。检查：`docker compose ps` 看 elasticsearch 是否 healthy；
`curl http://localhost:8000/health` 看 `capabilities.kb_enabled`；本地开发确认 `.env` 中 `ES_URL` 可达。
</details>

<details>
<summary><b>知识库入库慢 / 嵌入超时？</b></summary>

`EMBEDDING_BATCH_SIZE` 必须对齐 TEI 的 `--max-batch-size`（默认 4）。客户端 batch 大于它时单请求内
多轮前向推理，延迟成倍放大（实测 batch=24 → 53s vs batch=4 → 6.5s）。同时确认 `EMBEDDING_TIMEOUT`
大于单批实际耗时。详见 `backend/.env.example` 注释。
</details>

<details>
<summary><b>会话里没有麦克风图标？</b></summary>

语音默认关闭。`.env` 设 `VOICE_ENABLED=true` 且填入 `ASSEMBLYAI_API_KEY` 后重启后端。
nginx / vite 代理均已配置 WS 透传，无需额外操作。
</details>

<details>
<summary><b>MCP 注册后工具列表不出现？</b></summary>

界面点「刷新」或 `POST /mcp/refresh`。stdio server（uvx/npx）首次启动需拉包：内网环境可在
server 配置的 `env` 里加 `UV_DEFAULT_INDEX` 指向 pypi 镜像；连接超时上限由 `MCP_CONNECT_TIMEOUT` 控制。
</details>

<details>
<summary><b>如何彻底重置数据？</b></summary>

`docker compose down -v` 清空全部命名卷（业务数据 + 附件 + 知识库 + workspace），之后 `up -d` 重建。
</details>

<details>
<summary><b>Windows 本地开发注意？</b></summary>

命令示例均为 PowerShell（`copy`、反引号续行）。`python -m app.main` 自带 `--reload`；
路径配置（`LOCAL_ROOT`）用相对值 `data` 即可，后端会锚定到 `backend/` 目录，与启动 CWD 无关。
</details>

<a id="docs"></a>

## 详细文档

| 文档 | 内容 |
|---|---|
| [backend/README.md](./backend/README.md) | 后端架构、目录、数据模型（8 张表）、API 速览、allow_shell 运行时补依赖、文件变更徽标 |
| [frontend/README.md](./frontend/README.md) | 前端技术栈、功能、脚本、目录结构、开发约定 |
| [mcp-servers/README.md](./mcp-servers/README.md) | MCP 网关集群：附件解析 wrapper、kb_search、配置对齐要求 |
| [frontend/DESIGN.md](./frontend/DESIGN.md) | 前端设计准则与页面结构设计 |

## 贡献与许可

开源开放项目，欢迎 Issue / PR 共建。各模块技术细节见上表文档；参与开发前建议先阅读对应模块 README。
