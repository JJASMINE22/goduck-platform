# goduck-platform

个人智能体搭建 / 迭代 / 会话工具 — 基于 deepagents + LangGraph + MCP。

> 定位：个人用户搭建自己的智能体，支持新增/更新（更新即替换）、随时增删工具与技能，
> 重启自动加载已建智能体。更新智能体会级联清除其旧会话，配置历史 append-only 保留可还原。

## 架构

分层模块化单体。请求链路：`API 层 → Service 层 → Runtime/Persistence → Infra`。

```
backend/
├── app/
│   ├── main.py              # FastAPI 入口 + lifespan 装配 + 启动预热
│   ├── config.py            # pydantic-settings 集中配置（含派生路径 property）
│   ├── deps.py              # 依赖注入：AppState + SimpleUser + FastAPI 访问器
│   ├── seed.py              # CLI：YAML 模板 → 灌入智能体
│   │
│   ├── api/                 # 【API 层】FastAPI 路由 + 边界校验
│   │   ├── agents.py        #   /agents        智能体 CRUD + 校验 + 拓扑预览 + 历史还原
│   │   ├── sessions.py      #   /sessions      会话 CRUD + 历史
│   │   ├── stream.py        #   /sessions/*    SSE 流式对话 + HITL resume
│   │   ├── skills.py        #   /skills        技能上传/列/删
│   │   └── mcp.py           #   /mcp           MCP server 注册 + 工具列表 + 健康检查
│   │
│   ├── services/            # 【Service 层】业务逻辑（不做边界类型重复校验）
│   │   ├── agent_service.py #   AgentService   校验/CRUD/mermaid 渲染
│   │   ├── session_service.py#  SessionService 会话生命周期 + checkpointer 历史
│   │   ├── agent_registry.py#   AgentRegistry  按 agent_id 缓存编译图（LRU+TTL）
│   │   ├── mcp_lifecycle.py #   MCPLifecycle   数据驱动 MCP 连接池 + 工具名命名空间
│   │   ├── stream_adapter.py#   StreamAdapter  astream → SSE 事件
│   │   └── hitl_resolver.py #   HITLResolver   interrupt 校验 + 决策归一
│   │
│   ├── core/                # 【Runtime 层】deepagents 内核（迁移自 DeepAgentsDemos）
│   │   ├── schema_utils.py  #   AgentConfig/RubricConfig pydantic + YAML/JSON loader
│   │   ├── graph_utils.py   #   create_agent_recursive（compiled+dicted 混合）
│   │   ├── core_utils.py    #   middleware + _build_backend（条件挂载）
│   │   ├── topology_utils.py#   委派拓扑 Mermaid 渲染（build_delegation_mermaid）
│   │   └── hitl_utils.py    #   CLI HITL 决策收集（dev 用）
│   │
│   ├── models/              # 【Persistence 层】
│   │   ├── db.py            #   psycopg3 直连：DDL + agent/history/session/mcp_server CRUD
│   │   └── schemas.py       #   pydantic 请求/响应模型（边界校验）
│   │
│   ├── tools/rubric_tools.py#   rubric grader 内部取证工具（仅挂 RubricMiddleware）
│   └── mcps/                #   MCPClientManager + UnifiedGateway mcp_server（本地服务）
│
├── agent_confs/             # 智能体 YAML 模板（seed 用）
└── tests/                   # 纯 schema + 拓扑渲染单测
```

**持久化**：业务表（psycopg3 直连）与 LangGraph 原生表（`AsyncPostgresSaver` /
`AsyncPostgresStore`）共享同一 `AsyncConnectionPool`。

迁移原则：**不改 deepagents 调用方式**（`create_deep_agent` 签名稳定），只在外围
加服务层 / 协议层 / 持久化层。所有依赖走 `deps.py` 注入，无全局单例。

## 数据模型

业务表（4 张，psycopg3 直连，与 LangGraph 原生表共享同一连接池）：

