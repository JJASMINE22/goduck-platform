# goduck-platform Frontend

goduck-platform 的 Web 前端 —— 智能体编排与会话的交互层。

技术栈：**React 19 + TypeScript + Vite + Ant Design + Zustand + TanStack Query**，
集成 Mermaid 拓扑渲染、react-hook-form 表单与 SSE 流式对话。

## 功能

- **智能体编辑器**（`/agents/*`）：AgentConfig 树形编辑、实时校验、Mermaid 委派拓扑预览、配置历史与还原。
- **模型管理**（`/settings/models`）：注册 / 编辑 / 删除 OpenAI 兼容模型，保存前测试连通性（`api_key` 永不回显）。
- **MCP 管理**（`/settings/mcp`）：MCP server 注册 / 启停、工具列表与健康检查。
- **技能管理**（`/settings/skills`）：multipart 上传 / 删除技能。
- **会话与对话**（`/sessions/*`）：会话列表 + SSE 流式聊天 + HITL（审批 / 编辑 / 拒绝 / 追加回复）+ 附件上传。

## 环境变量

| 文件 | 用途 |
|---|---|
| `.env.development` | dev：`VITE_API_BASE=/api`，由 Vite proxy 转发到后端（见 `vite.config.ts`） |
| `.env.production` | 生产：填入后端真实地址 |

dev 期间通过 `vite.config.ts` 的 `server.proxy` 把 `/api/*` 转发到 `http://localhost:8000`，避开跨域。
若后端不在 `localhost:8000`，可用 `VITE_API_TARGET` 环境变量覆盖代理目标：

```powershell
$env:VITE_API_TARGET="http://192.168.1.10:8000"; npm run dev
```

## 脚本

```powershell
npm install         # 安装依赖
npm run dev         # 启动开发服务器（默认 http://localhost:5173）
npm run build       # 类型检查 + 生产构建（输出到 dist/）
npm run typecheck   # tsc 类型检查（不产物）
npm run lint        # eslint
npm run preview     # 预览生产构建
```

## 目录结构

```
src/
├── api/            # API 客户端封装（client.ts 统一请求 + 各域接口）
├── components/     # 通用组件（layout / feedback）
├── features/       # 功能模块
│   ├── agent-editor/   # AgentConfig 树形编辑器
│   ├── chat/           # SSE 聊天 + HITL
│   └── topology/       # Mermaid 渲染
├── pages/          # 页面（agents / sessions / settings）
├── store/          # zustand 状态（agentEditor 草稿等）
├── hooks/          # TanStack Query hooks（useModels / useAgents ...）
├── lib/            # 工具（format / antd 静态桥接）
├── types/          # 共享类型
└── App.tsx         # Provider 嵌套 + 路由
```

## 开发约定

- **API 基址**走 `VITE_API_BASE`，禁止硬编码 `localhost`。
- **错误归一**为 `ApiError`（`api/client.ts`），保留后端中文 detail 直接展示。
- 草稿状态（如智能体编辑器）由 zustand store 持有，避免组件卸载丢失。

## 依赖后端

前端无独立运行价值，需先启动 [后端](../backend/README.md)。完整本地启动流程见
[根 README](../README.md#快速开始本地)。
