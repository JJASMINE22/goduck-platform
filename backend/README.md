<p align="center">
  <img src="../logo.png" alt="goduck-platform logo" width="150">
</p>

<h1 align="center">goduck-platform backend</h1>

<p align="center">个人智能体搭建 / 迭代 / 会话工具的后端服务 —— 基于 <b>FastAPI + deepagents + LangGraph + MCP</b></p>

> 完整本地 / 容器启动指引见[根 README](../README.md#quickstart)，本文聚焦后端内部设计与 API。

## 目录

- [架构](#architecture) · [数据模型](#data-model) · [关键设计](#key-design)
- [运行](#run) · [API 速览](#api)
- [技能目录约定](#skills) · [运行时依赖补齐](#jit-install)
- [工作区文件变更徽标](#file-change) · [代码风格](#quality) · [目录来源](#origin)

<a id="architecture"></a>

## 架构

分层模块化单体。请求链路：`API 层 → Service 层 → Runtime/Persistence → Infra`。

```
backend/
├── app/
│   ├── main.py              # FastAPI 入口 + lifespan 装配（KB/语音按开关挂载）+ 启动预热
│   ├── config.py            # pydantic-settings 集中配置（含 workspace/memories/skills 派生路径）
│   ├── deps.py              # 依赖注入：AppState + SimpleUser + FastAPI 访问器
│   ├── seed.py              # CLI：YAML 模板 → 灌入智能体
│   │
│   ├── api/                 # 【API 层】FastAPI 路由 + 边界校验
│   │   ├── agents.py        #   /agents           智能体 CRUD + 校验 + 拓扑预览 + 历史还原 + 导出
│   │   ├── sessions.py      #   /sessions         会话 CRUD + 历史
│   │   ├── stream.py        #   /sessions/*       SSE 流式对话 + HITL resume + 附件/KB/shell 能力查询
│   │   ├── uploads.py       #   /sessions/*       附件上传（魔数判 mime）+ 附件能力
│   │   ├── workspace.py     #   /sessions/*       工作区树/预览/下载/删除（用户隔离）
│   │   ├── voice.py         #   WS /sessions/*    语音输入全管线（voice_enabled 时挂载）
│   │   ├── models.py        #   /models           模型注册 CRUD + 连通性测试
│   │   ├── mcp.py           #   /mcp              MCP server 注册 + 工具列表 + 健康检查
│   │   ├── skills.py        #   /skills           技能上传/列/删
│   │   ├── knowledge.py     #   /knowledge-bases  知识库 CRUD + 文档入库 + 检索（kb_enabled 时挂载）
│   │   └── cron.py          #   /cron             定时任务 CRUD + 立即触发 + 运行历史
│   │
│   ├── services/            # 【Service 层】业务逻辑（不做边界类型重复校验）
│   │   ├── agent_service.py #   校验/CRUD/mermaid 渲染
│   │   ├── agent_registry.py#   按 agent_id 缓存编译图（LRU+TTL）
│   │   ├── session_service.py#  会话生命周期 + checkpointer 历史
│   │   ├── model_service.py / model_store.py     # 模型 CRUD + 缓存 chat 客户端（key 轮换即时生效）
│   │   ├── mcp_lifecycle.py #   数据驱动 MCP 连接池 + 工具名命名空间
│   │   ├── stream_adapter.py#   astream → SSE 事件
│   │   ├── hitl_resolver.py #   interrupt 校验 + 决策归一
│   │   ├── thinking_patch.py#   思考流事件补丁
│   │   ├── storage.py       #   MinIO 附件字节持久层
│   │   ├── content_composer.py / message_content.py / mime_utils.py
│   │   │                    #   附件 → HumanMessage.content 组装（多模态）；KB 锚点以用户提示注入前置
│   │   ├── knowledge_service.py / embedding.py / vector_store.py / ingest_pool.py
│   │   │                    #   知识库：入库/检索、TEI 向量化、ES 向量库、后台入库池
│   │   ├── docling_parser.py#   docling 结构化解析 + OCR（KB_PARSER_ENGINE=docling）
│   │   ├── cron_service.py / cron_scheduler.py  # 定时任务 CRUD + 周期调度 + 失败熔断
│   │   ├── run_manager.py / run_source.py       # 会话/定时统一回合运行管理
│   │   ├── jev_client.py    #   TypeSafe Jev REST 客户端（概率判决）
│   │   ├── attachment_policy.py / vision_describer.py / tool_content.py
│   │   │                    #   附件能力判定 / 视觉模型识图 / 工具消息内容组装
│   │   ├── doc_convert.py   #   office 文档 → Markdown 预览（markitdown）
│   │   └── stt/assemblyai_stt.py                # AssemblyAI Streaming STT 客户端
│   │
│   ├── core/                # 【Runtime 层】deepagents 内核（迁移自 DeepAgentsDemos）
│   │   ├── schema_utils.py  #   AgentConfig/RubricConfig pydantic + YAML/JSON loader
│   │   ├── graph_utils.py   #   create_agent_recursive（compiled+dicted 混合）
│   │   ├── core_utils.py    #   middleware + _build_backend（memory/workspace/skills 条件挂载）
│   │   ├── topology_utils.py#   委派拓扑 Mermaid 渲染（build_delegation_mermaid）
│   │   ├── hitl_utils.py    #   CLI HITL 决策收集（dev 用）
│   │   ├── user_scoped_backend.py / user_scoped_shell_backend.py
│   │   │                    #   按用户隔离的虚拟文件系统 / shell 后端（u_<hash>/ 隔离）
│   │   ├── jev_gate.py / jev_kb_gate.py         # JEV shell 命令门控 / KB 检索降噪门控
│   │   └── file_change_tracker.py               # 工作区文件变更行级统计（difflib + contextvar）
│   │
│   ├── streaming/events.py  # SSE 事件契约（后端与前端共同的事件模型）
│   ├── models/              # 【Persistence 层】
│   │   ├── db.py            #   psycopg3 直连：8 张业务表 DDL + CRUD
│   │   └── schemas.py       #   pydantic 请求/响应模型（边界校验）
│   ├── tools/rubric_tools.py#   rubric grader 内部取证工具（仅挂 RubricMiddleware）
│   └── mcps/                #   内置 UnifiedGateway 网关 + mcp_services/ + stdio_guard 守卫
│
├── agent_confs/default.yaml # 种子智能体（网关 + researcher/analyzer/artist 子智能体）
├── data/skills/             # 技能池种子（本地目录，见“技能目录约定”；镜像构建时烘焙进 /data/skills）
├── Dockerfile               # uv/uvx/node + MCP 包缓存预热 + 非 root + tini
└── requirements.txt / pyproject.toml
```

**持久化**：业务表（psycopg3 直连）与 LangGraph 原生表（`AsyncPostgresSaver` /
`AsyncPostgresStore`）共享同一 `AsyncConnectionPool`。

迁移原则：**不改 deepagents 调用方式**（`create_deep_agent` 签名稳定），只在外围
加服务层 / 协议层 / 持久化层。所有依赖走 `deps.py` 注入，无全局单例。

<a id="data-model"></a>

## 数据模型

业务表（8 张，psycopg3 直连，与 LangGraph 原生表共享同一连接池，启动时幂等建表）：

| 表 | 作用 | 关键约束 |
|---|---|---|
| `model` | 注册模型（OpenAI 兼容客户端族 + api_key + base_url + 采样参数）。agent 经 `model_id` 引用。 | `name` 唯一；key 永不回显；被引用时禁止删除 |
| `agent` | 智能体当前 active 配置（单行可变）。`id` 全程稳定不变，更新即 in-place 覆写 `config_json`。 | `name` 唯一；`status` ∈ draft/active/archived |
| `agent_config_history` | 配置历史快照（append-only 审计 + 手动还原来源）。更新时不删，仅追加。 | `agent_id` 无 FK：agent 删除后快照保留为审计线索 |
| `session` | 会话。`last_agent_id` 记录最近使用的智能体（send 时写入，非绑定）；`thread_id` 是 LangGraph checkpointer 的键。 | `thread_id` 唯一；`last_agent_id` ON DELETE SET NULL（会话比 agent 长寿）；索引 `user_id` |
| `mcp_server` | MCP 服务注册（stdio/http）。`name` 作为工具名前缀。 | `name` 唯一；`config_json` 存 `{command,args,env}` 或 `{url,headers}` |
| `upload` | 附件元数据（字节在 MinIO）。 | 关联 session/user |
| `knowledge_base` | 知识库（ES 索引 `kb_<id>` + 可选 MinIO 原文留底）。 | `name` 唯一 |
| `kb_document` | 知识库文档（状态机 pending/ingesting/ready/failed + 分块数/页数）。 | 级联随 KB 删除 |

两类"历史"区分（会话-智能体解耦后）：**会话历史（session + LangGraph checkpoints）
= 与智能体更新/删除解耦，全程保留**；**配置历史（agent_config_history）= 保留可还原**。

> LangGraph 原生表（`checkpoints` / `checkpoint_writes` / `checkpoint_blobs` / store）
> 由 `AsyncPostgresSaver.setup()` / `AsyncPostgresStore.setup()` 创建，仅随 session
> purge 清理（不再随智能体更新/删除清理）。

<a id="key-design"></a>

## 关键设计

- **无版本化 + 会话-智能体解耦**：`agent.id` 全程稳定不变；更新 = in-place 覆写
  `config_json` + 追加 `agent_config_history` 快照。同一会话可逐回合切换智能体
  （上下文状态 / 回撤 / 中止 / HITL 均以 thread 为锚，不受切换影响）：
  `POST /messages` 等写者带 `agent_id`（缺省回落 `session.last_agent_id`），send
  时写入 last；withdraw / 能力端点 / `GET /interrupt` 走弱绑定解析（显式 >
  last）；`POST /resume` 恒经 last（HITL 悬空期门禁禁止切换/发新消息，构造性
  保证续跑在中断的那张图上）。删除 agent：chat 会话存活（last SET NULL，
  进入后选择新智能体即可继续/回撤），cron 专用会话归档保留产出。
  重启加载所有 `active` 智能体重建 registry。
- **数据驱动运行时**：模型（`model` 表）与 MCP server（`mcp_server` 表）全部经 API 注册、
  存 DB、重启自动加载；模型变更即时刷新 ModelStore 并使 agent 缓存失效（无需重启）。
- **能力开关**：`kb_enabled=false` 时不 import RAG 重依赖、不挂 knowledge 路由；
  `voice_enabled=true` 且 key 齐备才挂 voice WS 路由。`/health.capabilities` 是前端可见性的单一真相源。
- **工具全 MCP 化**：本地工具脚本移除；HITL 走声明式 `interrupt_on`（与 MCP 工具兼容）。
  工具按 `{server}__{tool}` 命名防跨 server 冲突。
- **拓扑可视化**：后端以 `AgentConfig` 为唯一数据源生成 Mermaid 文本（`build_delegation_mermaid`），
  前端 mermaid.js 渲染。创建/校验/更新/详情各接口附带 `mermaid` 字段，零图像/网络依赖。
- **用户隔离虚拟文件系统**：`/workspace/` `/memories/` `/skills/` 由
  `UserScopedFilesystemBackend` 落盘到 `workspace/u_<hash>/`（按 user_id 哈希隔离），
  工作区浏览 API 与写入同源 sanitize（写在哪 = 读在哪）。
- **HITL 统一**：`interrupt_on` 在 SSE/HTTP 层统一为 `event: interrupt`；SSE 异常兜底转 `event: error`。
  语音 WS 下行隧道与 SSE 事件同名同构（前端复用 dispatch）。

<a id="run"></a>

## 运行

详见[根 README](../README.md#quickstart)。要点：

```powershell
# 基础设施（只起三个 infra 服务，不 build 应用）
docker compose up -d postgres minio elasticsearch

# 后端
cd backend
copy .env.example .env      # 默认值已与 compose 对齐；KB/语音按需开启
pip install -e .
python -m app.main          # 或 uvicorn app.main:app --reload
```

- LLM 与 MCP server 均不在 `.env` 里 —— 模型通过 `/models`、MCP 通过 `/mcp/servers` 运行时注册。
- 内置 UnifiedGateway 网关（可选）：`python -m app.mcps.mcp_server`（127.0.0.1:8088/mcp）。
- 种子智能体（可选）：`python -m app.seed agent_confs/default.yaml --name "Default Gateway" --creator admin`。

<a id="api"></a>

## API 速览

接口按模块分组。SSE 流终止事件固定为 `done` / `interrupt` / `error`，前端可据此判定最终状态。
容器部署时经 nginx 剥 `/api` 前缀（即 `/api/agents` → `/agents`）。

### 智能体管理 `/agents`

| Method | Path | 说明 |
|--------|------|------|
| POST | `/agents/validate` | 校验 config + 返回 mermaid 预览（不落库） |
| POST | `/agents` | 创建（name 唯一，409 冲突）→ 建 registry → 返回 mermaid |
| PUT | `/agents/{id}` | 更新（必须存在）→ 重建 registry（会话不归档，续用新配置） |
| GET | `/agents` | 列出智能体 |
| GET | `/agents/{id}` | 详情 + 当前 active mermaid |
| GET | `/agents/{id}/topology.mmd` | Mermaid 文本（PlainTextResponse） |
| GET | `/agents/{id}/history` | 配置历史（还原选择） |
| POST | `/agents/{id}/restore` | 用 `{history_id}` 还原 |
| GET | `/agents/{id}/export.yaml` | 导出当前配置 YAML |
| DELETE | `/agents/{id}` | 归档（status=archived） |

### 会话与流式 `/sessions`

| Method | Path | 说明 |
|--------|------|------|
| POST | `/sessions` | 创建会话（agent_id 可选：缺省 = 空会话，进入后选择） |
| GET | `/sessions?user_id=` | 列出用户会话（含最近使用 agent_name） |
| GET | `/sessions/{id}` | 会话详情 |
| DELETE | `/sessions/{id}` | 归档会话 |
| GET | `/sessions/{id}/history` | checkpointer 历史消息 |
| POST | `/sessions/{id}/messages` | 发送消息（可带 agent_id/附件/KB 锚点）→ SSE 流；发送成功写 last_agent_id；悬空中断存在时 409 |
| POST | `/sessions/{id}/resume` | HITL 决策恢复 → SSE 流（恒经 last_agent_id 取图，不收 agent_id） |
| GET | `/sessions/{id}/interrupt` | 待审批查询（?agent_id= 可选，缺省回落 last） |
| POST | `/sessions/{id}/withdraw` | 回撤回合（body.agent_id 可选：弱绑定取图，last 悬空时选中即可回撤） |
| GET | `/sessions/{id}/attachment-capability` | 附件能力（?agent_id= 可选；解析落空全关降级） |
| GET | `/sessions/{id}/knowledge-capability` | 知识库能力（?agent_id= 可选） |
| GET | `/sessions/{id}/shell-capability` | shell 能力（?agent_id= 可选） |
| GET | `/sessions/{id}/workspace-capability` | 工作区能力（?agent_id= 可选） |
| WS | `/sessions/{id}/voice` | 语音输入全管线（start 帧可带 agent_id；audio/flush ⇄ stt_*/agent 事件） |

### 附件 `/sessions/{id}/uploads`

| Method | Path | 说明 |
|--------|------|------|
| POST | `/sessions/{id}/uploads` | multipart 上传（服务端魔数判定 mime，存 MinIO + DB） |

### 工作区 `/sessions/{id}/workspace*`

| Method | Path | 说明 |
|--------|------|------|
| GET | `/sessions/{id}/workspace-capability` | 是否启用工作区（驱动面板可见性） |
| GET | `/sessions/{id}/workspace/tree` | 递归文件树（≤500 条目 / ≤8 层） |
| GET | `/sessions/{id}/workspace/file` | 读文本文件（256KB 截断；二进制仅元信息） |
| GET | `/sessions/{id}/workspace/download` | 下载单文件（RFC5987 中文文件名）或目录 zip |
| GET | `/sessions/{id}/workspace/preview` | office 文档 → Markdown 预览 |
| DELETE | `/sessions/{id}/workspace` | 删除文件或目录（用户裁剪，递归删） |

### 模型管理 `/models`

| Method | Path | 说明 |
|--------|------|------|
| GET | `/models` | 列出注册模型（api_key 永不回显） |
| POST | `/models` | 注册（name 唯一）→ 即时刷新 ModelStore |
| PUT | `/models/{id}` | 更新（key 留空 = 保留旧 key 轮换）→ 失效引用 agent 缓存 |
| DELETE | `/models/{id}` | 删除（仍被 agent 引用时 409 阻断） |
| POST | `/models/test` | 连通性测试（不落库，保存前探测） |

### MCP 运维 `/mcp`

| Method | Path | 说明 |
|--------|------|------|
| GET | `/mcp/servers` | 列出已注册 MCP server |
| POST | `/mcp/servers` | 注册/更新（触发 refresh） |
| PUT | `/mcp/servers/{id}` | 改 config/enabled |
| DELETE | `/mcp/servers/{id}` | 删除（触发 refresh） |
| GET | `/mcp/tools` | 全量 namespaced 工具（下拉多选用） |
| GET | `/mcp/status` | 连接健康 + 工具数 |
| POST | `/mcp/refresh` | 强制重连 |

### 技能管理 `/skills`

| Method | Path | 说明 |
|--------|------|------|
| GET | `/skills` | 列出技能（子目录 + 文件数） |
| POST | `/skills` | multipart 上传技能（name + 文件，防目录穿越） |
| DELETE | `/skills/{name}` | 删除技能目录 |

### 知识库 `/knowledge-bases`（kb_enabled 时挂载）

| Method | Path | 说明 |
|--------|------|------|
| GET/POST | `/knowledge-bases` | 列出 / 创建（name 唯一） |
| PUT/DELETE | `/knowledge-bases/{id}` | 改名描述 / 删除（级联删文档与 ES 索引） |
| POST | `/knowledge-bases/{id}/documents` | multipart 上传 pdf/docx → 注册 + 后台入库 |
| GET | `/knowledge-bases/{id}/documents` | 文档列表（状态机 pending/ingesting/ready/failed） |
| DELETE | `/knowledge-bases/{id}/documents/{doc_id}` | 删除文档（含向量） |
| POST | `/knowledge-bases/{id}/documents/{doc_id}/reindex` | 重建索引（需 MinIO 留底） |
| GET | `/knowledge-bases/{id}/search?q=&k=` | 检索（管理面调试） |

### 定时任务 `/cron`

| Method | Path | 说明 |
|--------|------|------|
| GET/POST | `/cron/jobs` | 列出 / 创建定时任务（绑定智能体 + prompt，落库即生效） |
| GET/PUT/DELETE | `/cron/jobs/{job_id}` | 详情 / 更新（含启停）/ 删除 |
| POST | `/cron/jobs/{job_id}/run` | 立即触发一次（测试补跑） |
| GET | `/cron/jobs/{job_id}/runs` | 运行历史（最近 50 条） |

### 其他

| Method | Path | 说明 |
|--------|------|------|
| GET | `/health` | 健康检查 + `capabilities`（kb_enabled / voice_enabled） |

<a id="skills"></a>

## 技能目录约定

全局 skills 池：`{LOCAL_ROOT}/skills/`（个人用，跨智能体共享）。一个 skill = 一个子目录
`{LOCAL_ROOT}/skills/<name>/`。`AgentConfig.skills` 引用为 `/skills/<name>/`。
示例种子在 `backend/data/skills/`（weather / prd / langgraph-docs / svg-diagram 等；
本地目录、未入库，可按需 `git add -f` 纳入），镜像构建时烘焙进数据卷。

<a id="jit-install"></a>

## 运行时依赖补齐（allow_shell + JIT 安装）

预置 `allow_shell` 开关（`app/core/schema_utils.py` AgentConfig）：任一节点置 `true`，`_build_backend`
（`app/core/core_utils.py`）即挂载 `UserScopedLocalShellBackend`，框架自动注册 `execute` 工具。结合容器内
预装的 `uv/uvx/npm/npx/python`（`Dockerfile`），可让智能体**在会话中即时安装 MCP 前置依赖 / 补齐缺失环境**
——这是应对"镜像未预热的第三方 MCP"的主要运行时手段。

### 工作链路
1. 智能体通过 `execute` 跑安装命令（逐条 HITL 审批，见下"风险点"）。
2. 安装完成后，调 `POST /mcp/refresh` 让新注册的 stdio MCP 重连上线。
3. 目标 agent 在 `tool_names` 引用 `{server}__{tool}` 即可使用。

### 命令规范（容器以非 root `app`(uid 1001) 运行，无 sudo）
| 目标 | 正确做法 | 原因 |
|---|---|---|
| Python 包 | `pip install --user <pkg>` 或 `uv tool install <pkg>`（命中已授权的 `/opt/uv-cache`） | `app` 不可写 `/usr/local`；裸 `pip install` 会 `PermissionError` |
| Node 包 | `npm pack <pkg>`（仅拉缓存）或本地 `npm install` | 全局目录同样不可写 |
| 系统包 | **不支持**，只能走 `Dockerfile` 的 `apt-get`（build 期 root） | 容器无 `sudo`，`app` 非 sudoer |
| 真隔离 | `python -m venv /home/app/<env>` 后在 venv 内装 | venv 的 site-packages 不进后端 `sys.path` |

### ⚠️ 风险点（务必遵守）
- **共享解释器 + 共享 HOME**：`execute` 是 backend 容器内以 `app` 跑的 subprocess，与后端用**同一个
  `/usr/local/bin/python`、同一个 `HOME=/home/app`**。Python 默认把 `/home/app/.local/lib/python3.11/site-packages`
  加入 `sys.path` 且**优先级高于系统包** ⇒ `--user` 装的包后端也能 import；**若覆盖后端已有同名依赖的版本，会直接拖崩后端**。
  规避：`--user` 只加新包、绝不 `--upgrade` 已有依赖；需无副作用隔离时用 venv。
- **execute 非 jail**：`cwd` 仅设起始目录，命令可 `cd /`、读 `../..` 触达任意路径（含其他用户 workspace）。
  正因如此，`allow_shell` 一开会**强制注入** `interrupt_on={"execute": True}`（`app/core/graph_utils.py`，不可关闭）——
  每条命令必须人工 approve，这是唯一护栏。
- **不持久化**：运行时装入容器的包，容器重启 / 重打镜像即丢。要持久化须把 `/opt/uv-cache`、`/opt/npm-cache`
  挂成卷，或纳入 `Dockerfile` 的 `UVX_MCP_PACKAGES` / `NPM_MCP_PACKAGES` 构建期预热。

### 参考用例：环境安装专用智能体
建一个**仅开 shell** 的轻量 agent（`system_prompt` 约束其只做依赖安装，不做其他），典型会话：
```text
用户：注册一个 arxiv-mcp，但镜像没预热，先帮我装好。
agent(execute)：uv tool install arxiv-mcp-server        ← 你逐条 approve
用户：装好了，去 /settings/mcp 注册并刷新。
（前端：POST /mcp/servers 注册 stdio MCP → POST /mcp/refresh）
agent：已确认工具列表出现 arxiv__*，业务 agent 的 tool_names 引用即可。
```
> 生产建议：稳定依赖优先写进 `Dockerfile` 预热（`UVX_MCP_PACKAGES`），JIT 安装仅用于临时 / 补漏 / 一次性场景。

<a id="file-change"></a>

## 工作区文件变更徽标（file_change）

agent 落盘/编辑/删除工作区文件时，后端在写入瞬间（`TrackedUserScopedFilesystemBackend`，
仅观测、不改写入语义）用 difflib 计行级差异，经 contextvar 收集器（task 级隔离）汇入
SSE 流（`file_change` 事件，线序恒为 tool_call → tool_result → file_change），前端呈现
`编辑 · ChatPage.tsx +3 −2` 式徽标（聊天时间线卡片 + 工作区树节点）。

已知限制：
- `allow_shell` 的 `execute` 命令绕过文件后端，不可拦截：只有回合结束后的树刷新可见
  增删，无行级计数（shell 是显式高危审批路径，可接受）。
- 刷新页面后历史回合不回放徽标（MVP 不落库；Phase 2 可按回合存 records 回放）。
- 二进制/超大（>512KB）文件不做行级计数，仅记动作（added/removed=null）。

<a id="quality"></a>

## 代码风格

```powershell
cd backend
ruff check app/     # line-length 110，见 pyproject.toml
```

> 单测与辅助脚本（`tests/`、`scripts/`）为本地开发资产，未纳入仓库（见根 `.gitignore`）。

<a id="origin"></a>

## 目录来源

`core/` `mcps/` 迁移自 `DeepAgentsDemos`，改动点：
- 移除 `sys.path.append("./")` hack → 标准包导入
- `base_confs` 全局单例 → `config.py` (pydantic-settings) + `deps.py` (Depends)
- `_build_backend` 改条件挂载（memory/workspace/skills 按配置触发）+ 路径注入
- 新增 `memory` / `workspace` 配置字段；新增 `topology_utils`（委派拓扑 Mermaid 渲染）
- 本地工具脚本移除，工具全 MCP 化；HITL 统一为声明式 `interrupt_on`
