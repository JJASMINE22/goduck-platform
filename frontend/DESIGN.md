# goduck-platform 前端设计与实现准则

> 本文档是前端实现的**唯一设计权威**，与后端 `backend/README.md` + `.kilo/plans/*`
> 对齐。任何架构/风格决策的变更都应回写到本文档。
>
> 后端定位：个人智能体搭建 / 迭代 / 会话工具（deepagents + LangGraph + MCP）。
> 前端定位：**配置驱动的智能体工作台** + **SSE 流式会话终端** + **运维控制台**。

---

## 0. 现状对齐（后端契约速查）

| 域 | 前缀 | 关键能力 |
|---|---|---|
| 智能体 | `/agents` | CRUD + `/validate`（dry-run mermaid）+ `/history` + `/restore` + `/export.yaml` + `/topology.mmd` |
| 会话 | `/sessions` | CRUD + `/history` |
| 流式对话 | `/sessions/{id}/messages`、`/resume` | **POST 返回 SSE**；终止事件固定 `done` / `interrupt` / `error` |
| 技能 | `/skills` | multipart 上传 / 列 / 删 |
| MCP 运维 | `/mcp` | server CRUD + `/tools`（全量命名空间工具）+ `/status` + `/refresh` |
| 附件 | `/sessions/{id}/uploads`、`/attachment-capability` | multipart 上传（魔数判 mime）+ 能力查询 |
| 模型 | `/models` | CRUD + `/test`（连通性探测）|
| 健康 | `/health` | — |

- 后端默认监听 **`0.0.0.0:8000`**（`config.py`）。
- SSE 事件类型：`token`（保留，当前未用）/ `tool_call` / `tool_result` / `agent_message` / `interrupt` / `done` / `error`。
- `AgentConfig`（`core/schema_utils.py`）是配置树**唯一真相**：含递归 `subagents`、`rubric`、`interrupt_on`、`memory`/`workspace`/`skills`/`allow_attachments`。
- 智能体 `model_id` 绑定注册模型（`model` 表），模型元数据 `supports_vision` 决定附件路径。

---

## 1. 技术栈选型（含决策理由）

| 类别 | 选型 | 理由 |
|---|---|---|
| 框架 | **React 19 + TypeScript**（已就位） | 现有模板基础，保持不变 |
| 构建 | **Vite 8**（已就位） | 现有模板基础 |
| 路由 | **React Router v7** | 多页面工作台标准方案；与 Vite 兼容好 |
| 服务端状态 | **TanStack Query (React Query) v5** | 契约：CRUD 密集的运维 + 列表 + 缓存失效；天然匹配后端「更新即失效重建」语义 |
| 本地/UI 状态 | **Zustand** | 轻量；聊天草稿/HITL 待决/当前编辑中的 AgentConfig 树等 |
| 表单 | **react-hook-form + zod** | AgentConfig 树深、递归、字段多；zod schema 可与后端 pydantic 对齐 |
| UI 组件库 | **Ant Design 5**（中文友好，Table/Tree/Steps/Drawer/Form/Upload 成熟） | 个人工具优先**开发速度**；树形（递归 subagents）、多选（tool_names）、步骤式编辑器是刚需 |
| 样式 | **CSS Variables 主题** + 少量 **CSS Modules**；聊天界面可定制 | 不引入 Tailwind 以避免与 AntD 冲突；主题用 AntD 的 `ConfigProvider` + token |
| 图标 | **lucide-react** | 轻量、风格统一 |
| Markdown | **react-markdown + remark-gfm + rehype-highlight**；流式期 **remend** 补全半截语法 + 120ms 尾沿节流重解析（`Markdown` 组件 `streaming` prop） | 渲染 agent 回复、rubric、附件清单；边流边排版 |
| Mermaid/SVG 流式门禁 | **语法/净化门禁渐进渲染**（`MdStreamingContext`） | mermaid 半截语法 parse 失败静默等待（`.md-deferred` 占位），通过才渲；流式中 SVG 未闭合走源码降级 |
| Mermaid | **mermaid.js** | 后端只产文本，前端渲染（零网络依赖） |
| SSE | **`@microsoft/fetch-event-source`** | ⚠️ 后端是 **POST 返回 SSE**，浏览器原生 `EventSource` 仅支持 GET，**必须**用 fetch-based 方案 |
| 代码高亮 | **rehype-highlight**（随 markdown） | 工具调用参数 / 代码块 |

