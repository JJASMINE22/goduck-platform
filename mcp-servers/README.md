# mcp-servers

本地 MCP 服务集群 —— 为 goduck-platform 后端提供附件解析能力（图像 / 文档）。

通过 wrapper 模式封装第三方 MCP（如智谱视觉），对 LLM 只暴露干净的 `upload_id` 接口，
base64 / 临时文件路径 / 第三方协议细节全部隐藏在 wrapper 进程内。

## 目录结构

```
mcp-servers/
├── .env                  # 配置参数 + key（不入库）
├── mcp_servers.json      # 第三方 MCP 初始加载配置（${VAR} 从 .env 注入）
├── requirements.txt
├── main.py               # 入口
├── config.py             # .env + json 加载 + env 占位符展开
├── core/
│   └── server.py         # FastMCP 实例 + lifespan（MinIO + 第三方工具池）
├── routers/              # 各功能脚本（每个导出 register(mcp)）
│   ├── _common.py        # 共享工具：MinIO client / 临时文件 / MCP 结果打平
│   ├── image.py          # image_understand（图像解析）
│   └── document.py       # parse_document（docx 解析）
└── tmp/                  # 临时文件目录（第三方 MCP 读本地路径用）
```

## 设计要点

- **lifespan + Context 注入**：MinIO client、第三方工具池、tmp_dir 在启动时建立，
  yield 后注入每个工具调用的 `ctx.request_context.lifespan_context`，无全局变量。
- **路由模块化**：每个功能一个脚本，导出 `register(mcp)`，`core/server.py` 统一注册。
- **结果打平**：`flatten_mcp_text` 把内层 MCP content blocks 拆成纯字符串，
  避免 MCP-over-MCP 的双层嵌套泄漏给 LLM。
- **错误吞在 wrapper 内**：返回 `{"result":"error"}` 不 raise，配合 LangChain 错误回灌自修正。

## 安装

```powershell
cd mcp-servers
pip install -r requirements.txt
```

## 配置

1. 编辑 `.env`，填入 MinIO 凭证、智谱 API key 等
2. 编辑 `mcp_servers.json`，配置第三方 MCP（`env` 字段可用 `${VAR}` 引用 `.env` 变量）

## 启动

```powershell
python main.py
# 或: python -m core.server
```

默认监听 `0.0.0.0:8088`（在 `.env` 的 HOST/PORT 修改）。

## 注册到 goduck backend

在后端 `mcp_server` 表注册本服务：

```powershell
curl -X POST http://localhost:8000/mcp/servers -H "Content-Type: application/json" `
  -d '{\"name\":\"attachment_wrapper\",\"kind\":\"http\",\"config\":{\"url\":\"http://127.0.0.1:8088/mcp\"},\"enabled\":true}'
```

工具命名空间化后为：
- `attachment_wrapper__image_understand`
- `attachment_wrapper__parse_document`

## 扩展新功能

1. 在 `routers/` 新建脚本，实现工具并导出 `register(mcp)`
2. 在 `core/server.py` import 并调用 `xxx_router.register(mcp)`
3. 如需第三方 MCP，在 `mcp_servers.json` 添加配置

## 暂不支持

- `.doc`（老版二进制，python-docx 不支持）
- pdf / ppt / excel（后续扩展）
- agent 生成多模态回复的反向链路
