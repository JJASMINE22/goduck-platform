# goduck-platform

个人智能体搭建 / 编排 / 会话平台 —— 基于 **deepagents + LangGraph + MCP**。

> 定位：让个人用户搭建自己的智能体，支持新增 / 更新（更新即替换）、随时增删工具与技能，
> 重启自动加载已建智能体。更新智能体会级联清除其旧会话，配置历史 append-only 保留可还原。

## 架构

分层模块化单体。请求链路：`API 层 → Service 层 → Runtime/Persistence → Infra`。

```
┌─────────────────────────────────────────────────────────────┐
│                         Frontend (React)                     │
│   智能体编辑器 · 会话/SSE · 模型管理 · MCP管理 · 技能 · 拓扑   │
└───────────────────────────┬─────────────────────────────────┘
                            │ HTTP / SSE  (Vite proxy /api)
┌───────────────────────────▼─────────────────────────────────┐
│                    Backend (FastAPI + LangGraph)             │
│  API 层 → Service 层 → Runtime(deepagents) → Persistence    │
└──────────┬──────────────────────────────┬───────────────────┘
           │                              │
     ┌─────▼─────┐                  ┌─────▼─────┐
     │ PostgreSQL │                  │   MinIO   │
     │ 业务表+LG  │                  │  附件字节  │
     └───────────┘                  └───────────┘
```

- **持久化**：业务表（psycopg3 直连）与 LangGraph 原生表（`AsyncPostgresSaver` / `AsyncPostgresStore`）共享同一 `AsyncConnectionPool`。
- **数据驱动**：模型（LLM + API Key）与 MCP server 均存于 DB 表，通过 API 运行时注册，**不在环境变量里**；重启自动加载。
- **无版本化（方案 B）**：智能体 `id` 全程稳定；更新 = in-place 覆写配置 + 追加历史快照 + 级联删旧会话。

## 功能特性

- **智能体管理**：树形配置编辑、实时校验、Mermaid 委派拓扑预览、配置历史与一键还原。
- **会话与流式对话**：SSE 流式输出、HITL（Human-in-the-Loop）中断 / 审批 / 编辑 / 拒绝。
- **模型注册**：OpenAI 兼容客户端族（通吃 OpenAI / DeepSeek / Qwen / 本地），保存前可测试连通性。
- **MCP 工具**：MCP server 运行时注册，工具按 `{server}__{tool}` 命名空间化防冲突。
- **附件能力**：图片 / 文档上传（MinIO 持久），支持多模态（视觉）模型。
- **技能池**：跨智能体共享的 `/skills/` 目录，multipart 上传。

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | FastAPI · LangGraph · deepagents · langchain-openai · psycopg3 · MinIO · pydantic-settings |
| 前端 | React 19 · TypeScript · Vite · Ant Design · Zustand · TanStack Query · Mermaid · react-hook-form |
| 基础设施 | PostgreSQL 16 · MinIO（docker-compose 一键起） |

## 目录结构

```
goduck-platform/
├── backend/                 # FastAPI + LangGraph 后端（详见 backend/README.md）
│   ├── app/
│   │   ├── api/             # 路由：agents / sessions / stream / models / mcp / skills / uploads
│   │   ├── services/        # 业务逻辑（agent / session / mcp / model / stream / storage）
│   │   ├── core/            # deepagents 内核（递归构建 / 拓扑渲染 / HITL）
│   │   ├── models/          # psycopg3 持久化 + pydantic 边界模型
│   │   └── mcps/            # 本地 MCP 网关服务
│   ├── agent_confs/         # 智能体 YAML 模板（seed 用）
│   ├── .env.example
│   └── pyproject.toml
├── frontend/                # React 前端（详见 frontend/README.md）
│   ├── src/
│   │   ├── api/             # API 客户端封装
│   │   ├── features/        # agent-editor / chat / topology
│   │   ├── pages/           # agents / sessions / settings(models·mcp·skills)
│   │   └── store/           # zustand 状态
│   └── vite.config.ts       # dev 经 /api proxy 转发到后端
├── docker-compose.yml       # PostgreSQL + MinIO（本地开发基础设施）
└── .gitignore
```

> `mcp-servers/`（附件解析 wrapper 集群）后续单独纳入，本仓库暂不含。

## 快速开始（本地）

前置要求：**Docker**（起基础设施）、**Python ≥ 3.10**（后端）、**Node ≥ 20**（前端）。

### 1. 启动基础设施（PostgreSQL + MinIO）

```powershell
docker-compose up -d
# PostgreSQL -> localhost:5433   MinIO -> localhost:9000  控制台 -> localhost:9001
```

默认凭据（见 `docker-compose.yml` 内联默认值）：PostgreSQL `goduck/changeme`，MinIO `minioadmin/minioadmin123`。如需自定义，在仓库根目录建 `.env` 覆盖对应变量即可。

### 2. 配置后端

```powershell
cd backend
copy .env.example .env      # 默认值已与 docker-compose 对齐，开箱可用，无需改动
pip install -e .            # 安装依赖
```

> 业务表（agent / session / mcp_server / model / upload）+ LangGraph 原生表均由后端启动时自动建表，**无需手动跑迁移**。

### 3. 启动后端

```powershell
python -m app.main          # 或 uvicorn app.main:app --reload
# 看到 "goduck-platform started" 即成功；访问 http://localhost:8000/health
```

### 4. 启动前端

```powershell
cd frontend
npm install
npm run dev                 # http://localhost:5173  （/api 自动代理到 8000）
```

### 5. 在界面完成数据配置（顺序有依赖）

1. **注册模型**：`/settings/models` → 注册模型 → 填 `model_type` / `api_key` / `base_url`（可先点「测试连接」）。
2. **创建智能体**：`/agents/new` → 配置 AgentConfig 树 + 「绑定模型」下拉（必须先注册模型）→ 保存。
3. （可选）`/settings/mcp` 注册 MCP 网关、`/settings/skills` 上传技能。
4. **会话测试**：创建会话 → 发消息 → SSE 流式对话 / HITL。

> 智能体 `model_id` 必填，因此**模型必须先于智能体注册**。

## 测试

```powershell
# 后端（纯 schema + 拓扑渲染单测，无需 DB/MCP）
cd backend
python -m pytest tests/ -q

# 前端
cd frontend
npm run typecheck           # tsc 类型检查
npm run lint                # eslint
```

## 详细文档

- [backend/README.md](./backend/README.md) —— 后端架构、数据模型、API 速览、关键设计
- [frontend/README.md](./frontend/README.md) —— 前端技术栈、脚本、目录结构

## License

私有项目（Private）。