> **不引入**：Redux（过重）、axios（fetch + TanStack Query 已够）、Tailwind（与 AntD 主题体系冲突）。
> **保留升级口**：若后续需要更强品牌定制，可把 AntD 替换为 shadcn/ui + Tailwind，但需整体迁移，不混用。

---

## 2. 前端架构分层（对标后端分层模块化单体）

后端链路：`API → Service → Runtime/Persistence → Infra`。前端镜像为：

```
api/        ← 对标后端 api/：纯函数 fetch 封装，零业务逻辑，返回 Promise<T>
types/      ← 对标后端 schemas.py：TS 类型与 pydantic 1:1 对齐
hooks/      ← 对标后端 services/：React Query hooks，业务逻辑（缓存/失效/乐观更新）
store/      ← 对标后端 app.state：Zustand 跨页面 UI 状态
components/ ← 纯展示组件（无 fetch，靠 props/回调）
features/   ← 按域聚合的「特性组件」（含 hooks 调用的复合视图，如 AgentEditor）
pages/      ← 路由级页面（组装 features）
lib/        ← 纯工具（sse 解析、mermaid 渲染、日期、mime 展示）
```

**层间规则**（强约束，对标后端「Service 不重复边界校验」）：
- `api/` 不依赖 `hooks/`/`store/`/`components/`。
- `hooks/` 只依赖 `api/` + `types/` + `lib/`。
- `components/` 不直接调 `api/`；需要数据时通过 `hooks/` 注入。
- `pages/` 只做组装与路由参数解析，不写业务逻辑。

---

## 3. 目录结构

```
frontend/
├── index.html
├── vite.config.ts            # 加 server.proxy /api → :8000、@ alias
├── tsconfig.app.json
├── package.json
└── src/
    ├── main.tsx
    ├── App.tsx               # 路由根 + 全局 Provider
    ├── api/                  # API 客户端层
    │   ├── client.ts         #   基础 fetch 封装：baseURL/错误归一/JSON 解析
    │   ├── agents.ts
    │   ├── sessions.ts
    │   ├── stream.ts         #   POST-SSE 专用（fetch-event-source）
    │   ├── skills.ts
    │   ├── mcp.ts
    │   ├── uploads.ts
    │   ├── models.ts
    │   └── health.ts
    ├── types/                # 与后端 schemas.py 1:1（见 §5）
    │   ├── agent.ts
    │   ├── session.ts
    │   ├── stream.ts
    │   ├── mcp.ts
    │   ├── upload.ts
    │   └── model.ts
    ├── hooks/                # React Query hooks（见 §7）
    │   ├── useAgents.ts
    │   ├── useSessions.ts
    │   ├── useStreamChat.ts  # SSE 会话状态机（见 §8）
    │   ├── useSkills.ts
    │   ├── useMcp.ts
    │   ├── useUploads.ts
    │   └── useModels.ts
    ├── store/                # Zustand UI 状态
    │   ├── chatStore.ts      #   当前会话草稿、HITL 待决、附件暂存
    │   └── agentEditorStore.ts # 当前编辑中的 AgentConfig 树（未保存草稿）
    ├── features/             # 域聚合复合视图
    │   ├── agent-editor/     #   AgentConfig 树形/步骤编辑器 + mermaid 预览
    │   ├── chat/             #   聊天界面（消息流 + HITL + 附件 + KB 留口）
    │   ├── topology/         #   mermaid 渲染封装
    │   └── capability/       #   能力驱动的附件/KB 组件
    ├── components/           # 纯展示通用组件
    │   ├── layout/           #   AppShell / Sidebar / Topbar
    │   ├── markdown/
    │   └── feedback/         #   ErrorBoundary / EmptyState / Loading
    ├── pages/                # 路由级（见 §9）
    ├── lib/
    │   ├── sse.ts            #   fetch-event-source 封装
    │   ├── mermaid.ts        #   mermaid 安全渲染（只渲染后端产出的文本）
    │   ├── mime.ts           #   mime → 图标/展示名（前端只读展示，不判类型）
    │   └── format.ts         #   时间/字节/uuid 短显
    ├── styles/
    │   ├── theme.ts          #   AntD ConfigProvider token
    │   └── tokens.css        #   CSS 变量（配色/间距/圆角）
    └── vite-env.d.ts
```

---

## 4. 设计准则（对标后端设计原则）

