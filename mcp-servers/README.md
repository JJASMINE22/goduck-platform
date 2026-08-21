# mcp-servers

本地 MCP 服务集群 —— 为 goduck-platform 后端提供**附件解析**与**知识库检索**能力。

通过 wrapper 模式封装第三方 MCP（如智谱视觉），对 LLM 只暴露干净的 `upload_id` 接口，
base64 / 临时文件路径 / 第三方协议细节全部隐藏在 wrapper 进程内。

## 目录结构

```
mcp-servers/
├── .env / .env.example      # 配置参数 + key（不入库）
├── mcp_servers.json         # 第三方 MCP 初始加载配置（${VAR} 从 .env 注入）
├── requirements.txt
├── main.py                  # 入口
├── config.py                # .env + json 加载 + env 占位符展开
├── core/
│   └── server.py            # FastMCP 实例 + lifespan（MinIO + 第三方工具池 + 向量检索工厂）
├── routers/                 # 各功能脚本（每个导出 register(mcp)）
│   ├── _common.py           # 共享工具：MinIO client / 临时文件 / MCP 结果打平
│   ├── image.py             # image_understand（图像解析）
│   ├── document.py          # parse_document（docx 解析）
│   └── knowledge.py         # kb_search（知识库向量检索，ES 直连）
├── services/
│   ├── embedding.py         # 向量化客户端（OpenAI 兼容端点，与 backend 同构）
│   └── vector_store.py      # ES 向量库工厂（按 kb 索引组合懒构建缓存）
└── tmp/                     # 临时文件目录（第三方 MCP 读本地路径用）
```

## 设计要点

- **lifespan + Context 注入**：MinIO client、第三方工具池、向量检索工厂、tmp_dir 在启动时建立，
  yield 后注入每个工具调用的 `ctx.request_context.lifespan_context`，无全局变量。
- **路由模块化**：每个功能一个脚本，导出 `register(mcp)`，`core/server.py` 统一注册。
- **结果打平**：`flatten_mcp_text` 把内层 MCP content blocks 拆成纯字符串，
  避免 MCP-over-MCP 的双层嵌套泄漏给 LLM。
- **错误吞在 wrapper 内**：返回 `{"result":"error"}` 不 raise，配合 LangChain 错误回灌自修正。
- **kb_search JIT 检索链路**：`kb_ids → kb_<id>` 逗号拼接索引名 → 按索引组合懒构建并缓存
  ES store，全链路 ES 直连、无 backend HTTP 跳。工具名固定 `kb_search`，注册后命名空间为
  `{server}__kb_search`，正好命中 backend 的 KB 锚点扫描（会话锚定的库通过该工具下发）。

## 安装

```powershell
cd mcp-servers
copy .env.example .env       # Linux/macOS 用 cp
pip install -r requirements.txt
```

## 配置

1. 编辑 `.env`，填入 MinIO 凭证、智谱 API key、ES 地址、向量化端点。
   **知识库相关项（ES_URL / EMBEDDING_* / KB_TOP_K）必须与 backend/.env 严格一致**，
   尤其是 `EMBEDDING_DIMS` —— 入库（backend）与检索（本服务）必须维度对称，否则检索失败。
2. 编辑 `mcp_servers.json`，配置第三方 MCP（`env` 字段可用 `${VAR}` 引用 `.env` 变量）。

## 启动

```powershell
python main.py
# 或: python -m core.server
```

默认监听 `0.0.0.0:8088`（在 `.env` 的 HOST/PORT 修改）。

## 注册到 goduck backend

在界面 `/settings/mcp` 注册，或调 API（写入后端 `mcp_server` 表，重启自动加载）：

```powershell
curl -X POST http://localhost:8000/mcp/servers -H "Content-Type: application/json" `
  -d '{\"name\":\"attachment_wrapper\",\"kind\":\"http\",\"config\":{\"url\":\"http://127.0.0.1:8088/mcp\"},\"enabled\":true}'
```

工具命名空间化后为：

- `attachment_wrapper__image_understand` —— 图像解析（附件消息自动路由）
- `attachment_wrapper__parse_document` —— docx 文档解析
- `attachment_wrapper__kb_search` —— 知识库检索（会话锚定 KB 时 agent 自动调用）

## 扩展新功能

1. 在 `routers/` 新建脚本，实现工具并导出 `register(mcp)`
2. 在 `core/server.py` import 并调用 `xxx_router.register(mcp)`
3. 如需第三方 MCP，在 `mcp_servers.json` 添加配置；如需向量检索，复用 `services/` 的工厂

## 暂不支持

- `.doc`（老版二进制，python-docx 不支持）
- pdf / ppt / excel 解析（知识库入库侧 backend 已支持 pdf/docx；wrapper 侧后续扩展）
- agent 生成多模态回复的反向链路
