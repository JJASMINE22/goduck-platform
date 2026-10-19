<p align="center">
  <img src="../logo.png" alt="goduck-platform" width="260">
</p>

<p align="center">frontend —— 智能体编排与会话的交互层（Web 前端）</p>

技术栈：**React 19 + TypeScript + Vite + Ant Design + Zustand + TanStack Query**，
集成 Mermaid / @xyflow 拓扑渲染、react-hook-form 表单与 SSE 流式对话。

> 完整本地 / 容器启动指引见[根 README](../README.md#quickstart)。

## 功能

| 模块 | 路由 | 说明 |
|---|---|---|
| 智能体列表 / 编辑器 | `/agents`、`/agents/:id` | AgentConfig 树形编辑、实时校验、Mermaid 委派拓扑预览、配置历史与一键还原 |
| 会话列表 / 聊天 | `/sessions`、`/sessions/:id` | SSE 流式聊天 + HITL（审批 / 编辑 / 拒绝 / 追加回复）+ TODO 计划面板 + 回合时间线（TurnRail）+ 文件变更徽标 |
| 附件 | 会话输入框 | 图片 / 文档上传 chip，能力驱动（`attachment-capability` 决定可见性与接受类型） |
| 知识库锚定 | 会话输入框 | 输入框左侧选择知识库，agent 先检索后作答 |
| 语音输入 | 会话输入框 | 长按麦克风说话，实时转写回填输入框，松开自动发送（`voice_enabled` 时显示） |
| 工作区面板 | 会话页右侧 | 用户隔离文件树、文本 / office Markdown 预览、下载、删除 |
| 模型管理 | `/settings/models` | 注册 / 编辑 / 删除 OpenAI 兼容模型，保存前测试连通性（`api_key` 永不回显） |
| MCP 管理 | `/settings/mcp` | MCP server 注册 / 启停、工具列表与健康检查 |
| 技能管理 | `/settings/skills` | multipart 上传 / 删除技能 |
| 知识库管理 | `/settings/knowledge` | 建库、pdf/docx 上传入库（状态机跟踪）、删除、重建索引、调试检索 |

**能力驱动 UI**：菜单、路由与页面入口由后端 `/health.capabilities`（kb_enabled /
voice_enabled）与各会话 capability 接口驱动——未启用的功能整个无痕隐藏，前端零配置。

## 环境变量

| 文件 | 用途 |
|---|---|
| `.env.development` | dev：`VITE_API_BASE=/api`，由 Vite proxy 转发到后端（见 `vite.config.ts`） |
| `.env.production` | 生产：`VITE_API_BASE=/api`（与边缘 nginx 的 /api 剥前缀转发配合，同源无 CORS） |

dev 期间通过 `vite.config.ts` 的 `server.proxy` 把 `/api/*`（含语音 WebSocket）转发到
`http://localhost:8000`，避开跨域。若后端不在 `localhost:8000`，可用 `VITE_API_TARGET`
环境变量覆盖代理目标：

```powershell
$env:VITE_API_TARGET="http://192.168.1.10:8000"; npm run dev
```

## 脚本

```powershell
npm install         # 安装依赖
npm run dev         # 启动开发服务器（默认 http://localhost:5173，监听所有网卡）
npm run build       # 类型检查 + 生产构建（输出到 dist/）
npm run typecheck   # tsc 类型检查（不产物）
npm run lint        # eslint
npm run preview     # 预览生产构建
```

## 目录结构

```
src/
├── api/            # API 客户端封装（client.ts 统一请求 + 各域接口：
│                   #   agents / sessions / stream / models / mcp / skills /
│                   #   uploads / knowledge / workspace / voice / health / shell）
├── components/     # 通用组件（layout / feedback）
├── features/       # 功能模块
│   ├── agent-editor/   # AgentConfig 树形编辑器
│   ├── chat/           # SSE 聊天 + HITL 卡片 + TodoPanel + TurnRail 时间线
│   │                   #   + MessageAttachments + FileChangesCard + ChatInput
│   ├── knowledge/      # 知识库管理（建库 / 文档入库状态 / 检索调试）
│   ├── topology/       # Mermaid / dagre-react 拓扑渲染
│   └── workspace/      # 工作区文件树 + 预览（WorkspaceView / WorkspaceFilePreview）
├── pages/          # 页面（agents / sessions / settings{models·mcp·skills·knowledge}）
├── store/          # zustand 状态（agentEditor 草稿 / chatStore 会话态）
├── hooks/          # TanStack Query hooks（useModels / useAgents / useStreamChat /
│                   #   useVoiceInput / useCapabilities ...）
├── lib/            # 工具（format / antd 静态桥接）
├── styles/         # antd 主题 / locale
├── types/          # 共享类型
└── App.tsx         # Provider 嵌套 + 路由
```

## 开发约定

- **API 基址**走 `VITE_API_BASE`，禁止硬编码 `localhost`。
- **错误归一**为 `ApiError`（`api/client.ts`），保留后端中文 detail 直接展示。
- 草稿状态（如智能体编辑器）由 zustand store 持有，避免组件卸载丢失。
- 流式事件（SSE 与语音 WS 隧道）共用同一套 dispatch；终止事件固定 `done` / `interrupt` / `error`。

## 依赖后端

前端无独立运行价值，需先启动 [后端](../backend/README.md)。完整本地启动流程见
[根 README](../README.md#quickstart)。