后端原则（plan §0）：单数据源 / 无全局单例 / 路径注入 / 优雅无冗余 / 重启自洽。
前端对应准则：

| # | 准则 | 对标后端 | 落地 |
|---|---|---|---|
| F1 | **类型即契约** | 单数据源 | `types/` 与 pydantic schema 1:1；API 层签名用类型，禁止 `any` |
| F2 | **能力驱动 UI** | 路径注入（配置驱动） | 附件/KB 组件可见性**只**由 `*-capability` 接口返回决定，绝不硬编码「该 agent 支持 XX」 |
| F3 | **SSE 终止事件优先** | 优雅无冗余 | 聊天状态机以 `done`/`interrupt`/`error` 为唯一终态判定；中间事件只增量更新 |
| F4 | **错误兜底** | 风险与兜底表 | 每个查询/变更/SSE 都有 error 分支；SSE 断流必须能恢复或明确报错，不卡死 loading |
| F5 | **失效即重建** | 重启加载 active 智能体 | 变更后用 TanStack Query `invalidateQueries` 精确失效；与后端「更新即 invalidate+rebuild registry」语义对称 |
| F6 | **不信任前端值** | 后端魔数判 mime / upload_id 防编造 | 前端**不**传 mime/kind（后端自查）；前端只传 `upload_id`；前端校验是体验，后端是安全 |
| F7 | **单一渲染入口** | topology 单一 Mermaid 数据源 | mermaid 文本**只**来自后端（`active_mermaid` / `/topology.mmd` / `/validate`），前端不自行生成拓扑 |
| F8 | **草稿与已存分离** | 两类历史区分（会话历史删 / 配置历史留） | 编辑中的 AgentConfig 树放 Zustand 草稿；保存才落库；历史还原走 `/restore` |

---

## 5. 类型契约规范（pydantic ↔ TypeScript）

### 5.1 映射规则

| pydantic | TypeScript |
|---|---|
| `uuid.UUID` | `string`（品牌类型可选 `type UUID = string`） |
| `datetime` | `string`（ISO 8601，展示时格式化） |
| `Optional[T]` | `T \| null` |
| `list[T]` | `T[]` |
| `dict[str, Any]` | `Record<string, unknown>`（配置树等已知结构用具名类型，不滥用 `any`） |
| `Literal["a","b"]` | `"a" \| "b"` |
| 嵌套 BaseModel | 接口/类型 |

### 5.2 核心类型骨架（与后端 `schemas.py` / `schema_utils.py` 对齐）

```ts
// types/agent.ts —— AgentConfig 树（单数据源，对应 backend AgentConfig）
export interface RubricConfig {
  content?: string | null;
  fixed?: boolean;
  max_iterations?: number;     // 1..20
  grader_tool_names?: string[];
}

export interface AgentConfig {
  name?: string | null;        // 主代理为 null
  description?: string | null;
  system_prompt: string;       // 必填
  tool_names?: string[];       // MCP 命名空间工具，来源 GET /mcp/tools
  skills?: string[] | null;    // 形如 "/skills/<name>/"
  memory?: string[] | null;    // "/memories/<name>.md"，仅主代理
  workspace?: string | null;   // 单一目录名，仅主代理
  interrupt_on?: Record<string, boolean> | null;
  is_main?: boolean;
  is_dicted?: boolean;
  subagents?: AgentConfig[] | null;   // ★ 递归
  rubric?: RubricConfig | null;
  allow_attachments?: boolean;
}

export interface AgentOut {
  id: string; name: string; description: string | null;
  model_id: string | null; status: "draft" | "active" | "archived";
  creator: string; updater: string | null;
  created_at: string; updated_at: string;
}
export interface AgentDetail extends AgentOut {
  config_json: AgentConfig;    // 后端存的是 dict，前端按 AgentConfig 解析
  active_mermaid?: string | null;
}
```

> 注意：后端 `AgentCreate.config` 是 `dict[str, Any]`，但语义就是 `AgentConfig` 树。前端**始终**按 `AgentConfig` 类型构造与提交，提交时序列化为普通对象即可。