| 表 | 作用 | 关键约束 |
|---|---|---|
| `agent` | 智能体当前 active 配置（单行可变）。`id` 全程稳定不变，更新即 in-place 覆写 `config_json`。 | `name` 唯一（创建/更新约束依据）；`status` ∈ draft/active/archived |
| `agent_config_history` | 配置历史快照（append-only 审计 + 手动还原来源）。更新时不删，仅追加。 | `agent_id` → `agent(id)` ON DELETE CASCADE；按 `(agent_id, created_at DESC)` 索引 |
| `session` | 会话。绑定 `agent_id`；`thread_id` 是 LangGraph checkpointer 的键。 | `thread_id` 唯一；`agent_id` → `agent(id)` ON DELETE CASCADE；同时索引 `user_id` |
| `mcp_server` | MCP 服务注册（用户配置的第三方/本地 MCP）。`name` 作为工具名前缀。 | `name` 唯一；`kind` ∈ stdio/http；`config_json` 存 `{command,args,env}` 或 `{url,headers}` |

两类"历史"区分：**会话历史（session + LangGraph checkpoints）= 更新智能体时删除**；
**配置历史（agent_config_history）= 保留可还原**。

> LangGraph 原生表（`checkpoints` / `checkpoint_writes` / `checkpoint_blobs` / store）
> 由 `AsyncPostgresSaver.setup()` / `AsyncPostgresStore.setup()` 创建，更新智能体时
> 会显式删除失效 `thread_id` 对应的 checkpoints 三表行以防泄漏。

## 关键设计

- **无版本化（方案 B）**：`agent.id` 全程稳定不变；更新 = in-place 覆写 `config_json`
  + 追加 `agent_config_history` 快照。更新即级联删该 agent 的全部会话，并清理 LangGraph
  checkpoints 残留（会话历史删、配置历史留）。重启加载所有 `active` 智能体重建 registry。
- **工具全 MCP 化**：本地工具脚本移除；HITL 走声明式 `interrupt_on`（与 MCP 工具兼容，
  已按官方文档验证）。MCP server 由 `mcp_server` 表注册，启动时动态连接，工具按
  `{server}__{tool}` 命名防跨 server 冲突。
- **拓扑可视化**：后端以 `AgentConfig` 为唯一数据源生成 Mermaid 文本（`build_delegation_mermaid`），
  前端 mermaid.js 渲染。创建/校验/更新/详情各接口附带 `mermaid` 字段，零图像/网络依赖。
- **复用优先**：校验复用 `AgentConfig.model_validate`；agent 构建复用 `create_agent_recursive`；
  backend 条件挂载（memory/workspace/skills 按配置是否声明决定是否挂路由）。
- **HITL 统一**：`interrupt_on` 在 SSE/HTTP 层统一为 `event: interrupt`；SSE 异常兜底转 `event: error`。

## 运行时依赖补齐（allow_shell + JIT 安装）

预置 `allow_shell` 开关（`app/core/schema_utils.py` AgentConfig）：任一节点置 `true`，`_build_backend`
（`app/core/core_utils.py`）即挂载 `UserScopedLocalShellBackend`，框架自动注册 `execute` 工具。结合容器内
预装的 `uv/uvx/npm/npx/python`（`Dockerfile`），可让智能体**在会话中即时安装 MCP 前置依赖 / 补齐缺失环境**
——这是应对"镜像未预热的第三方 MCP"的主要运行时手段。

### 工作链路
1. 智能体通过 `execute` 跑安装命令（逐条 HITL 审批，见下"风险点"）。
2. 安装完成后，调 `POST /mcp/refresh`（见"API 速览 / MCP 运维"）让新注册的 stdio MCP 重连上线。
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

## 运行

### 1. 依赖

conda 环境 `agent`（已有 langchain/langgraph/deepagents 等）。新增文件上传依赖：

```powershell
pip install python-multipart
```

### 2. 配置

复制 `backend/.env.example` → `backend/.env`。默认值已与根目录 `docker-compose.yml`
对齐（PostgreSQL `goduck/changeme`、MinIO `minioadmin/minioadmin123`），用 compose 起基础设施时无需改动。
LLM 与 MCP server 均不在 `.env` 里 —— 模型通过 `/models`、MCP 通过 `/mcp/servers` 运行时注册（见下）。
所有路径走配置，**禁止硬编码**。

### 3. 基础设施（PostgreSQL + MinIO）

```powershell
docker-compose up -d          # 一键起 PostgreSQL(5433) + MinIO(9000)；或使用现有实例
```

业务表（agent / agent_config_history / session / mcp_server / model / upload）由 app lifespan
自动建表（幂等）；LangGraph 原生表由 `AsyncPostgresSaver.setup()` / `AsyncPostgresStore.setup()` 创建。

