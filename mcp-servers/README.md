<p align="center">
  <img src="../logo.png" alt="goduck-platform logo" width="150">
</p>

<h1 align="center">mcp-servers</h1>

<p align="center">本地 MCP 服务集群 —— 为 goduck-platform 后端提供 <b>知识库检索</b> 能力</p>

> **附件解析已迁移**（2026-08）：图像描述与文档转 Markdown 不再经 MCP 工具，
> 由 backend 在消息发送时确定性预消费并组装进 content（见 backend
> `app/services/content_composer.py` + `vision_describer.py`）。原
> `image_understand` / `parse_document` wrapper 及第三方视觉 MCP 桥接退役。

## 目录结构

```
mcp-servers/
├── .env / .env.example      # 配置参数 + key（不入库）
├── mcp_servers.json         # 第三方 MCP 占位（当前为空；桥接已退役）
├── requirements.txt
├── main.py                  # 入口
├── config.py                # .env 加载（ES / 向量化 / 端口）
├── core/
│   └── server.py            # FastMCP 实例 + lifespan（向量检索工厂）
├── routers/                 # 各功能脚本（每个导出 register(mcp)）
│   └── knowledge.py         # kb_search（知识库向量检索，ES 直连）
└── services/
    ├── embedding.py         # 向量化客户端（OpenAI 兼容端点，与 backend 同构）
    └── vector_store.py      # ES 向量库工厂（按 kb 索引组合懒构建缓存）
```

## 设计要点

- **lifespan + Context 注入**：向量检索工厂在启动时建立，yield 后注入每个
  工具调用的 `ctx.request_context.lifespan_context`，无全局变量。
- **路由模块化**：每个功能一个脚本，导出 `register(mcp)`，`core/server.py` 统一注册。
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

编辑 `.env`，填入 ES 地址、向量化端点。
**知识库相关项（ES_URL / EMBEDDING_* / KB_TOP_K）必须与 backend/.env 严格一致**，
尤其是 `EMBEDDING_DIMS` —— 入库（backend）与检索（本服务）必须维度对称，否则检索失败。

## 启动

```powershell
python main.py
# 或: python -m core.server
```

默认监听 `0.0.0.0:8088`（在 `.env` 的 HOST/PORT 修改）。

## 注册到 goduck backend

在界面 `/settings/mcp` 注册，或调 API（写入后端 `mcp_server` 表，重启自动加载）。
工具命名空间化后为 `super-goduck__kb_search` —— 知识库检索（会话锚定 KB 时 agent 自动调用）。

## 扩展新功能

1. 在 `routers/` 新建脚本，实现工具并导出 `register(mcp)`
2. 在 `core/server.py` import 并调用 `xxx_router.register(mcp)`
3. 如需向量检索，复用 `services/` 的工厂