```ts
// types/stream.ts —— SSE 事件（对应 backend streaming/events.py）
export type SSEEventType =
  | "token" | "tool_call" | "tool_result" | "agent_message"
  | "interrupt" | "done" | "error";

export interface SSEEvent<T = Record<string, unknown>> {
  type: SSEEventType;
  agent_path?: string | null;   // "root" | "root/researcher"
  payload: T;
}

// interrupt.payload 形状（对应 hitl_resolver._extract_interrupt）
export interface InterruptPayload {
  action_requests: Array<{ action?: { name: string; args: Record<string, unknown> } }>;
  review_configs: Record<string, unknown>[];
  raw: Record<string, unknown>[];
}

// resume 决策（对应 schemas.Decision）
export type DecisionType = "approve" | "edit" | "reject" | "respond";
export interface Decision {
  type: DecisionType;
  message?: string | null;
  edited_action?: { name: string; args: Record<string, unknown> } | null;
}
```

---

## 6. API 客户端层规范

### 6.1 基础封装（`api/client.ts`）

- 单一 `request<T>(path, init)`：拼 `BASE_URL`、带 JSON `Content-Type`、解析 JSON、归一化错误。
- `BASE_URL`：dev 走 Vite proxy（`/api` → `http://localhost:8000`），生产由 `VITE_API_BASE` 注入。**禁止硬编码** `localhost:8000`（对标后端路径注入原则）。
- 错误归一：HTTP 非 2xx → 抛 `ApiError { status, detail, message }`，保留后端 `detail`（后端中文消息可直接展示）。
- multipart：独立 `postForm()`，`Content-Type` 由浏览器自动设带 boundary。

### 6.2 一个域一个文件，函数名对齐后端语义

```ts
// api/agents.ts
export const agentsApi = {
  validate: (config: AgentConfig) => post<ValidateResponse>("/agents/validate", { config }),
  create: (b: AgentCreate) => post<AgentDetail>("/agents", b),
  update: (id: string, b: AgentUpdate) => put<AgentDetail>(`/agents/${id}`, b),
  list: () => get<AgentOut[]>("/agents"),
  get: (id: string) => get<AgentDetail>(`/agents/${id}`),
  topologyMmd: (id: string) => fetchText(`/agents/${id}/topology.mmd`),  // PlainTextResponse
  history: (id: string) => get<AgentHistoryItem[]>(`/agents/${id}/history`),
  restore: (id: string, b: RestoreRequest) => post<AgentDetail>(`/agents/${id}/restore`, b),
  exportYaml: (id: string) => fetchText(`/agents/${id}/export.yaml`),
  archive: (id: string) => del<AgentOut>(`/agents/${id}`),
};
```

### 6.3 关键非 JSON 接口
- `/agents/{id}/topology.mmd`、`/agents/{id}/export.yaml` 是 `PlainTextResponse` → 用 `fetchText()`，**不**按 JSON 解析。

---

## 7. 状态管理规范

### 7.1 服务端状态（TanStack Query）

- **query key 约定**：`["agents"]`、`["agents", id]`、`["sessions", { userId }]`、`["mcp","tools"]`、`["models"]`。
- **失效策略**（对标后端 update→invalidate+rebuild）：
  - 创建/更新/归档/还原 agent → `invalidateQueries({ queryKey: ["agents"] })` + 失效该 agent 详情。
  - 注册/改/删 MCP server → 失效 `["mcp","servers"]` + `["mcp","tools"]` + `["mcp","status"]`（后端会触发 refresh）。
  - 增删模型 → 失效 `["models"]`（后端已会 invalidate 全部 agent，前端列表下次拉取即跟随）。
- **乐观更新**：归档会话、删技能、删 MCP server 可乐观更新，失败回滚。

### 7.2 本地状态（Zustand）

| store | 内容 |
|---|---|
| `chatStore` | 当前会话消息列表（含流式增量）、`streamStatus: idle\|streaming\|interrupted\|done\|error`、HITL 待决 `interrupt`、附件暂存 `upload_ids`、KB 开关（留口） |
| `agentEditorStore` | 编辑中的 AgentConfig 树草稿、脏标志、当前校验错误、mermaid 预览缓存 |

> 聊天消息流**不**进 TanStack Query（流式增量不适合缓存），放 Zustand；历史消息（`/sessions/{id}/history`）用 Query 拉取后灌入 store。

---

## 8. SSE 流式处理规范（核心）

### 8.1 为什么不能用 `EventSource`
后端 `POST /sessions/{id}/messages` 返回 SSE（sse-starlette）。浏览器 `EventSource` 仅支持 GET、不支持自定义 body/header。**强制**使用 `@microsoft/fetch-event-source`（基于 fetch + ReadableStream）。

### 8.2 状态机（聊天 UI 唯一终态来源）