### 4. MCP 网关（可选，本地启动的服务）

```powershell
python -m app.mcps.mcp_server          # 监听 127.0.0.1:8088/mcp
```

注册 MCP server（运行时持久化到 `mcp_server` 表）：

```powershell
# 例：注册上面启动的本地网关
curl -X POST http://localhost:8000/mcp/servers -H "Content-Type: application/json" \
  -d '{"name":"UnifiedGateway","kind":"http","config":{"url":"http://127.0.0.1:8088/mcp"},"enabled":true}'
```

### 5. 启动后端

```powershell
cd backend
python -m app.main                     # 或: uvicorn app.main:app --reload
```

启动会预热所有 `active` 智能体（单个失败不阻塞）。

### 6. 种子智能体（首次）

```powershell
python -m app.seed agent_confs/default.yaml --name "Default Gateway" --creator admin
```

## API 速览

接口按模块分组。SSE 流终止事件固定为 `done` / `interrupt` / `error`，前端可据此判定最终状态。

### 智能体管理 `/agents`

| Method | Path | 说明 |
|--------|------|------|
| POST | `/agents/validate` | 校验 config + 返回 mermaid 预览（不落库） |
| POST | `/agents` | 创建（name 唯一，409 冲突）→ 建 registry → 返回 mermaid |
| PUT | `/agents/{id}` | 更新（必须存在）→ 级联删会话+清 checkpoint → 重建 registry |
| GET | `/agents` | 列出智能体 |
| GET | `/agents/{id}` | 详情 + 当前 active mermaid（悬浮预览） |
| GET | `/agents/{id}/topology.mmd` | Mermaid 文本（PlainTextResponse） |
| GET | `/agents/{id}/history` | 配置历史（还原选择） |
| POST | `/agents/{id}/restore` | 用 `{history_id}` 还原 |
| GET | `/agents/{id}/export.yaml` | 导出当前配置 YAML |
| DELETE | `/agents/{id}` | 归档（status=archived） |

### 会话管理 `/sessions`

| Method | Path | 说明 |
|--------|------|------|
| POST | `/sessions` | 创建会话（绑定 agent_id） |
| GET | `/sessions?user_id=` | 列出用户会话（含 agent_name） |
| GET | `/sessions/{id}` | 会话详情 |
| DELETE | `/sessions/{id}` | 归档会话 |
| GET | `/sessions/{id}/history` | checkpointer 历史消息 |
| POST | `/sessions/{id}/messages` | 发送消息 → SSE 流 |
| POST | `/sessions/{id}/resume` | HITL 决策恢复 → SSE 流 |

### 技能管理 `/skills`

| Method | Path | 说明 |
|--------|------|------|
| GET | `/skills` | 列出技能（子目录 + 文件数） |
| POST | `/skills` | multipart 上传技能（name + 文件，防目录穿越） |
| DELETE | `/skills/{name}` | 删除技能目录 |

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

### 其他

| Method | Path | 说明 |
|--------|------|------|
| GET | `/health` | 健康检查 |

## 技能目录约定

全局 skills 池：`{LOCAL_ROOT}/skills/`（个人用，跨智能体共享）。一个 skill = 一个子目录
`{LOCAL_ROOT}/skills/<name>/`。`AgentConfig.skills` 引用为 `/skills/<name>/`。

## 测试

```powershell
cd backend
python -m pytest tests/ -q            # 需安装 pytest
```

`tests/test_file_change_tracker.py` 为纯单测（无需 DB/MCP）：difflib 行级计数边界、
tracked backend 的前读→super→后读埋点、contextvar 收集器生命周期与并发隔离。

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

## 目录来源

`core/` `mcps/` 迁移自 `DeepAgentsDemos`，改动点：
- 移除 `sys.path.append("./")` hack → 标准包导入
- `base_confs` 全局单例 → `config.py` (pydantic-settings) + `deps.py` (Depends)
- `_build_backend` 改条件挂载（memory/workspace/skills 按配置触发）+ 路径注入
- 新增 `memory` / `workspace` 配置字段；新增 `topology_utils`（委派拓扑 Mermaid 渲染）
- 本地工具脚本移除，工具全 MCP 化；HITL 统一为声明式 `interrupt_on`