```
idle ──(send)──▶ streaming ──┬── done ──▶ idle
                             ├── interrupt ──▶ interrupted ──(resume)──▶ streaming
                             └── error ──▶ idle（展示错误）
```

- **只**以终止事件 `done`/`interrupt`/`error` 切换终态；`token`/`agent_message`/`tool_call`/`tool_result` 仅做增量 UI 更新。
- `interrupt` 事件 → 读 `payload.action_requests`，渲染 HITL 决策卡（approve/edit/reject/respond）。
- `done` → 清理 loading；`error` → 展示 `payload.message`（后端已脱敏/兜底）。
- 流程中任何异常（网络断）→ 视作 `error`（对标后端 error floor）。

### 8.3 中间事件渲染规则
| 事件 | 渲染 |
|---|---|
| `agent_message` | 追加到当前 agent 消息气泡（按 `agent_path` 区分主/子代理，子代理可折叠） |
| `tool_call` | 折叠「工具调用」卡：`name` + `args`（JSON 高亮） |
| `tool_result` | 挂回对应 `tool_call_id` 的卡片下方，默认折叠 |
| `interrupt` | HITL 决策卡（见 §10.3） |

### 8.4 封装示例（`hooks/useStreamChat.ts` 思路）
```ts
// 伪代码：POST-SSE + 终止事件驱动
import { fetchEventSource } from "@microsoft/fetch-event-source";

async function streamMessage(sessionId, body, handlers) {
  await fetchEventSource(`/api/sessions/${sessionId}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    openWhenHidden: true,            // 切后台不断
    onmessage(ev) {
      const e = JSON.parse(ev.data) as SSEEvent;
      switch (e.type) {
        case "agent_message": handlers.onAgentMessage(e); break;
        case "tool_call":     handlers.onToolCall(e); break;
        case "tool_result":   handlers.onToolResult(e); break;
        case "interrupt":     handlers.onInterrupt(e); handlers.onTerminal(); break;
        case "done":          handlers.onTerminal(); break;
        case "error":         handlers.onError(e); break;
      }
    },
    onerror(err) { handlers.onFatal(err); throw err; }, // 停止自动重连（避免重复发消息）
  });
}
```
> ⚠️ `onerror` 抛错以**关闭** fetch-event-source 的默认重连（否则会在 POST 幂等性未知时重发消息）。

---

## 9. 路由与页面结构

```
/                          → 重定向到 /agents
/agents                    → 智能体列表（新建按钮 / 状态过滤 / 归档入口）
/agents/new                → 新建（AgentEditor + 实时 validate + mermaid 预览）
/agents/:id                → 详情/编辑（tabs：配置 / 拓扑 / 历史 / 导出）
/sessions                  → 会话列表（按 user_id，含 agent_name）
/sessions/:id              → 聊天界面（核心）
/settings                  → 设置入口
/settings/models           → 模型注册管理（CRUD + /test 连通探测）
/settings/mcp              → MCP server 运维（CRUD + tools + status + refresh）
/settings/skills           → 技能池管理（上传/列/删）
```

布局：`AppShell`（左侧 Sidebar 导航 + 顶栏健康指示）包裹所有路由。Sidebar 项：智能体、会话、设置（模型/MCP/技能）。

---

## 10. 核心 UI 模式

### 10.1 Agent 配置编辑器（`features/agent-editor/`）
- **结构**：`Steps` 分步（基本信息 / 主代理配置 / 子代理树 / 工具与技能 / 评审 Rubric / 高级）。
- **子代理树**：AntD `Tree` + 递归编辑；强制后端校验规则（子代理禁 memory/workspace，dicted 不可嵌套）在前端**预校验**（体验），最终以 `POST /agents/validate` 为准（安全）。
- **工具多选**：数据源 `GET /mcp/tools`（全量命名空间工具），多选写入 `tool_names`。
- **实时预览**：配置变更防抖 → 调 `/agents/validate` → 返回 `mermaid` → `features/topology` 渲染。
- **草稿**：未保存树存 `agentEditorStore`；离开页面有脏数据确认。
- **保存**：新建走 `POST /agents`，编辑走 `PUT /agents/:id`（会话-智能体解耦后：更新不再级联删会话——既有会话直接续用新配置，无需弹窗告知；仅提示"更新后新回合即用新配置"）。

### 10.2 拓扑预览（`features/topology/`）
- 接收 mermaid 文本（来自 `active_mermaid` / validate / topology.mmd），用 mermaid.js 渲染。
- **安全**：只渲染后端产出的 mermaid，禁用前端拼接；mermaid `securityLevel: 'strict'`。
- 详情页支持悬浮预览（小图）+ 全屏查看（大图）。

### 10.3 聊天界面（`features/chat/`）
布局：左会话历史抽屉 + 中消息流 + 右上下文面板（附件/KB 留口）。

- **消息流**：user（右）/ agent（左，按 `agent_path` 标主/子代理）/ 工具卡（折叠）/ HITL 卡。
- **思考块**：默认收起（不自动展开/收起，尊重用户操作）；展开态放宽幅宽（`min(90%, 960px)`，
  收起回窄列）；流式中展开态贴底跟随（上滑即停跟），结束固化「思考 N 秒」
  （`endedAt` 落戳，仅 live 流式有值）并转 Markdown 渲染。
- **阅读模式（简洁 | 详细）**：composer 工具条图标钮 + Dropdown 单选列表（`DisplayModeToggle`，
  选中项文本后置裸 ✔ 主色高亮；图标随模式切换 Eye=详细 / EyeOff=简洁，状态即所见），纯客户端
  展示偏好（`chatDisplayStore`，localStorage 持久化，缺省详细），后端零感知。简洁 = MessageList 时间线投影跳过工具调用卡
  （含 kb_search 卡）；思考块、任务清单、用量徽标、文件变更卡、HITL 卡均不受影响。过滤只发生
  在渲染层——chatStore 恒为全量超集，切换即时生效、可回溯（含流式进行中，切回详细已到达的
  工具卡立即显影），回撤时间窗等存储层语义不变。归档/任务会话无 composer，沿用既有偏好展示。
- **输入区**：
  - 文本框 + 发送（禁用条件：`streamStatus !== idle && !== interrupted`；另：未选智能体的空会话禁发）。
  - **智能体选择器**（会话-智能体解耦）：composer 左下角，逐回合选择；切换即按新选中
    重拉全部能力查询（query key 含 agentId，keyed refetch）。门禁：流式生成中 /
    HITL 悬空期（且 last_agent_id 可解析）禁用——last 悬空时保持可选，为"选中新
    智能体回撤悬空回合"留逃生路径。选中态是 `chatStore.selectedAgentId`（纯 UI 态，
    永不落库），进入会话从 `session.last_agent_id` 初始化。
  - 附件按钮（**按 `attachment-capability` 渲染**，见 §10.4）。
  - rubric 单次覆盖输入（可选，对应 `SendMessageRequest.rubric`）。
- **HITL 卡**：`interrupt` 事件到达 → 渲染决策按钮组（approve / edit / reject / respond）。
  - `edit` → 展开表单编辑 `edited_action.{name,args}`。
  - 提交 → `POST /sessions/:id/resume` `{ decisions: [...] }` → 继续同状态机（恒经
    last_agent_id 取图；悬空期发新消息后端显式 409，需先审批或回撤该回合）。

### 10.4 能力驱动 UI（`features/capability/`）—— 准则 F2 落地
- 进入会话先 `GET /sessions/:id/attachment-capability`，据返回渲染：
  - `allow_attachments=false` → 隐藏附件按钮。
  - `allow_image=false` → 不收图片（即使能选文件）。
  - `allow_file=["docx"]` → `accept=".docx"`（前端仅做体验限制，类型判定以服务端魔数为唯一可信源）。
  - `max_size_mb` → 前端预校验体积（体验）。
- **KB 留口**：未来 `GET /sessions/:id/kb-capability` 同模式：`kb_available=false` → 隐藏「启用知识库」开关；为 true → 渲染 collection/version 下拉（见 §13）。

### 10.5 附件上传（`features/capability/`）
- `POST /sessions/:id/uploads`：multipart，**前端只传 `file`（含 filename）+ `user_id`（+ 可选 `agent_id`）**，**不传 mime/kind**（后端魔数判定，准则 F6）。
- 上传成功 → 把返回的 `upload_id`（+ filename/kind 展示）存入 `chatStore.attachments`，发消息时只带 `upload_id`。
- 上传后切到不支持图像的智能体 → 发送门 TOCTOU 纵深显式 422（"模型配置已变更或已切换智能体"）。

### 10.6 模型管理（`pages/settings/ModelsPage`）
- 列表 + 新建/编辑（`name`/`model_type`/`api_key`/`base_url`/`supports_vision`/`enabled`）。
- **保存前连通探测**：调 `POST /models/test`（不落库），成功才建议保存。
- 删除被引用模型 → 后端返 409 含 `referencing_agents` → 前端展示「请先为以下智能体更换模型」并列出引用清单。

---

## 11. 样式与主题规范

- **配色**：CSS 变量（`--color-bg`/`--color-fg`/`--color-primary`/`--color-danger`/`--token-...`），AntD 经 `ConfigProvider` 同步 token。
- **角色色**（消息流区分）：主代理 / 子代理 / 工具 / 用户 各一色 token，便于后续暗色模式。
- **双主题（Light | Dark，已实现）**：
  - 单一主题源 `store/themeStore`（zustand + localStorage），挂 `<html data-theme>` + `colorScheme`；
    AntD 经 `createAntdTheme(dark)` 切 `defaultAlgorithm`/`darkAlgorithm`（App.tsx 动态取用）。
  - `tokens.css` 两套值集：`:root`（浅色）与 `[data-theme="dark"]`（冷夜画布 + Google 暗色系提亮主色/语义色/角色色）。
  - **流体玻璃暗色适配**：所有玻璃配方（底/边/rim 高光/镜面亮斑/流光/选中态凸起/composer 三态）
    收敛为 `--glass-*` 变量（亮=白玻璃、暗=深色半透底 + 低 α 白 rim），index.css 玻璃规则零硬编码色；
    环境光层暗色下提亮（夜色中的流动辉光）；hljs 双色板（GitHub Light/Dark）随 data-theme 切换；
    mermaid 按主题出 neo/dark 变体并在切换时重渲染（Markdown/MermaidPreview 以 theme 为 effect 依赖）。
  - **切换入口**：侧边栏卡片底部「浅色 | 深色」玻璃分段胶囊（`ThemeToggle`，折叠态退化为圆形图标钮）。
  - **玻璃语言一致性**：顶栏页面主动作主按钮（主色染玻璃 `--btn-primary-*`）、Segmented 选中滑块
    （玻璃凸起，镜像菜单选中态）、Select 下拉弹层与选中项（body 前缀全局玻璃化）、
    中止按钮实心停止符（`Square fill`）统一同源成像配方。
- **间距/圆角**：统一 token，禁止散落魔法数字。
- **字体**：系统字体栈 + 等宽字体（代码/工具参数）。
- **响应式（自适应，无模式开关）**：桌面为主的工作台 + 竖屏/窄窗口自适应并存。断点
  （与 `hooks/useMediaQuery.ts` 的 `BREAKPOINTS` 同源）：**≤1100px（narrow）** 侧栏自动折叠
  为图标轨（回到宽屏恢复展开）、智能体编辑器三栏改「画布独占 + 模块/详情抽屉」；
  **≤768px（compact）** 侧栏整体改左缘抽屉（Topbar 汉堡钮唤起）、间距收紧、列表表格
  横向滚动、输入 16px 防 iOS 聚焦缩放。分工：**内容区分级优先容器查询**（如 TurnRail，
  免疫侧栏开合），仅组件树必须切换处（侧栏/编辑器抽屉化）经 `useMediaQuery` 换树，
  纯展示降级一律走 CSS（`index.css`「窄视口自适应」段）。触屏适配与断点正交
  （`@media (hover: none)`：hover 显形 affordance 常显；拖拽手柄统一 Pointer Events
  + `touch-action: none`）。竖屏布局是正确性问题（横版布局在窄视口即错），交给断点
  自动触发，不做用户开关——手动控件仅保留既有的侧栏折叠与主题切换。

---

## 12. 错误处理与兜底

| 场景 | 处理 |
|---|---|
| 查询失败 | TanStack Query `error` → 页面级 EmptyState + 重试按钮 |
| 变更失败 | 表单内联错误 + message；后端 `detail`（中文）直接展示 |
| 409（name 冲突 / 模型不可用 / session 归档） | 据 `detail` 精准提示 |
| SSE `error` 事件 | 聊天流追加系统错误消息，状态回 `idle` |
| SSE 网络断 | `onerror` → 视作 error 兜底（不自动重发消息，防重复） |
| 组件异常 | `ErrorBoundary` 包裹路由级，降级渲染 |
| 404（agent/session 不存在） | 路由级 NotFound 页 |

> 对标后端「error floor：SSE 异常兜底转 error 事件」—— 前端「error floor：任何异常都有可恢复 UI，不卡死 loading」。

---

## 13. 为后续能力留口（基于 plan 文档）

| 能力 | 来源 plan | 留口位置 | 触发方式 |
|---|---|---|---|
| **知识库（KB）混合接入** | `kb-hybrid-access-plan.md` | 聊天输入区「启用知识库」开关 + collection/version 下拉；`SendMessageRequest` 预留 `kb_enabled`/`kb_collections`/`kb_versions` 字段 | `GET /sessions/:id/kb-capability` 返回 `kb_available=true` 时渲染（后端未上线前隐藏） |
| **多模态回复（反向链路）** | attachment plan §十四 | 消息渲染层预留「非文本 content block」渲染分支 | 后端 agent_message content 由 str → list 时启用 |
| **更多文档类型（pdf/ppt/excel）** | attachment plan §十三 | 附件 `accept` 由 `allow_file` 动态拼接，非硬编码 | 后端 `allow_file` 数组扩展即自动跟进 |
| **KB 写入/索引管理面** | kb plan §十一 | 设置区预留 `/settings/kb` 路由占位 | 后端管理面 API 上线后填充 |
| **品牌定制升级** | — | UI 组件库替换口（AntD → shadcn/ui+Tailwind） | 整体迁移，不与 AntD 混用 |

> 留口原则：**能力驱动**（准则 F2）。任何新能力上线 = 新增一个 `*-capability` 查询 + 对应 UI 开关，不改动核心聊天状态机。

---

## 14. 开发与质量门禁

- `npm run lint`（ESLint，已就位）—— 提交前必过。
- `npm run build`（`tsc -b && vite build`）—— 类型零错误。
- **禁止**：`any`（必要时 `unknown` + 类型守卫）、`// @ts-ignore`、console.log 进提交、硬编码 `localhost:8000`。
- 环境：`.env.development`（`VITE_API_BASE=/api`）+ `.env.production`（`VITE_API_BASE=<prod>`）。
- Vite proxy（`vite.config.ts`）：`/api` → `http://localhost:8000`，`rewrite: /api → ''`。

---

## 15. 落地顺序建议（自洽依赖链）

1. **基建**：Vite proxy + alias + AntD ConfigProvider + AppShell 布局 + 路由骨架 + `api/client.ts` + `types/*`。
2. **模型管理**（`/settings/models`）：最简单 CRUD + `/test`，验证 API 层与 Query 模式。
3. **MCP 运维**（`/settings/mcp`）：CRUD + tools 列表 + status（为 agent 编辑器的工具多选供数）。
4. **技能管理**（`/settings/skills`）：multipart 上传模式验证。
5. **智能体编辑器**（`/agents/*`）：核心配置树 + validate + mermaid 预览（依赖 2/3 供数）。
6. **会话列表**（`/sessions`）：标准列表。
7. **聊天界面**（`/sessions/:id`）：SSE 状态机 + 消息流 + HITL（最复杂，放最后）。
8. **附件**（聊天内）：`attachment-capability` 驱动 + multipart 上传 + `upload_id` 串联。
9. **KB 留口**：仅占位，等后端 `kb-capability`。

---

## 附：与后端的契约对照表

| 前端行为 | 后端契约 | 备注 |
|---|---|---|
| 保存 agent 前预览 | `POST /agents/validate` → `{valid, errors, mermaid}` | 不落库 |
| 保存 agent | `POST /agents` / `PUT /agents/:id` → `AgentDetail`（含 `active_mermaid`） | 更新会级联删会话，需 UI 告知 |
| 还原配置 | `POST /agents/:id/restore` `{history_id}` | 先校验 model 可用（409 兜底） |
| 选工具 | `GET /mcp/tools` | 全量命名空间工具 |
| 选技能 | `GET /skills` | `/skills/<name>/` |
| 选模型 | `GET /models` | `supports_vision` 驱动附件路径 |
| 发消息 | `POST /sessions/:id/messages`（SSE） | 终止：done/interrupt/error |
| HITL 恢复 | `POST /sessions/:id/resume` `{decisions}`（SSE） | 非中断态返 409 |
| 上传附件 | `POST /sessions/:id/uploads`（multipart，只传 file+user_id） | mime 服务端判 |
| 附件能力 | `GET /sessions/:id/attachment-capability` | 驱动上传组件可见性 |
| 历史消息 | `GET /sessions/:id/history` → `MessageOut[]` | 空数组非错误 |
