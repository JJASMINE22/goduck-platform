"""psycopg3-direct persistence layer.

Shares the SAME `AsyncConnectionPool` as the LangGraph checkpointer / store.
Business tables: agent / agent_config_history / session / mcp_server.
LangGraph-native tables (checkpoints / checkpoint_writes / checkpoint_blobs / store)
are created by AsyncPostgresSaver/Store `.setup()`.

Design (plan §1, 方案 B; 会话-智能体解耦后修订，见 .kilo/plans/1790660700000):
- agent.id 全程稳定不变; 更新 = in-place 覆写 config_json + 追加 agent_config_history 快照。
- 会话-智能体解耦：session.last_agent_id 只是"最近使用的智能体"（send 时写入），
  不再构成绑定。更新 agent 不再归档其会话（同 schema 工厂下旧 checkpoint 可安全
  上新图）；删除 agent 不再级联删会话（last_agent_id 经 FK SET NULL 置空，
  会话与 checkpoint 存活，进入后选择新智能体即可继续）。
  agent_config_history 无 FK（审计线索独立于 agent 生命周期，对齐 jev_decision）。

Notes:
- psycopg3 uses `%s` placeholders (NOT asyncpg's `$1`).
- Native JSON/JSONB adaptation — pass python dict directly, no json.dumps.
- All multi-step ops run inside explicit transactions.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Json
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)

# LangGraph AsyncPostgresSaver default table names (cleaned on agent update).
_CHECKPOINT_TABLES = ("checkpoints", "checkpoint_writes", "checkpoint_blobs")

# ---------------------------------------------------------------------------
# DDL — business tables only (idempotent)
# ---------------------------------------------------------------------------
BUSINESS_DDL = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS model (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR NOT NULL UNIQUE,
    provider_kind   VARCHAR NOT NULL DEFAULT 'openai_compatible',
    model_type      TEXT NOT NULL,
    api_key         TEXT NOT NULL,
    base_url        TEXT,
    supports_vision BOOLEAN NOT NULL DEFAULT false,
    temperature     REAL,                                  -- 可空：None=用 provider 默认
    top_p           REAL,                                  -- 可空：None=用 provider 默认
    enable_thinking BOOLEAN,                               -- 三态：NULL=不注入(provider默认)/true=enabled/false=disabled
    enabled         BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================ 向量模型（embedding）============================
-- 知识库 RAG 嵌入端点注册（独立于 model/LLM，一表一域）。语义字段被 KB
-- 引用后不可变（service 守卫）：静默换端点/维度会让既有索引向量空间失配。
CREATE TABLE IF NOT EXISTS embedding (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name              VARCHAR NOT NULL UNIQUE,
    provider_kind     VARCHAR NOT NULL DEFAULT 'openai_compatible',
    model_type        TEXT NOT NULL,                  -- 发往端点的 model 名（TEI 忽略；云端须真实型号）
    api_key           TEXT NOT NULL,                  -- TEI 用 dummy
    base_url          TEXT NOT NULL,                  -- OpenAI 兼容端点（含 /v1；compose 内 TEI=http://tei:8082/v1）
    dims              INT,                            -- MRL 截断维度；NULL=模型原生
    batch_size        INT NOT NULL DEFAULT 24,        -- 单请求文本数（须 ≤ TEI --max-client-batch-size）
    timeout           INT NOT NULL DEFAULT 90,        -- 单批请求超时（秒）
    check_ctx_length  BOOLEAN NOT NULL DEFAULT false, -- TEI 必须 false（tiktoken 规避）
    enabled           BOOLEAN NOT NULL DEFAULT true,
    creator           VARCHAR NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================ 用户与登录（auth）============================
-- app_user：当前生效事实（pending/rejected 只活在 auth_request，不进本表）。
-- username=登录锚点（不可变，唯一性按 casefold 比较）；nickname=纯展示名
-- （即时查重+黑名单，可改）。业务表 user_id 一律 FK 到本表 uuid——身份与
-- 展示解耦，改名零级联。
CREATE TABLE IF NOT EXISTS app_user (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username            VARCHAR NOT NULL,
    nickname            VARCHAR NOT NULL,
    password_hash       TEXT    NOT NULL,                   -- argon2id，明文永不落库
    is_manager          BOOLEAN NOT NULL DEFAULT false,
    state               VARCHAR NOT NULL DEFAULT 'active',  -- active | disabled
    failed_attempts     INT     NOT NULL DEFAULT 0,         -- 连续登录失败（成功即清零）
    locked_until        TIMESTAMPTZ,                        -- 爆破锁退避（NULL=未锁）
    last_login_at       TIMESTAMPTZ,
    password_updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_app_user_username ON app_user(lower(username));
CREATE UNIQUE INDEX IF NOT EXISTS idx_app_user_nickname ON app_user(lower(nickname));

-- 注册申请（审批制）：批准 = 原子建户；驳回 = no-op。rejected 行保留 =
-- 用户名占位（防骚扰重放），需重新开放由管理员删除申请行。
CREATE TABLE IF NOT EXISTS auth_request (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    type        VARCHAR NOT NULL DEFAULT 'register',        -- 当前仅 register（泛化预留）
    payload     JSONB   NOT NULL,                           -- {username, nickname, password_hash}
    status      VARCHAR NOT NULL DEFAULT 'pending',         -- pending | approved | rejected
    reason      TEXT,                                       -- 驳回原因（可选）
    reviewed_by UUID REFERENCES app_user(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_auth_request_status ON auth_request(status, created_at DESC);

-- 登录会话令牌：服务端会话表（登出/停用/改密 = 删行即时吊销）。cookie 只存
-- 原始 token，库内存 sha256 哈希——库泄露不可逆推 token。
CREATE TABLE IF NOT EXISTS auth_session (
    token_hash   VARCHAR(64) PRIMARY KEY,                   -- sha256 hex
    user_id      UUID NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_auth_session_user ON auth_session(user_id);

CREATE TABLE IF NOT EXISTS agent (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR NOT NULL UNIQUE,
    description     TEXT,
    config_json     JSONB   NOT NULL,
    model_id        UUID,
    vision_model_id UUID,                                  -- 下挂视觉模型（基座无视觉时的图像预消费）
    -- 用途类型：'chat'=聊天会话 | 'cron'=定时任务专用。创建后不可变（方案 A）；
    -- 转换走导出 YAML 重建。约束会话/任务两侧的绑定边界（session/cron_job 创建时校验）。
    kind            VARCHAR NOT NULL DEFAULT 'chat',
    status          VARCHAR NOT NULL DEFAULT 'active',
    creator         VARCHAR NOT NULL,
    updater         VARCHAR,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_config_history (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- 无 FK：会话比 agent 长寿后，历史快照是"当时跑在什么配置上"的审计线索
    -- （对齐 jev_decision 的无 FK 设计）；agent 删除后行保留，agent_id 悬空无害。
    agent_id        UUID NOT NULL,
    config_json     JSONB   NOT NULL,
    model_id        UUID,
    vision_model_id UUID,
    action          VARCHAR NOT NULL,
    operator        VARCHAR NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ach_agent ON agent_config_history(agent_id, created_at DESC);

CREATE TABLE IF NOT EXISTS session (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- 最近使用的智能体（会话-智能体解耦）：send 时写入，非绑定关系。
    -- NULL=创建后尚未选择/发送；agent 删除时 SET NULL，会话与 checkpoint 存活。
    last_agent_id UUID REFERENCES agent(id) ON DELETE SET NULL,
    -- 归属用户（FK app_user；身份由登录令牌推导，客户端不再传 user_id）
    user_id       UUID NOT NULL REFERENCES app_user(id),
    thread_id     VARCHAR NOT NULL UNIQUE,
    title         TEXT,
    status        VARCHAR NOT NULL DEFAULT 'active',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- 索引名保留 idx_session_agent（迁移安装下随列改名自动跟随，避免改名竞态）；
-- 其建语句在 _MIGRATIONS（列改名之后）执行——存量安装在此刻列名仍为 agent_id，
-- 提前到 DDL 会因列不存在而启动失败。
CREATE INDEX IF NOT EXISTS idx_session_user  ON session(user_id);

CREATE TABLE IF NOT EXISTS mcp_server (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name         VARCHAR NOT NULL UNIQUE,
    kind         VARCHAR NOT NULL,
    config_json  JSONB   NOT NULL,
    enabled      BOOLEAN NOT NULL DEFAULT true,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS upload (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id    UUID NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    user_id       UUID NOT NULL REFERENCES app_user(id),
    bucket_key    TEXT NOT NULL,
    filename      TEXT NOT NULL,
    mime          TEXT NOT NULL,
    kind          TEXT NOT NULL,
    size          BIGINT NOT NULL,
    page_count    INT,
    preview       TEXT,
    -- 图像描述缓存（vision_model 预消费结果；固定预 prompt 模板 ⇒ 同图跨轮复用）
    vision_desc        TEXT,
    vision_desc_model  TEXT,
    vision_desc_at     TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_upload_session ON upload(session_id);
CREATE INDEX IF NOT EXISTS idx_upload_user ON upload(user_id);

-- ============================ 定时任务（cron） ============================
-- DB 为唯一真相源：scheduler 每 tick 轮询 due 行，无内存注册表。
-- session 侧经 kind='cron' 鉴别（见 _MIGRATIONS），1:1 绑定任务专用会话。
CREATE TABLE IF NOT EXISTS cron_job (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES app_user(id),
    agent_id            UUID NOT NULL REFERENCES agent(id) ON DELETE CASCADE,
    -- 专用任务会话（session.kind='cron'）。不设硬 FK：session 已由 agent 级联管
    -- 理，交叉 FK 会引入级联顺序脆弱性；删除路径由 API 层守卫（kind='cron' 拒删）。
    session_id          UUID NOT NULL,
    name                VARCHAR NOT NULL,
    prompt              TEXT NOT NULL,
    interval_minutes    INT NOT NULL CHECK (interval_minutes >= 1),
    start_at            TIMESTAMPTZ NOT NULL DEFAULT now(),   -- 首跑锚点（interval 相位锚）
    enabled             BOOLEAN NOT NULL DEFAULT true,
    next_run_at         TIMESTAMPTZ,                          -- NULL=暂停/完结；认领时原子推进
    last_run_at         TIMESTAMPTZ,
    last_status         VARCHAR,     -- running|ok|error|awaiting_approval
    last_error          TEXT,
    consecutive_failures INT NOT NULL DEFAULT 0,              -- 熔断计数（连续 error）
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, name)
);
CREATE INDEX IF NOT EXISTS idx_cron_job_due ON cron_job(next_run_at) WHERE enabled;

CREATE TABLE IF NOT EXISTS cron_run (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id      UUID NOT NULL REFERENCES cron_job(id) ON DELETE CASCADE,
    status      VARCHAR NOT NULL DEFAULT 'running',  -- running|ok|error|awaiting_approval
    trigger     VARCHAR NOT NULL DEFAULT 'scheduled',-- scheduled|manual
    error       TEXT,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_cron_run_job ON cron_run(job_id, started_at DESC);

-- ============================ 知识库（RAG）状态卡 ============================
-- 仅状态/元数据；chunk 文本/向量真值入 Elasticsearch（index = kb_<kb_id>）。
CREATE TABLE IF NOT EXISTS knowledge_base (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name          VARCHAR NOT NULL UNIQUE,
    description   TEXT,
    es_index_name VARCHAR NOT NULL,          -- 'kb_<uuid>'，创建时写定
    dims          INT,                        -- 嵌入维度（显示/漂移检测）：绑定库 = embedding 行同值投影；
                                                -- 未绑定（迁移期）= 全局 EMBEDDING_DIMS（legacy 路径）
    embedding_id  UUID,                       -- 绑定向量模型（embedding 表；无 FK，应用层守卫）。
                                                -- NULL = legacy（dims + env 双读回落，plan
                                                -- kb-embedding-model-registry Phase 1）
    status        VARCHAR NOT NULL DEFAULT 'active',
    creator       VARCHAR NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS kb_document (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kb_id        UUID NOT NULL REFERENCES knowledge_base(id) ON DELETE CASCADE,
    bucket_key   TEXT,                        -- MinIO 对象键；KB_STORE_MINIO=false 时为 NULL
    filename     TEXT NOT NULL,
    mime         TEXT NOT NULL,
    size         BIGINT NOT NULL,
    page_count   INT,
    status       VARCHAR NOT NULL DEFAULT 'pending',  -- pending|indexing|indexed|failed
    chunk_count  INT,
    progress     INT,                                  -- 入库进度 0..100（indexing 期间随批推进；indexed=100）
    chunk_size   INT,                                  -- 解析切块参数（生效值，解析触发时写入；NULL=未解析/历史行→用全局默认）
    chunk_overlap INT,                                 -- 同上
    error        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_kbdoc_kb ON kb_document(kb_id);

-- ============================ Jev 判决审计（shell 守卫 + rubric 门控）============================
-- append-only：每条 Jev 判决一行（含调用失败行）。Jev 无 rationale 输出，此表是
-- 唯一审计与灰度标定数据源（分歧样本 = 判决与人工决议/LLM verdict 相左的行）。
-- decision_type 区分两类判决：shell_guard（每条非白名单 execute 命令一行）/
-- rubric（每轮 rubric 门控一行，command 为空、tool_call_id 承载判决键
-- rubric:{grading_run_id}:{iteration}）。增长可控，暂不做清理任务，按需归档。
CREATE TABLE IF NOT EXISTS jev_decision (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 可空且不设 FK 级联：审计数据独立于会话生命周期（会话删除后审计仍可追溯）
    session_id      UUID,
    thread_id       VARCHAR NOT NULL,
    agent_id        UUID NOT NULL,
    tool_call_id    VARCHAR NOT NULL,               -- shell: execute 调用 id；rubric: 判决键
    decision_type   VARCHAR NOT NULL DEFAULT 'shell_guard',  -- shell_guard | rubric
    mode            VARCHAR NOT NULL,               -- shadow | enforce | triage
    source          VARCHAR NOT NULL,               -- rule_* | jev | unavailable
    verdict         VARCHAR NOT NULL,               -- auto_run | ask_human | auto_pass | ask_llm
    command         TEXT,                           -- shell 守卫的被判命令；rubric 判决无命令（NULL）
    state_rendered  TEXT,                           -- 实际发送的 state（截断后）
    answers         JSONB,                          -- Jev 原始返回（概率+置信度+usage）
    latency_ms      INT,
    model           VARCHAR
);
CREATE INDEX IF NOT EXISTS idx_jev_decision_agent ON jev_decision(agent_id, created_at DESC);

-- ============================ 会话排队发送（queued send）============================
-- queued_message 是候客厅而非消息存储：直发路径不落本表，消息真相源恒为
-- checkpointer 历史。sent 行仅审计（GET /queue 不返回）；failed 行带 fail_reason。
CREATE TABLE IF NOT EXISTS queued_message (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id  UUID NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    user_id     UUID NOT NULL,
    agent_id    UUID NOT NULL,                      -- 入队时解析的意图智能体（发送时才写 last_agent_id）
    content     TEXT NOT NULL,
    rubric      TEXT,                               -- 评审提示（与 SendMessageRequest.rubric 对齐）
    upload_ids  JSONB NOT NULL DEFAULT '[]',
    kb_ids      JSONB NOT NULL DEFAULT '[]',
    status      VARCHAR NOT NULL DEFAULT 'pending', -- pending|sending|sent|failed|canceled
    fail_reason TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_queued_message_session
    ON queued_message(session_id, status, created_at);

-- 会话队列暂停态（持久化：error/withdrawn/restart 挂起跨重启保留；行缺失 = active）
CREATE TABLE IF NOT EXISTS queue_state (
    session_id    UUID PRIMARY KEY REFERENCES session(id) ON DELETE CASCADE,
    paused_reason VARCHAR NOT NULL,                 -- error | withdrawn | restart
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================ 工作区文件变更明细（file_change 落库）============================
-- SSE file_change 事件的持久镜像：刷新/重进会话后明细回放的数据源（撤销基座同表，
-- 撤销本身另行评审）。anchor_message_id = 回合首条 HumanMessage id：回撤按锚精确
-- 删行，与 checkpointer 的 RemoveMessage 软删语义一致；run_token 供按 run 归因。
-- diff_text NULL = 二进制/超大/超限降级（仅计数）。append-only，随会话级联删除。
CREATE TABLE IF NOT EXISTS file_change_log (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id        UUID NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    anchor_message_id VARCHAR NOT NULL,
    run_token         VARCHAR NOT NULL,
    path              TEXT NOT NULL,
    action            VARCHAR NOT NULL,             -- create | edit | delete
    added             INT,
    removed           INT,
    diff_text         TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_file_change_log_session ON file_change_log(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_file_change_log_anchor ON file_change_log(session_id, anchor_message_id);
"""

# session.agent_id → last_agent_id 一次性物理改名（幂等：旧列不存在即跳过；
# 新装库 DDL 直接建 last_agent_id，不经过本迁移）。改名后 FK/索引自动跟随列，
# 约束名不变（见 _SESSION_FK_SET_NULL_MIGRATION）。
_SESSION_AGENT_RENAME_MIGRATION = """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'session' AND column_name = 'agent_id'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'session' AND column_name = 'last_agent_id'
    ) THEN
        ALTER TABLE session RENAME COLUMN agent_id TO last_agent_id;
    END IF;
END
$$;
"""


# session FK 行为切换：ON DELETE CASCADE → ON DELETE SET NULL（会话比 agent 长寿）。
# 列改名后旧约束仍以旧名 session_agent_id_fkey 存在（约束名不随列改名），
# 先 DROP 旧约束再以新名重建；存量行均有有效 agent 引用，验证必然通过。
# 幂等：目标约束名已存在（迁移已跑过 / 新装 DDL 已建）即跳过。
_SESSION_FK_SET_NULL_MIGRATION = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'session_last_agent_id_fkey'
    ) THEN
        ALTER TABLE session DROP CONSTRAINT IF EXISTS session_agent_id_fkey;
        ALTER TABLE session
            ADD CONSTRAINT session_last_agent_id_fkey
            FOREIGN KEY (last_agent_id) REFERENCES agent(id) ON DELETE SET NULL;
    END IF;
END
$$;
"""


# Idempotent column migrations for pre-existing tables (CREATE TABLE IF NOT
# EXISTS won't add columns to already-present tables). Each is guarded so it's
# safe to run repeatedly.
_MIGRATIONS = [
    "ALTER TABLE agent ADD COLUMN IF NOT EXISTS model_id UUID",
    "ALTER TABLE agent ADD COLUMN IF NOT EXISTS vision_model_id UUID",
    "ALTER TABLE agent_config_history ADD COLUMN IF NOT EXISTS model_id UUID",
    "ALTER TABLE agent_config_history ADD COLUMN IF NOT EXISTS vision_model_id UUID",
    "ALTER TABLE model ADD COLUMN IF NOT EXISTS temperature REAL",
    "ALTER TABLE model ADD COLUMN IF NOT EXISTS top_p REAL",
    "ALTER TABLE model ADD COLUMN IF NOT EXISTS enable_thinking BOOLEAN",
    "ALTER TABLE kb_document ADD COLUMN IF NOT EXISTS progress INT",
    "ALTER TABLE kb_document ADD COLUMN IF NOT EXISTS chunk_size INT",
    "ALTER TABLE kb_document ADD COLUMN IF NOT EXISTS chunk_overlap INT",
    # docling 路径（plan: kb-docling-parse-display）：kb-src 归档键内嵌版本
    # （v{doc_version}；当前恒 1，语义扩展点="文档或转换器版本变更"）与
    # 产出当前 chunk 的解析引擎（NULL=legacy/历史行——数据双轨期判别源）。
    "ALTER TABLE kb_document ADD COLUMN IF NOT EXISTS doc_version INT NOT NULL DEFAULT 1",
    "ALTER TABLE kb_document ADD COLUMN IF NOT EXISTS parser_engine VARCHAR",
    # 向量模型注册化（plan: kb-embedding-model-registry）：embedding 表见 DDL；
    # 存量库 embedding_id 回填由启动 seed 流程完成（main lifespan，非 SQL 迁移）。
    "ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS embedding_id UUID",
    "ALTER TABLE upload ADD COLUMN IF NOT EXISTS vision_desc TEXT",
    "ALTER TABLE upload ADD COLUMN IF NOT EXISTS vision_desc_model TEXT",
    "ALTER TABLE upload ADD COLUMN IF NOT EXISTS vision_desc_at TIMESTAMPTZ",
    # 会话级 token 用量累计（jsonb，固定五键，见 add_session_usage）
    "ALTER TABLE session ADD COLUMN IF NOT EXISTS usage_totals JSONB",
    # 会话类型鉴别：'chat'=普通会话（默认，存量行自动归一） | 'cron'=定时任务专用会话
    # （由 POST /cron/jobs 内部代建，消息/回撤接口对其 409，仅允许 HITL 审批 resume）
    "ALTER TABLE session ADD COLUMN IF NOT EXISTS kind VARCHAR NOT NULL DEFAULT 'chat'",
    # 智能体用途类型（'chat' | 'cron'，创建后不可变）；存量智能体归一为 chat
    "ALTER TABLE agent ADD COLUMN IF NOT EXISTS kind VARCHAR NOT NULL DEFAULT 'chat'",
    # jev_decision 双用途化（rubric 门控，plan: jev-rubric-grader-gate）：
    # decision_type 鉴别判决来源；command 放开 NOT NULL（rubric 判决无命令）。
    # DROP NOT NULL 天然幂等，重复执行无害。
    "ALTER TABLE jev_decision ADD COLUMN IF NOT EXISTS decision_type VARCHAR NOT NULL DEFAULT 'shell_guard'",
    "ALTER TABLE jev_decision ALTER COLUMN command DROP NOT NULL",
    # 会话-智能体解耦（plan: 1790660700000 §8/§10.3）：agent_id → last_agent_id
    # 语义迁移。列物理改名（存量安装执行一次；新装 DDL 直接建 last_agent_id，
    # 本块探测 agent_id 不存在即跳过）。改名后 FK/索引自动跟随列。
    _SESSION_AGENT_RENAME_MIGRATION,
    "ALTER TABLE session ALTER COLUMN last_agent_id DROP NOT NULL",
    # FK 行为切换：CASCADE（删 agent 级联删会话）→ SET NULL（会话比 agent 长寿）。
    _SESSION_FK_SET_NULL_MIGRATION,
    # 索引随列就位（新装在此创建；迁移安装已随列改名持有同名索引，IF NOT EXISTS 跳过）
    "CREATE INDEX IF NOT EXISTS idx_session_agent ON session(last_agent_id)",
    # agent_config_history 拆除 FK（历史快照保留为审计线索，行独立于 agent 生命周期）
    "ALTER TABLE agent_config_history DROP CONSTRAINT IF EXISTS agent_config_history_agent_id_fkey",
]

# enable_thinking 三态化一次性迁移：旧 schema 为 NOT NULL 且旧语义 false=不注入；
# 新语义需要 false=显式关闭（disabled），与旧 false 语义冲突，须把存量 false 归一为
# NULL。以"列仍为 NOT NULL"作为仅首次执行的探测器（此后列已可空，用户显式写入的
# false=disabled 不会被再次改写）。
_THINKING_TRI_STATE_MIGRATION = """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'model' AND column_name = 'enable_thinking' AND is_nullable = 'NO'
    ) THEN
        ALTER TABLE model ALTER COLUMN enable_thinking DROP NOT NULL;
        UPDATE model SET enable_thinking = NULL WHERE enable_thinking = false;
    END IF;
END
$$;
"""


async def init_business_schema(pool: AsyncConnectionPool) -> None:
    """Create business tables if absent + run column migrations. Safe to repeat."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            # 连接池 prepare_threshold=0 会强制预编译，而预编译语句不支持一次执行多条
            # SQL；故把 BUSINESS_DDL 按 ';' 拆成单条逐条执行（DDL 内无字面量分号，安全）。
            for stmt in BUSINESS_DDL.split(";"):
                stmt = stmt.strip()
                if stmt:
                    await cur.execute(stmt)
            for stmt in _MIGRATIONS:
                await cur.execute(stmt)
            # DO $$..$$ 为单条语句（内部分号在 dollar-quote 内），不走 ';' 拆分
            await cur.execute(_THINKING_TRI_STATE_MIGRATION)
    logger.info("business schema ready (model / agent / agent_config_history / session / mcp_server / upload / knowledge_base / kb_document)")

    # kb_document 同名拦截的并发兜底：加 UNIQUE(kb_id, filename)。先清理历史重复行
    # （保留最新），再加唯一索引；任一步失败仅告警，不阻断启动——应用层查询仍为主防线。
    try:
        async with pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM kb_document a USING kb_document b "
                        "WHERE a.kb_id = b.kb_id AND a.filename = b.filename "
                        "AND (a.created_at, a.id) < (b.created_at, b.id)"
                    )
                    await cur.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS idx_kbdoc_kb_filename "
                        "ON kb_document(kb_id, filename)"
                    )
    except Exception:  # noqa: BLE001
        logger.exception("ensure kb_document(kb_id, filename) unique index failed (legacy dupes?)")

    # Session same-user same-title deduplication concurrency fallback: partial unique index (only active + non-empty title participates in deduplication,
    # archived sessions exempt → archiving releases the title). First clean up historically duplicated
    # active rows (keep the latest for same user same title), then add the index; on failure only warn without blocking startup.
    # 注意：未来若增加「取消归档」端点，须做同名预检（索引不会自动放行 re-active）。
    try:
        async with pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM session a USING session b "
                        "WHERE a.user_id = b.user_id AND a.title = b.title "
                        "AND a.status = 'active' AND b.status = 'active' "
                        "AND (a.updated_at, a.id) < (b.updated_at, b.id)"
                    )
                    await cur.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS idx_session_user_title_active "
                        "ON session(user_id, title) "
                        "WHERE status = 'active' AND title IS NOT NULL AND title <> ''"
                    )
    except Exception:  # noqa: BLE001
        logger.exception("ensure session(user_id, title) active-unique index failed (legacy dupes?)")


# ============================ Agent ops ===================================

async def create_agent(
    pool: AsyncConnectionPool,
    name: str,
    description: Optional[str],
    config_json: dict,
    model_id: uuid.UUID,
    creator: str,
    vision_model_id: Optional[uuid.UUID] = None,
    kind: str = "chat",
) -> dict:
    """Atomically create agent + first history snapshot (action=create).

    kind 仅供创建时指定（'chat' | 'cron'），更新路径不接触该列（不可变）。
    """
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                try:
                    await cur.execute(
                        "INSERT INTO agent (name, description, config_json, model_id, "
                        "vision_model_id, kind, creator) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                        "RETURNING id, name, description, config_json, model_id, "
                        "vision_model_id, kind, status, creator, updater, created_at, updated_at",
                        (name, description, Json(config_json), model_id, vision_model_id, kind, creator),
                    )
                except UniqueViolation:
                    # 并发兜底：service 预检通过后、INSERT 落地前的同名竞态 → 归一 409
                    raise FileExistsError(name)
                agent = await cur.fetchone()
                await cur.execute(
                    "INSERT INTO agent_config_history (agent_id, config_json, model_id, "
                    "vision_model_id, action, operator) "
                    "VALUES (%s, %s, %s, %s, 'create', %s)",
                    (agent["id"], Json(config_json), model_id, vision_model_id, creator),
                )
    return agent


async def update_agent(
    pool: AsyncConnectionPool,
    agent_id: uuid.UUID,
    config_json: dict,
    model_id: uuid.UUID,
    description: Optional[str],
    updater: str,
    name: Optional[str] = None,
    vision_model_id: Optional[uuid.UUID] = None,
) -> Optional[dict]:
    """Update agent config + model_id + vision_model_id in-place + append history snapshot.

    会话-智能体解耦后不再归档该 agent 的 active 会话：同 schema 工厂下旧
    checkpoint 可安全上新图续跑，同 agent 配置更新只是弱化版切换（plan
    1790660700000 §2.2）。cron 自动停用保留（跨更新的 awaiting_approval
    是真实风险，plan §9.5）。
    Returns None if agent not found. name=None（默认）保持原名，便于 restore_agent
    等不改名的调用方。
    """
    paused_cron = 0
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                try:
                    await cur.execute(
                        "UPDATE agent SET config_json = %s, model_id = %s, "
                        "vision_model_id = %s, "
                        "description = COALESCE(%s, description), "
                        "name = COALESCE(%s, name), "
                        "updater = %s, updated_at = now() WHERE id = %s "
                        "RETURNING id, name, description, config_json, model_id, "
                        "vision_model_id, kind, status, creator, updater, created_at, updated_at",
                        (Json(config_json), model_id, vision_model_id, description, name, updater, agent_id),
                    )
                except UniqueViolation:
                    # 并发改名兜底：预检无冲突后、UPDATE 落地前名字被他方占用 → 409。
                    # 仅 name 实际变更时才可能触发（name=None 保持原名不参与竞争）。
                    raise FileExistsError(name)
                agent = await cur.fetchone()
                if agent is None:
                    return None

                await cur.execute(
                    "INSERT INTO agent_config_history (agent_id, config_json, model_id, "
                    "vision_model_id, action, operator) "
                    "VALUES (%s, %s, %s, %s, 'update', %s)",
                    (agent_id, Json(config_json), model_id, vision_model_id, updater),
                )

                # 级联停用绑定该 agent 的定时任务：任务会话与 agent 1:1 锁定，
                # 更新后的图语义漂移对无人值守执行是真实风险。用户确认后可手动
                # 重新启用（启用时自动复活会话行）。
                await cur.execute(
                    "UPDATE cron_job SET enabled = false, updated_at = now(), "
                    "last_error = '绑定智能体已更新，任务已自动暂停；确认后可重新启用' "
                    "WHERE agent_id = %s AND enabled",
                    (agent_id,),
                )
                paused_cron = cur.rowcount
    if paused_cron:
        logger.info("agent %s updated; auto-paused %d cron jobs", agent_id, paused_cron)
    return agent


async def get_history_snapshot(
    pool: AsyncConnectionPool,
    agent_id: uuid.UUID,
    history_id: uuid.UUID,
) -> Optional[dict]:
    """Return {config_json, model_id, vision_model_id} for a history snapshot, or None."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT config_json, model_id, vision_model_id FROM agent_config_history "
                "WHERE id = %s AND agent_id = %s",
                (history_id, agent_id),
            )
            return await cur.fetchone()


async def restore_agent(
    pool: AsyncConnectionPool,
    agent_id: uuid.UUID,
    history_id: uuid.UUID,
    operator: str,
    vision_model_id: Optional[uuid.UUID] = None,
) -> Optional[dict]:
    """Restore an agent's config + model_id from a history snapshot (acts as update).

    The caller MUST validate the snapshot's model_id beforehand (see
    get_history_snapshot): history.model_id has no FK, so legacy snapshots may
    be NULL or point to a deleted model; restoring either would leave the agent
    unbuildable (sessions get archived by update_agent, but the agent itself
    must stay runnable). vision_model_id 同理无 FK——由调用方裁决后经
    ``vision_model_id`` 覆盖（快照值不可用时传 None 降级解除下挂）。
    """
    snapshot = await get_history_snapshot(pool, agent_id, history_id)
    if snapshot is None:
        return None
    effective_vision = vision_model_id if vision_model_id is not None else snapshot.get("vision_model_id")
    return await update_agent(
        pool, agent_id, snapshot["config_json"], snapshot["model_id"],
        description=None, updater=operator,
        vision_model_id=effective_vision,
    )


async def list_agents(pool: AsyncConnectionPool, include_archived: bool = False) -> list[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            where = "" if include_archived else " WHERE a.status != 'archived'"
            await cur.execute(
                f"SELECT a.id, a.name, a.description, a.model_id, m.name AS model_name, "
                f"a.vision_model_id, vm.name AS vision_model_name, a.kind, "
                f"a.status, a.creator, a.updater, a.created_at, a.updated_at "
                f"FROM agent a LEFT JOIN model m ON m.id = a.model_id "
                f"LEFT JOIN model vm ON vm.id = a.vision_model_id{where} "
                f"ORDER BY a.updated_at DESC"
            )
            rows = await cur.fetchall()
    return rows


async def get_agent(pool: AsyncConnectionPool, agent_id: uuid.UUID) -> Optional[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT a.id, a.name, a.description, a.config_json, a.model_id, "
                "m.name AS model_name, a.vision_model_id, vm.name AS vision_model_name, "
                "a.kind, a.status, a.creator, a.updater, "
                "a.created_at, a.updated_at "
                "FROM agent a LEFT JOIN model m ON m.id = a.model_id "
                "LEFT JOIN model vm ON vm.id = a.vision_model_id WHERE a.id = %s",
                (agent_id,),
            )
            row = await cur.fetchone()
    return row


async def get_agent_by_name(pool: AsyncConnectionPool, name: str) -> Optional[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, name, description, config_json, model_id, kind, status, creator, "
                "updater, created_at, updated_at FROM agent WHERE name = %s",
                (name,),
            )
            row = await cur.fetchone()
    return row


async def list_active_agents(pool: AsyncConnectionPool) -> list[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, name, config_json FROM agent WHERE status = 'active'"
            )
            rows = await cur.fetchall()
    return rows


async def archive_agent(pool: AsyncConnectionPool, agent_id: uuid.UUID) -> Optional[dict]:
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "UPDATE agent SET status = 'archived', updated_at = now() WHERE id = %s "
                    "RETURNING id, name, description, config_json, model_id, kind, status, "
                    "creator, updater, created_at, updated_at",
                    (agent_id,),
                )
                row = await cur.fetchone()
                if row is not None:
                    # 级联停用：archived agent 不可再被调度触发（FK 级联仅在物理删除时生效）
                    await cur.execute(
                        "UPDATE cron_job SET enabled = false, updated_at = now(), "
                        "last_error = '绑定智能体已归档，任务已自动暂停' "
                        "WHERE agent_id = %s AND enabled",
                        (agent_id,),
                    )
    return row


async def delete_agent(pool: AsyncConnectionPool, agent_id: uuid.UUID) -> bool:
    """Physically delete the agent row（会话-智能体解耦语义，plan §6.6）.

    会话比 agent 长寿，删除不再级联清场：
    - chat 会话：行与 checkpoint 全部存活，last_agent_id 经 FK SET NULL 置空；
      进入会话选择新智能体即可继续对话 / 回撤（弱绑定解析，stream.py）；
    - cron 会话：绑定任务行随 cron_job FK 级联消失，会话先行软归档保留产出
      （对齐"删除任务归档其专用会话"的既有语义）；
    - agent_config_history：无 FK（迁移拆除），快照保留为审计线索；
    - checkpoint 三表仅随 session purge 清理，与本删除解耦。
    Irreversible. Returns True if the agent row was deleted, False if not found.
    """
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                # cron 专用会话先行归档（须在 agent 删除级联掉 cron_job 之前，
                # 按 last_agent_id 圈定其专用会话）
                await cur.execute(
                    "UPDATE session SET status = 'archived', updated_at = now() "
                    "WHERE kind = 'cron' AND last_agent_id = %s AND status = 'active'",
                    (agent_id,),
                )
                archived_cron = cur.rowcount
                await cur.execute("DELETE FROM agent WHERE id = %s", (agent_id,))
                deleted = cur.rowcount > 0
    if deleted:
        logger.info(
            "agent %s hard-deleted; chat sessions survive (last_agent_id set NULL), "
            "%d cron sessions archived",
            agent_id, archived_cron,
        )
    return deleted


async def list_history(pool: AsyncConnectionPool, agent_id: uuid.UUID) -> list[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, config_json, model_id, action, operator, created_at "
                "FROM agent_config_history WHERE agent_id = %s ORDER BY created_at DESC",
                (agent_id,),
            )
            rows = await cur.fetchall()
    return rows


# ============================ Session ops =================================

async def create_session(
    pool: AsyncConnectionPool,
    agent_id: Optional[uuid.UUID],
    user_id: uuid.UUID,
    thread_id: str,
    title: Optional[str],
    kind: str = "chat",
) -> dict:
    """Insert a session row.

    agent_id 可选（会话-智能体解耦）：None = 创建时尚未选择智能体，首条
    消息发送时写入 last_agent_id（db.set_session_last_agent）。

    UniqueViolation → 归一化异常（service 预检与 INSERT 之间竞态的兜底）：
    - idx_session_user_title_active：同用户活跃会话标题撞车 → FileExistsError(409)
    - session_thread_id_key：自传 thread_id 撞车 → ValueError(400)
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            try:
                await cur.execute(
                    "INSERT INTO session (last_agent_id, user_id, thread_id, title, kind) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "RETURNING id, last_agent_id, user_id, thread_id, title, status, kind, "
                    "created_at, updated_at",
                    (agent_id, user_id, thread_id, title, kind),
                )
                row = await cur.fetchone()
            except UniqueViolation as e:
                constraint = getattr(e.diag, "constraint_name", "") or ""
                if constraint == "session_thread_id_key":
                    raise ValueError(f"thread_id '{thread_id}' 已被其它会话使用")
                # 其余唯一冲突均为 (user_id, title) 活跃会话标题
                raise FileExistsError(title or "")
    return row


async def set_session_last_agent(
    pool: AsyncConnectionPool,
    session_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> None:
    """send 时写入 last_agent_id（唯一写点，plan §4.4 写者语义）。

    在校验通过后、start_run 前调用：run 单写者闸排除并发 send；crash 窗口
    内 last 指向"意图 agent"，无害。resume/withdraw 不写（选中是纯 UI 态）。
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE session SET last_agent_id = %s WHERE id = %s",
                (agent_id, session_id),
            )


async def get_active_session_by_user_title(
    pool: AsyncConnectionPool, user_id: uuid.UUID, title: str
) -> Optional[dict]:
    """按 (user_id, title) 查活跃会话（去重预检用；归档会话豁免）。"""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, last_agent_id, user_id, thread_id, title, status, created_at, updated_at "
                "FROM session WHERE user_id = %s AND title = %s AND status = 'active' "
                "LIMIT 1",
                (user_id, title),
            )
            return await cur.fetchone()


async def get_session_by_thread(
    pool: AsyncConnectionPool, thread_id: str
) -> Optional[dict]:
    """按 thread_id 查会话（自传 thread_id 冲突预检用）。"""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, last_agent_id, user_id, thread_id, title, status, created_at, updated_at "
                "FROM session WHERE thread_id = %s",
                (thread_id,),
            )
            return await cur.fetchone()


async def list_sessions_by_user(
    pool: AsyncConnectionPool, user_id: uuid.UUID, include_archived: bool = False
) -> list[dict]:
    # LEFT JOIN（会话-智能体解耦）：last_agent 悬空（agent 已删 / 未选择）时
    # 会话仍需列出，agent_name 为 NULL 由前端优雅降级展示。
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            extra = "" if include_archived else " AND s.status != 'archived'"
            await cur.execute(
                f"SELECT s.id, s.last_agent_id, s.user_id, s.thread_id, s.title, s.status, s.kind, "
                f"s.created_at, s.updated_at, a.name AS agent_name "
                f"FROM session s LEFT JOIN agent a ON a.id = s.last_agent_id "
                f"WHERE s.user_id = %s{extra} ORDER BY s.updated_at DESC",
                (user_id,),
            )
            rows = await cur.fetchall()
    return rows


async def get_session(pool: AsyncConnectionPool, session_id: uuid.UUID) -> Optional[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, last_agent_id, user_id, thread_id, title, status, kind, usage_totals, "
                "created_at, updated_at FROM session WHERE id = %s",
                (session_id,),
            )
            row = await cur.fetchone()
    return row


async def archive_session(pool: AsyncConnectionPool, session_id: uuid.UUID) -> Optional[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "UPDATE session SET status = 'archived', updated_at = now() WHERE id = %s "
                "RETURNING id, last_agent_id, user_id, thread_id, title, status, created_at, updated_at",
                (session_id,),
            )
            row = await cur.fetchone()
    return row


async def delete_session(pool: AsyncConnectionPool, session_id: uuid.UUID) -> bool:
    """Physically delete a session + its LangGraph checkpoint state. Irreversible."""
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT thread_id FROM session WHERE id = %s", (session_id,)
                )
                row = await cur.fetchone()
                if row is None:
                    return False
                thread_id = row[0]
                await cur.execute("DELETE FROM session WHERE id = %s", (session_id,))
                if thread_id:
                    for tbl in _CHECKPOINT_TABLES:
                        await cur.execute(
                            f"DELETE FROM {tbl} WHERE thread_id = %s", (thread_id,)
                        )
    logger.info("session %s hard-deleted", session_id)
    return True


async def touch_session(pool: AsyncConnectionPool, session_id: uuid.UUID) -> None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("UPDATE session SET updated_at = now() WHERE id = %s", (session_id,))


# usage_totals 的固定键（写入与展示的唯一契约；缺失键按 0 处理）
_USAGE_KEYS = ("input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens", "total_tokens")


async def add_session_usage(
    pool: AsyncConnectionPool, session_id: uuid.UUID, delta: dict
) -> None:
    """把一次 LLM 调用的 token 用量原子累加进 session.usage_totals（jsonb）。

    单条 UPDATE 在 SQL 侧做读-改-写（COALESCE 旧值 + 增量），无需应用层事务；
    键集合固定为 _USAGE_KEYS，历史行缺键 / 列为 NULL 均从 0 起算。累加语义是
    成本视角：回撤（withdraw）不回退已消耗的 token；子代理调用同样计入。
    delta 中未知键忽略（防御事件 payload 演进）。
    """
    values = [int(delta.get(k) or 0) for k in _USAGE_KEYS]
    if not any(values):
        return
    set_expr = ",\n      ".join(
        f"'{k}', COALESCE((usage_totals->>'{k}')::bigint, 0) + %s" for k in _USAGE_KEYS
    )
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE session SET usage_totals = jsonb_build_object(\n      {set_expr}\n      ) "
                "WHERE id = %s",
                (*values, session_id),
            )


# ============================ 工作区文件变更明细（file_change 落库）=============================
# SSE file_change 事件的持久镜像（写入点：run 终端 on_file_change 回调，best-effort）。
# records 为 tracker FileChangeRecord.to_payload() 形状（path/action/added/removed/diff）。

async def insert_file_changes(
    pool: AsyncConnectionPool,
    session_id: uuid.UUID,
    anchor_message_id: str,
    run_token: str,
    records: list[dict],
) -> None:
    """一批 file_change 记录镜像入库（同批单事务；空批直通）。"""
    if not records:
        return
    rows = [
        (session_id, anchor_message_id, run_token, r["path"], r["action"], r.get("added"), r.get("removed"), r.get("diff"))
        for r in records
    ]
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO file_change_log "
                "(session_id, anchor_message_id, run_token, path, action, added, removed, diff_text) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                rows,
            )


async def list_file_changes(
    pool: AsyncConnectionPool, session_id: uuid.UUID, anchor_message_id: str | None = None
) -> list[dict]:
    """会话的文件变更记录（到达序）。anchor 提供时仅取该回合（前端按末回合回放）；
    无 anchor 取最近 500 条兜底（防全表回放，当前无调用方依赖）。"""
    where = "WHERE session_id = %s"
    params: list = [session_id]
    if anchor_message_id is not None:
        where += " AND anchor_message_id = %s"
        params.append(anchor_message_id)
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT path, action, added, removed, diff_text, created_at "
                f"FROM file_change_log {where} "
                f"ORDER BY created_at {'DESC LIMIT 500' if anchor_message_id is None else ''}",
                params,
            )
            rows = await cur.fetchall()
    return list(reversed(rows)) if anchor_message_id is None else rows


async def delete_file_changes_by_anchor(
    pool: AsyncConnectionPool, session_id: uuid.UUID, anchor_message_id: str
) -> None:
    """回撤一回合：按锚点删该回合全部镜像行（与消息软删对齐，best-effort）。"""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM file_change_log WHERE session_id = %s AND anchor_message_id = %s",
                (session_id, anchor_message_id),
            )


# ============================ 会话排队发送（queued send）=============================
# 状态机见 .kilo/plans/queued-send-messages.md §2。行级操作均为单条 UPDATE 原子
# 完成（无读-改-写窗口）；FIFO 认领经 FOR UPDATE SKIP LOCKED（单 worker 下由
# QueueService per-session 锁先串行，此为多副本兜底）。

_QUEUED_COLUMNS = (
    "id, session_id, user_id, agent_id, content, rubric, "
    "upload_ids, kb_ids, status, fail_reason, created_at"
)


async def insert_queued_message(
    pool: AsyncConnectionPool,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    agent_id: uuid.UUID,
    content: str,
    rubric: Optional[str],
    upload_ids: list[str],
    kb_ids: list[str],
) -> dict:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"INSERT INTO queued_message "
                "(session_id, user_id, agent_id, content, rubric, upload_ids, kb_ids) "
                f"VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING {_QUEUED_COLUMNS}",
                (
                    session_id, user_id, agent_id, content, rubric,
                    Json(upload_ids or []), Json(kb_ids or []),
                ),
            )
            return await cur.fetchone()


async def count_queued_by_status(
    pool: AsyncConnectionPool, session_id: uuid.UUID, statuses: tuple[str, ...]
) -> int:
    placeholders = ", ".join(["%s"] * len(statuses))
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT count(*) FROM queued_message "
                f"WHERE session_id = %s AND status IN ({placeholders})",
                (session_id, *statuses),
            )
            return (await cur.fetchone())[0]


async def list_queued_messages(
    pool: AsyncConnectionPool, session_id: uuid.UUID
) -> list[dict]:
    """待发 / 发送中 / 失败 / 已取消行（sent 审计行不返回），created_at FIFO 序。"""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_QUEUED_COLUMNS} FROM queued_message "
                "WHERE session_id = %s AND status != 'sent' "
                "ORDER BY created_at",
                (session_id,),
            )
            return await cur.fetchall()


async def get_queued_message(
    pool: AsyncConnectionPool, session_id: uuid.UUID, item_id: uuid.UUID
) -> Optional[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_QUEUED_COLUMNS} FROM queued_message "
                "WHERE id = %s AND session_id = %s",
                (item_id, session_id),
            )
            return await cur.fetchone()


async def claim_next_queued(
    pool: AsyncConnectionPool, session_id: uuid.UUID
) -> Optional[dict]:
    """认领队头 pending 行（FIFO）→ sending；无可认领返回 None。"""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "UPDATE queued_message SET status = 'sending' "
                "WHERE id = ("
                "  SELECT id FROM queued_message"
                "  WHERE session_id = %s AND status = 'pending'"
                "  ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED"
                ") "
                f"RETURNING {_QUEUED_COLUMNS}",
                (session_id,),
            )
            return await cur.fetchone()


async def mark_queued_sent(pool: AsyncConnectionPool, item_id: uuid.UUID) -> None:
    """sending → sent：run 终态即已消耗回合（interrupt/error/canceled 同置，消息已进历史）。"""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE queued_message SET status = 'sent', fail_reason = NULL "
                "WHERE id = %s AND status = 'sending'",
                (item_id,),
            )


async def mark_queued_failed(
    pool: AsyncConnectionPool, item_id: uuid.UUID, reason: str
) -> None:
    """sending → failed：发送前复检失败（单条意图失效，跳过续排）。"""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE queued_message SET status = 'failed', fail_reason = %s "
                "WHERE id = %s AND status = 'sending'",
                (reason, item_id),
            )


async def reset_queued_to_pending(pool: AsyncConnectionPool, item_id: uuid.UUID) -> None:
    """sending → pending：start_run 冲突 / 限额的回置（留待下一终态或手动触发）。"""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE queued_message SET status = 'pending' "
                "WHERE id = %s AND status = 'sending'",
                (item_id,),
            )


async def requeue_canceled_queued(
    pool: AsyncConnectionPool, session_id: uuid.UUID, item_ids: Optional[list[uuid.UUID]]
) -> int:
    """canceled → pending（保留 created_at，FIFO 序不变）。ids 缺省 = 全部 canceled。"""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            if item_ids:
                placeholders = ", ".join(["%s"] * len(item_ids))
                await cur.execute(
                    "UPDATE queued_message SET status = 'pending', fail_reason = NULL "
                    f"WHERE session_id = %s AND status = 'canceled' AND id IN ({placeholders})",
                    (session_id, *item_ids),
                )
            else:
                await cur.execute(
                    "UPDATE queued_message SET status = 'pending', fail_reason = NULL "
                    "WHERE session_id = %s AND status = 'canceled'",
                    (session_id,),
                )
            return cur.rowcount


async def cancel_pending_queued(
    pool: AsyncConnectionPool, session_id: uuid.UUID, item_ids: Optional[list[uuid.UUID]]
) -> int:
    """pending → canceled（中止 / 单条删除前的取消）。ids 缺省 = 全部 pending。"""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            if item_ids:
                placeholders = ", ".join(["%s"] * len(item_ids))
                await cur.execute(
                    f"UPDATE queued_message SET status = 'canceled' "
                    f"WHERE session_id = %s AND status = 'pending' AND id IN ({placeholders})",
                    (session_id, *item_ids),
                )
            else:
                await cur.execute(
                    "UPDATE queued_message SET status = 'canceled' "
                    "WHERE session_id = %s AND status = 'pending'",
                    (session_id,),
                )
            return cur.rowcount


async def delete_queued_message(
    pool: AsyncConnectionPool, session_id: uuid.UUID, item_id: uuid.UUID
) -> Optional[str]:
    """删除单行，返回删除前的 status（行不存在 / 属他会话返回 None）。"""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "DELETE FROM queued_message WHERE id = %s AND session_id = %s "
                "RETURNING status",
                (item_id, session_id),
            )
            row = await cur.fetchone()
    return row["status"] if row else None


async def clear_queued_messages(pool: AsyncConnectionPool, session_id: uuid.UUID) -> int:
    """清空队列（删除全部非 sent 行；[清空] 动作）。"""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM queued_message WHERE session_id = %s AND status != 'sent'",
                (session_id,),
            )
            return cur.rowcount


async def prune_sent_queued(
    pool: AsyncConnectionPool, session_id: uuid.UUID, keep: int = 200
) -> int:
    """sent 审计行按会话限量保留（删旧留新），防无限增长。"""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM queued_message WHERE id IN ("
                "  SELECT id FROM queued_message"
                "  WHERE session_id = %s AND status = 'sent'"
                "  ORDER BY created_at DESC OFFSET %s"
                ")",
                (session_id, keep),
            )
            return cur.rowcount


async def reset_stale_sending_queued(pool: AsyncConnectionPool) -> int:
    """lifespan 启动清扫：崩溃残留的 sending 行回置 pending。"""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE queued_message SET status = 'pending' "
                "WHERE status = 'sending'"
            )
            return cur.rowcount


async def pause_nonempty_queues(
    pool: AsyncConnectionPool, reason: str = "restart"
) -> int:
    """lifespan 启动清扫：一切非空队列（有 pending/sending 行）统一置暂停。

    与"run 缓冲俱失"的重启语义对齐：在途意图挂起待确认（error/withdrawn 的
    既有挂起行被 restart 覆盖为同效挂起，语义不回退）。
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO queue_state (session_id, paused_reason) "
                "SELECT DISTINCT session_id, %s FROM queued_message "
                "WHERE status IN ('pending', 'sending') "
                "ON CONFLICT (session_id) DO UPDATE "
                "SET paused_reason = EXCLUDED.paused_reason, updated_at = now()",
                (reason,),
            )
            return cur.rowcount


async def get_queue_pause(
    pool: AsyncConnectionPool, session_id: uuid.UUID
) -> Optional[str]:
    """队列暂停原因；无行 = active。"""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT paused_reason FROM queue_state WHERE session_id = %s",
                (session_id,),
            )
            row = await cur.fetchone()
    return row[0] if row else None


async def pause_queue(
    pool: AsyncConnectionPool, session_id: uuid.UUID, reason: str
) -> None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO queue_state (session_id, paused_reason) VALUES (%s, %s) "
                "ON CONFLICT (session_id) DO UPDATE "
                "SET paused_reason = EXCLUDED.paused_reason, updated_at = now()",
                (session_id, reason),
            )


async def resume_queue(pool: AsyncConnectionPool, session_id: uuid.UUID) -> None:
    """清除暂停（行删除即 active）。"""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM queue_state WHERE session_id = %s", (session_id,)
            )


async def clear_queue_pause_if(
    pool: AsyncConnectionPool, session_id: uuid.UUID, reason: str
) -> None:
    """仅当当前暂停原因为 reason 时清除（新消息入队清 restart 用）。"""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM queue_state WHERE session_id = %s AND paused_reason = %s",
                (session_id, reason),
            )


# ============================ MCP server ops ==============================

async def list_mcp_servers(pool: AsyncConnectionPool) -> list[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, name, kind, config_json, enabled, created_at "
                "FROM mcp_server ORDER BY created_at"
            )
            rows = await cur.fetchall()
    return rows


async def get_mcp_server(
    pool: AsyncConnectionPool, server_id: uuid.UUID
) -> Optional[dict]:
    """Fetch one MCP server registration by id."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, name, kind, config_json, enabled, created_at "
                "FROM mcp_server WHERE id = %s",
                (server_id,),
            )
            return await cur.fetchone()


async def get_mcp_server_by_name(pool: AsyncConnectionPool, name: str) -> Optional[dict]:
    """Row by unique name — create/update name-conflict pre-checks."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, name, kind, config_json, enabled, created_at "
                "FROM mcp_server WHERE name = %s",
                (name,),
            )
            return await cur.fetchone()


async def list_agent_configs(pool: AsyncConnectionPool) -> list[dict]:
    """All agents' {id, name, config_json} — MCP 引用扫描用（改名/删除前置校验）。

    config_json 任意层级的 *tool_names（tool_names / grader_tool_names /
    subagents[*].tool_names ...）均按名引用 `{server}__{tool}` 命名空间。含
    archived（与 list_agents_by_model 同口径：解除归档后工具必须仍可用）。
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT id, name, config_json FROM agent")
            return await cur.fetchall()


async def insert_mcp_server(
    pool: AsyncConnectionPool,
    name: str,
    kind: str,
    config_json: dict,
    enabled: bool,
) -> dict:
    """Insert a new MCP server registration.

    UniqueViolation → FileExistsError：应用层 check-then-insert 之间的并发竞态
    由 mcp_server.name UNIQUE 约束兜住，路由层统一映射 409（对齐 kb_document）。
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            try:
                await cur.execute(
                    "INSERT INTO mcp_server (name, kind, config_json, enabled) "
                    "VALUES (%s, %s, %s, %s) "
                    "RETURNING id, name, kind, config_json, enabled, created_at",
                    (name, kind, Json(config_json), enabled),
                )
                return await cur.fetchone()
            except UniqueViolation:
                raise FileExistsError(name)


async def update_mcp_server(
    pool: AsyncConnectionPool,
    server_id: uuid.UUID,
    name: str,
    kind: str,
    config_json: dict,
    enabled: bool,
) -> Optional[dict]:
    """Update an MCP server registration by PK (name may change).

    取代旧的按名 upsert（改名会 INSERT 新行残留旧行；撞名会静默覆写他人配置）。
    UniqueViolation → FileExistsError 为并发改名兜底。Returns None if absent.
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            try:
                await cur.execute(
                    "UPDATE mcp_server SET name = %s, kind = %s, config_json = %s, "
                    "enabled = %s WHERE id = %s "
                    "RETURNING id, name, kind, config_json, enabled, created_at",
                    (name, kind, Json(config_json), enabled, server_id),
                )
                return await cur.fetchone()
            except UniqueViolation:
                raise FileExistsError(name)


async def delete_mcp_server(pool: AsyncConnectionPool, server_id: uuid.UUID) -> bool:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM mcp_server WHERE id = %s", (server_id,))
            return cur.rowcount > 0


# ============================ Upload ops ==================================

async def insert_upload(
    pool: AsyncConnectionPool,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    bucket_key: str,
    filename: str,
    mime: str,
    kind: str,
    size: int,
    page_count: Optional[int],
    preview: Optional[str],
    upload_id: Optional[uuid.UUID] = None,
) -> dict:
    """Insert an upload metadata row. kind/mime resolved server-side at upload.

    upload_id: optional explicit PK. The upload layer sets it equal to the MinIO
    bucket_key so the DB-less wrapper can fetch the object by upload_id alone.
    """
    cols = "session_id, user_id, bucket_key, filename, mime, kind, size, page_count, preview"
    placeholders = "%s, %s, %s, %s, %s, %s, %s, %s, %s"
    params: list = [session_id, user_id, bucket_key, filename, mime, kind, size, page_count, preview]
    if upload_id is not None:
        cols = "id, " + cols
        placeholders = "%s, " + placeholders
        params = [upload_id, *params]
    sql = (
        f"INSERT INTO upload ({cols}) VALUES ({placeholders}) "
        "RETURNING id, session_id, user_id, bucket_key, filename, mime, kind, size, "
        "page_count, preview, created_at"
    )
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            row = await cur.fetchone()
    return row


async def get_upload(pool: AsyncConnectionPool, upload_id: uuid.UUID) -> Optional[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, session_id, user_id, bucket_key, filename, mime, kind, size, "
                "page_count, preview, vision_desc, vision_desc_model, vision_desc_at, "
                "created_at FROM upload WHERE id = %s",
                (upload_id,),
            )
            row = await cur.fetchone()
    return row


async def get_upload_vision_desc(
    pool: AsyncConnectionPool, upload_id: uuid.UUID
) -> Optional[str]:
    """读取图像描述缓存（仅 desc 文本；不存在/未解析返回 None）。"""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT vision_desc FROM upload WHERE id = %s", (upload_id,)
            )
            row = await cur.fetchone()
    return row[0] if row else None


async def set_upload_vision_desc(
    pool: AsyncConnectionPool,
    upload_id: uuid.UUID,
    desc: str,
    model_label: str,
) -> None:
    """写入图像描述缓存（固定预 prompt 模板 ⇒ 同图跨轮/跨提问复用）。"""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE upload SET vision_desc = %s, vision_desc_model = %s, "
                "vision_desc_at = now() WHERE id = %s",
                (desc, model_label, upload_id),
            )


async def list_uploads_by_session(pool: AsyncConnectionPool, session_id: uuid.UUID) -> list[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, session_id, user_id, bucket_key, filename, mime, kind, size, "
                "page_count, preview, created_at FROM upload "
                "WHERE session_id = %s ORDER BY created_at DESC",
                (session_id,),
            )
            rows = await cur.fetchall()
    return rows


async def get_uploads_by_ids(
    pool: AsyncConnectionPool,
    upload_ids: list[uuid.UUID],
    session_id: uuid.UUID,
) -> dict[str, dict]:
    """Batch-fetch upload metadata by id, hard-scoped to a session.

    Used by history rendering: upload_ids come from HumanMessage
    additional_kwargs (written at send time), never parsed from content.
    Returns ``{str(id): row}`` for O(1) lookup by the caller. Scoping by
    session_id guarantees a stale id from another session never leaks.
    """
    if not upload_ids:
        return {}
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, filename, mime, kind, size, page_count, preview "
                "FROM upload WHERE id = ANY(%s) AND session_id = %s",
                (upload_ids, session_id),
            )
            rows = await cur.fetchall()
    return {str(r["id"]): r for r in rows}


# ============================ Knowledge base ops ==========================
# 仅状态/元数据 CRUD（PG 状态卡）；chunk 文本/向量检索归 Elasticsearch。

_KB_COLUMNS = (
    "id, name, description, es_index_name, dims, embedding_id, status, creator, created_at, updated_at"
)
# JOIN 场景需限定表别名：kb_document 同样含 id/created_at/updated_at，
# 直接用 _KB_COLUMNS 会 AmbiguousColumn。结果列名仍为 id/name/...（dict 键不变）。
_KB_COLUMNS_QUALIFIED = ", ".join(f"kb.{c}" for c in _KB_COLUMNS.split(", "))
_KBDOC_COLUMNS = (
    "id, kb_id, bucket_key, filename, mime, size, page_count, "
    "status, chunk_count, progress, chunk_size, chunk_overlap, error, "
    "doc_version, parser_engine, created_at, updated_at"
)


async def create_kb(
    pool: AsyncConnectionPool,
    name: str,
    description: Optional[str],
    es_index_name: str,
    dims: Optional[int],
    creator: str,
    kb_id: Optional[uuid.UUID] = None,
    embedding_id: Optional[uuid.UUID] = None,
) -> dict:
    """Create a knowledge base state card. es_index_name='kb_<id>' / dims=global
    EMBEDDING_DIMS are written at creation (dims recorded for drift detection only).

    kb_id: optional explicit PK. Set so es_index_name = 'kb_<id>' is consistent
    (index name embeds the id; resolving both at the service layer avoids a
    placeholder-then-update round trip).
    embedding_id: optional vector-model binding (None = legacy dims+env path).
    """
    cols = "name, description, es_index_name, dims, embedding_id, creator"
    placeholders = "%s, %s, %s, %s, %s, %s"
    params: list = [name, description, es_index_name, dims, embedding_id, creator]
    if kb_id is not None:
        cols = "id, " + cols
        placeholders = "%s, " + placeholders
        params = [kb_id, *params]
    sql = (
        f"INSERT INTO knowledge_base ({cols}) VALUES ({placeholders}) "
        f"RETURNING {_KB_COLUMNS}"
    )
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return await cur.fetchone()


async def get_kb(pool: AsyncConnectionPool, kb_id: uuid.UUID) -> Optional[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_KB_COLUMNS} FROM knowledge_base WHERE id = %s",
                (kb_id,),
            )
            return await cur.fetchone()


async def get_kb_by_name(pool: AsyncConnectionPool, name: str) -> Optional[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_KB_COLUMNS} FROM knowledge_base WHERE name = %s",
                (name,),
            )
            return await cur.fetchone()


async def get_kbs_by_ids(
    pool: AsyncConnectionPool, kb_ids: list[uuid.UUID]
) -> list[dict]:
    """Fetch selected KBs by id (stream.py KB anchor injection: needs id+name)."""
    if not kb_ids:
        return []
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_KB_COLUMNS} FROM knowledge_base WHERE id = ANY(%s)",
                (kb_ids,),
            )
            return await cur.fetchall()


async def list_kbs(
    pool: AsyncConnectionPool, include_inactive: bool = False
) -> list[dict]:
    """List knowledge bases with aggregated doc_count / chunk_count."""
    where = "" if include_inactive else " WHERE kb.status = 'active'"
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_KB_COLUMNS_QUALIFIED}, e.name AS embedding_name, "
                f"COUNT(d.id) AS doc_count, COALESCE(SUM(d.chunk_count), 0) AS chunk_count "
                f"FROM knowledge_base kb LEFT JOIN kb_document d ON d.kb_id = kb.id "
                f"LEFT JOIN embedding e ON e.id = kb.embedding_id "
                f"{where} "
                f"GROUP BY kb.id, e.name ORDER BY kb.updated_at DESC"
            )
            return await cur.fetchall()


async def update_kb(
    pool: AsyncConnectionPool,
    kb_id: uuid.UUID,
    name: str,
    description: Optional[str],
    dims: Optional[int] = None,
    embedding_id: Optional[uuid.UUID] = None,
) -> Optional[dict]:
    """dims / embedding_id 用 COALESCE：传 None 保持原值（服务层守卫后才传入）。"""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"UPDATE knowledge_base SET name = %s, "
                f"description = COALESCE(%s, description), "
                f"dims = COALESCE(%s, dims), "
                f"embedding_id = COALESCE(%s, embedding_id), updated_at = now() "
                f"WHERE id = %s RETURNING {_KB_COLUMNS}",
                (name, description, dims, embedding_id, kb_id),
            )
            return await cur.fetchone()


async def delete_kb(pool: AsyncConnectionPool, kb_id: uuid.UUID) -> bool:
    """Delete a KB row; CASCADE removes its documents. ES index drop is the caller's
    responsibility (delete_kb in KnowledgeService) since DB layer knows nothing of ES."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM knowledge_base WHERE id = %s", (kb_id,))
            return cur.rowcount > 0


async def create_kb_document(
    pool: AsyncConnectionPool,
    kb_id: uuid.UUID,
    bucket_key: Optional[str],
    filename: str,
    mime: str,
    size: int,
    page_count: Optional[int],
    doc_id: Optional[uuid.UUID] = None,
) -> dict:
    """Insert a document state card (status defaults to 'pending').

    doc_id: optional explicit PK. Set == MinIO bucket_key so ingest can fetch
    the object by doc_id alone when KB_STORE_MINIO=true.
    """
    cols = "kb_id, bucket_key, filename, mime, size, page_count"
    placeholders = "%s, %s, %s, %s, %s, %s"
    params: list = [kb_id, bucket_key, filename, mime, size, page_count]
    if doc_id is not None:
        cols = "id, " + cols
        placeholders = "%s, " + placeholders
        params = [doc_id, *params]
    sql = (
        f"INSERT INTO kb_document ({cols}) VALUES ({placeholders}) "
        f"RETURNING {_KBDOC_COLUMNS}"
    )
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            try:
                await cur.execute(sql, params)
                return await cur.fetchone()
            except UniqueViolation:
                # 并发兜底：应用层 check 与 insert 之间的竞态由唯一索引兜住 → 同名冲突
                raise FileExistsError(filename)


async def get_kb_document_by_name(
    pool: AsyncConnectionPool, kb_id: uuid.UUID, filename: str
) -> Optional[dict]:
    """按 (kb_id, filename) 查文档（精确匹配，区分大小写）。供上传去重预检。"""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_KBDOC_COLUMNS} FROM kb_document "
                "WHERE kb_id = %s AND filename = %s",
                (kb_id, filename),
            )
            return await cur.fetchone()


async def get_kb_document(
    pool: AsyncConnectionPool, doc_id: uuid.UUID
) -> Optional[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_KBDOC_COLUMNS} FROM kb_document WHERE id = %s",
                (doc_id,),
            )
            return await cur.fetchone()


async def list_kb_documents(
    pool: AsyncConnectionPool, kb_id: uuid.UUID
) -> list[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_KBDOC_COLUMNS} FROM kb_document WHERE kb_id = %s "
                f"ORDER BY created_at DESC",
                (kb_id,),
            )
            return await cur.fetchall()


async def update_kb_document_status(
    pool: AsyncConnectionPool,
    doc_id: uuid.UUID,
    *,
    status: str,
    chunk_count: Optional[int] = None,
    error: Optional[str] = None,
    page_count: Optional[int] = None,
    progress: Optional[int] = None,
    parser_engine: Optional[str] = None,
) -> Optional[dict]:
    """Transition a document's status (pending|indexing|indexed|failed).

    chunk_count/page_count/progress/parser_engine: COALESCE keeps existing when
    None. error is set explicitly (pass None to clear a prior failure message).
    progress 0..100：indexing 期间随嵌入批次推进（调用方逐批传入）；indexed
    时传 100。parser_engine 仅终态回写（indexed），标记产出当前 chunk 的引擎。"""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"UPDATE kb_document SET status = %s, "
                f"chunk_count = COALESCE(%s, chunk_count), error = %s, "
                f"page_count = COALESCE(%s, page_count), "
                f"progress = COALESCE(%s, progress), "
                f"parser_engine = COALESCE(%s, parser_engine), "
                f"updated_at = now() "
                f"WHERE id = %s RETURNING {_KBDOC_COLUMNS}",
                (status, chunk_count, error, page_count, progress, parser_engine, doc_id),
            )
            return await cur.fetchone()


async def update_kb_document_parse_params(
    pool: AsyncConnectionPool,
    doc_id: uuid.UUID,
    chunk_size: int,
    chunk_overlap: int,
) -> Optional[dict]:
    """解析触发时持久化切块参数（实际生效值，reindex 沿用 → 重建可复现）。"""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"UPDATE kb_document SET chunk_size = %s, chunk_overlap = %s, "
                f"updated_at = now() WHERE id = %s RETURNING {_KBDOC_COLUMNS}",
                (chunk_size, chunk_overlap, doc_id),
            )
            return await cur.fetchone()


async def reset_kb_documents(
    pool: AsyncConnectionPool, kb_id: uuid.UUID
) -> int:
    """维度变更后重置该库全部文档状态卡：status→pending、清 chunk_count/progress/error。

    只 UPDATE 不删除：PG 行与 MinIO 原始留底全部保留，用户可按新维度手动重新解析
    （保证后续可用）。ES 索引已由调用方整体 drop（旧维度向量不可复用）。返回受影响行数。
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE kb_document SET status = 'pending', chunk_count = NULL, "
                "progress = NULL, error = NULL, parser_engine = NULL, "
                "updated_at = now() "
                "WHERE kb_id = %s",
                (kb_id,),
            )
            return cur.rowcount


async def fail_stale_indexing_documents(
    pool: AsyncConnectionPool, error: str
) -> int:
    """启动恢复：入库任务纯内存（asyncio task），进程重启后 'indexing' 状态
    永久悬挂（无任务在跑、前端禁用重新解析入口）。归一为 failed 并注明原因，
    用户可重新解析（ingest 幂等删旧 chunk）。返回受影响行数。"""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE kb_document SET status = 'failed', error = %s, "
                "updated_at = now() WHERE status = 'indexing'",
                (error,),
            )
            return cur.rowcount


async def delete_kb_document(pool: AsyncConnectionPool, doc_id: uuid.UUID) -> bool:
    """Delete a document row. ES chunk removal is the caller's responsibility."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM kb_document WHERE id = %s", (doc_id,))
            return cur.rowcount > 0


# ============================ Model ops ==================================

_MODEL_COLUMNS = (
    "id, name, provider_kind, model_type, base_url, supports_vision, "
    "temperature, top_p, enable_thinking, enabled, created_at, updated_at"
)


async def insert_model(
    pool: AsyncConnectionPool,
    name: str,
    provider_kind: str,
    model_type: str,
    api_key: str,
    base_url: Optional[str],
    supports_vision: bool,
    temperature: Optional[float],
    top_p: Optional[float],
    enable_thinking: Optional[bool],
    enabled: bool,
) -> dict:
    """Insert a registered model definition. api_key stored at the row level."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            try:
                await cur.execute(
                    f"INSERT INTO model (name, provider_kind, model_type, api_key, base_url, "
                    f"supports_vision, temperature, top_p, enable_thinking, enabled) "
                    f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    f"RETURNING {_MODEL_COLUMNS}",
                    (name, provider_kind, model_type, api_key, base_url, supports_vision,
                     temperature, top_p, enable_thinking, enabled),
                )
                row = await cur.fetchone()
            except UniqueViolation:
                # 并发兜底：service 预检通过后、INSERT 落地前的同名竞态 → 归一 409
                raise FileExistsError(name)
    return row


async def update_model(
    pool: AsyncConnectionPool,
    model_id: uuid.UUID,
    *,
    name: str,
    provider_kind: str,
    model_type: str,
    api_key: Optional[str],
    base_url: Optional[str],
    supports_vision: bool,
    temperature: Optional[float],
    top_p: Optional[float],
    enable_thinking: Optional[bool],
    enabled: bool,
) -> Optional[dict]:
    """Update a registered model's mutable fields. model_id (PK) never changes.

    api_key=None means "keep existing"; otherwise overwrite (key rotation).
    """
    set_api_key = "api_key = %s" if api_key is not None else "api_key = model.api_key"
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            params = (
                [name, provider_kind, model_type, base_url, supports_vision,
                 temperature, top_p, enable_thinking, enabled]
                + ([api_key] if api_key is not None else [])
                + [model_id]
            )
            try:
                await cur.execute(
                    f"UPDATE model SET name = %s, provider_kind = %s, model_type = %s, "
                    f"base_url = %s, supports_vision = %s, temperature = %s, top_p = %s, "
                    f"enable_thinking = %s, enabled = %s, {set_api_key}, "
                    f"updated_at = now() WHERE id = %s RETURNING {_MODEL_COLUMNS}",
                    tuple(params),
                )
                row = await cur.fetchone()
            except UniqueViolation:
                # 并发改名兜底：预检无冲突后、UPDATE 落地前名字被他方占用 → 409
                raise FileExistsError(name)
    return row


async def get_model(pool: AsyncConnectionPool, model_id: uuid.UUID) -> Optional[dict]:
    """Full row incl. api_key (server-side only)."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_MODEL_COLUMNS}, api_key FROM model WHERE id = %s",
                (model_id,),
            )
            row = await cur.fetchone()
    return row


async def get_model_public(pool: AsyncConnectionPool, model_id: uuid.UUID) -> Optional[dict]:
    """Row WITHOUT api_key (for API responses)."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_MODEL_COLUMNS} FROM model WHERE id = %s",
                (model_id,),
            )
            row = await cur.fetchone()
    return row


async def list_models(
    pool: AsyncConnectionPool,
    supports_vision: Optional[bool] = None,
    enable_thinking: Optional[bool] = None,
) -> list[dict]:
    """List WITHOUT api_key (for API responses). Optional filters (AND-combined).

    supports_vision 过滤附带 enabled=true（能力候选必须可用）；
    enable_thinking 三态精确匹配（false=显式关闭，供视觉下挂候选过滤）。
    """
    clauses: list[str] = []
    params: list = []
    if supports_vision is not None:
        clauses.append("supports_vision = %s AND enabled = true")
        params.append(supports_vision)
    if enable_thinking is not None:
        clauses.append("enable_thinking = %s")
        params.append(enable_thinking)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_MODEL_COLUMNS} FROM model{where} ORDER BY created_at",
                tuple(params),
            )
            rows = await cur.fetchall()
    return rows


async def list_enabled_models(pool: AsyncConnectionPool) -> list[dict]:
    """Full rows incl. api_key for ModelStore startup loading."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_MODEL_COLUMNS}, api_key FROM model WHERE enabled = true"
            )
            rows = await cur.fetchall()
    return rows


async def delete_model(pool: AsyncConnectionPool, model_id: uuid.UUID) -> bool:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM model WHERE id = %s", (model_id,))
            return cur.rowcount > 0


async def get_model_by_name(pool: AsyncConnectionPool, name: str) -> Optional[dict]:
    """Public row (no api_key) by unique name — for explicit create/update conflict checks."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_MODEL_COLUMNS} FROM model WHERE name = %s",
                (name,),
            )
            row = await cur.fetchone()
    return row


async def list_agents_by_model(pool: AsyncConnectionPool, model_id: uuid.UUID) -> list[dict]:
    """Agents referencing a model as base OR vision mount (id + name) — delete blocking."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, name FROM agent "
                "WHERE model_id = %s OR vision_model_id = %s ORDER BY name",
                (model_id, model_id),
            )
            rows = await cur.fetchall()
    return rows


# ============================ Embedding ops ================================

_EMBEDDING_COLUMNS = (
    "id, name, provider_kind, model_type, base_url, dims, batch_size, "
    "timeout, check_ctx_length, enabled, creator, created_at, updated_at"
)


async def insert_embedding(
    pool: AsyncConnectionPool,
    *,
    name: str,
    provider_kind: str,
    model_type: str,
    api_key: str,
    base_url: str,
    dims: Optional[int],
    batch_size: int,
    timeout: int,
    check_ctx_length: bool,
    enabled: bool,
    creator: str,
) -> dict:
    """Insert a registered embedding definition. api_key stored at row level.

    UniqueViolation → FileExistsError（并发兜底，对齐 insert_model）。
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            try:
                await cur.execute(
                    f"INSERT INTO embedding (name, provider_kind, model_type, api_key, "
                    f"base_url, dims, batch_size, timeout, check_ctx_length, enabled, creator) "
                    f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    f"RETURNING {_EMBEDDING_COLUMNS}",
                    (name, provider_kind, model_type, api_key, base_url, dims,
                     batch_size, timeout, check_ctx_length, enabled, creator),
                )
                return await cur.fetchone()
            except UniqueViolation:
                raise FileExistsError(name)


async def update_embedding(
    pool: AsyncConnectionPool,
    embedding_id: uuid.UUID,
    *,
    name: str,
    provider_kind: str,
    model_type: str,
    api_key: Optional[str],
    base_url: str,
    dims: Optional[int],
    batch_size: int,
    timeout: int,
    check_ctx_length: bool,
    enabled: bool,
) -> Optional[dict]:
    """Update an embedding's mutable fields. id (PK) never changes.

    api_key=None keeps the existing key (rotation semantics, 对齐 update_model)。
    语义字段（端点/型号/维度）被 KB 引用时的不可变约束由 service 守卫。
    """
    set_api_key = "api_key = %s" if api_key is not None else "api_key = embedding.api_key"
    params = (
        [name, provider_kind, model_type, base_url, dims, batch_size,
         timeout, check_ctx_length, enabled]
        + ([api_key] if api_key is not None else [])
        + [embedding_id]
    )
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            try:
                await cur.execute(
                    f"UPDATE embedding SET name = %s, provider_kind = %s, model_type = %s, "
                    f"base_url = %s, dims = %s, batch_size = %s, timeout = %s, "
                    f"check_ctx_length = %s, enabled = %s, {set_api_key}, "
                    f"updated_at = now() WHERE id = %s RETURNING {_EMBEDDING_COLUMNS}",
                    tuple(params),
                )
                return await cur.fetchone()
            except UniqueViolation:
                raise FileExistsError(name)


async def get_embedding(
    pool: AsyncConnectionPool, embedding_id: uuid.UUID
) -> Optional[dict]:
    """Full row incl. api_key (server-side only; update 守卫比对用)."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_EMBEDDING_COLUMNS}, api_key FROM embedding WHERE id = %s",
                (embedding_id,),
            )
            return await cur.fetchone()


async def get_embedding_public(
    pool: AsyncConnectionPool, embedding_id: uuid.UUID
) -> Optional[dict]:
    """Row WITHOUT api_key (for API responses)."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_EMBEDDING_COLUMNS} FROM embedding WHERE id = %s",
                (embedding_id,),
            )
            return await cur.fetchone()


async def get_embedding_by_name(
    pool: AsyncConnectionPool, name: str
) -> Optional[dict]:
    """Public row (no api_key) by unique name — create/update 冲突预检."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_EMBEDDING_COLUMNS} FROM embedding WHERE name = %s",
                (name,),
            )
            return await cur.fetchone()


async def list_embeddings(pool: AsyncConnectionPool) -> list[dict]:
    """全部注册行（无 api_key，管理面展示）."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_EMBEDDING_COLUMNS} FROM embedding ORDER BY created_at"
            )
            return await cur.fetchall()


async def list_enabled_embeddings(pool: AsyncConnectionPool) -> list[dict]:
    """Enabled full rows incl. api_key — EmbeddingStore startup loading."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_EMBEDDING_COLUMNS}, api_key FROM embedding WHERE enabled = true"
            )
            return await cur.fetchall()


async def delete_embedding(pool: AsyncConnectionPool, embedding_id: uuid.UUID) -> bool:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM embedding WHERE id = %s", (embedding_id,))
            return cur.rowcount > 0


async def list_kbs_by_embedding(
    pool: AsyncConnectionPool, embedding_id: uuid.UUID
) -> list[dict]:
    """KBs bound to an embedding (id + name) — 删除/语义变更阻断守卫."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, name FROM knowledge_base "
                "WHERE embedding_id = %s ORDER BY name",
                (embedding_id,),
            )
            return await cur.fetchall()


async def seed_default_embedding(
    pool: AsyncConnectionPool,
    *,
    name: str,
    provider_kind: str,
    model_type: str,
    api_key: str,
    base_url: str,
    dims: Optional[int],
    batch_size: int,
    timeout: int,
    check_ctx_length: bool,
) -> Optional[dict]:
    """启动引导默认向量模型行（bootstrap 语义对齐 app_user）。

    幂等策略：
    - 同名行已存在 → 返回之（值不覆盖：seed 只在无主时建）。
    - 无同名行，但注册表非空且已无待回填库 → 跳过（None）。防"用户改名
      接管后，每次重启按 settings 名多造一条孤儿默认行"。
    - 全新库 / 存在 embedding_id IS NULL 的库 → 创建。
    行值与 env 同源 ⇒ "默认行 ≡ env 路径"（迁移期行为等价不变式，plan
    kb-embedding-model-registry §6）。
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_EMBEDDING_COLUMNS} FROM embedding WHERE name = %s", (name,)
            )
            row = await cur.fetchone()
            if row is not None:
                return row
            await cur.execute(
                "SELECT (SELECT count(*) FROM embedding) AS emb_n, "
                "(SELECT count(*) FROM knowledge_base WHERE embedding_id IS NULL) AS unbound_n"
            )
            stats = await cur.fetchone()
            if stats["emb_n"] > 0 and stats["unbound_n"] == 0:
                return None  # 用户已接管注册表（如改名），不再造行
            await cur.execute(
                f"INSERT INTO embedding (name, provider_kind, model_type, api_key, "
                f"base_url, dims, batch_size, timeout, check_ctx_length, enabled, creator) "
                f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, true, 'system') "
                f"RETURNING {_EMBEDDING_COLUMNS}",
                (name, provider_kind, model_type, api_key, base_url, dims,
                 batch_size, timeout, check_ctx_length),
            )
            return await cur.fetchone()


async def backfill_kb_embeddings(
    pool: AsyncConnectionPool, embedding_id: uuid.UUID
) -> int:
    """存量库 embedding_id 回填（NULL → 默认行；幂等）。返回受影响行数。"""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE knowledge_base SET embedding_id = %s, updated_at = now() "
                "WHERE embedding_id IS NULL",
                (embedding_id,),
            )
            return cur.rowcount


# ============================ Cron ops =====================================
# DB 为唯一真相源：本节函数是 scheduler / API 与 cron 表的全部契约。
# 时间语义：next_run_at / start_at 均为 timestamptz（UTC 往返）；认领采用
# "乐观并发推进"（WHERE next_run_at = 观察值），单进程下近乎免费，多副本天然防双触发。

_CRON_JOB_COLUMNS = (
    "j.id, j.user_id, j.agent_id, j.session_id, j.name, j.prompt, j.interval_minutes, "
    "j.start_at, j.enabled, j.next_run_at, j.last_run_at, j.last_status, j.last_error, "
    "j.consecutive_failures, j.created_at, j.updated_at, "
    "a.name AS agent_name, s.thread_id, u.username AS owner_username"
)


async def create_cron_job(
    pool: AsyncConnectionPool,
    user_id: uuid.UUID,
    agent_id: uuid.UUID,
    name: str,
    prompt: str,
    interval_minutes: int,
    start_at,
    next_run_at,
) -> dict:
    """Atomically create the dedicated session (kind='cron') + the job row.

    Job insert failure rolls the session back (same tx) — no orphan sessions.
    UniqueViolation maps: (user_id, name) → FileExistsError；session 活跃标题
    撞车（"[定时] name" 与用户手建会话同名）→ FileExistsError(title)。
    """
    thread_id = f"thread-{uuid.uuid4().hex}"
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                session = None
                try:
                    await cur.execute(
                        "INSERT INTO session (last_agent_id, user_id, thread_id, title, kind) "
                        "VALUES (%s, %s, %s, %s, 'cron') "
                        "RETURNING id, last_agent_id, user_id, thread_id, title, status, kind",
                        (agent_id, user_id, thread_id, f"[定时] {name}"),
                    )
                    session = await cur.fetchone()
                    await cur.execute(
                        "INSERT INTO cron_job (user_id, agent_id, session_id, name, prompt, "
                        "interval_minutes, start_at, next_run_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                        "RETURNING id, session_id, next_run_at",
                        (user_id, agent_id, session["id"], name, prompt,
                         interval_minutes, start_at, next_run_at),
                    )
                    job = await cur.fetchone()
                except UniqueViolation as e:
                    constraint = getattr(e.diag, "constraint_name", "") or ""
                    if constraint == "cron_job_user_id_name_key":
                        raise FileExistsError(name)
                    raise FileExistsError(f"[定时] {name}")
                job["thread_id"] = session["thread_id"]
    return job


async def get_cron_job(pool: AsyncConnectionPool, job_id: uuid.UUID) -> Optional[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_CRON_JOB_COLUMNS} FROM cron_job j "
                "JOIN agent a ON a.id = j.agent_id "
                "LEFT JOIN session s ON s.id = j.session_id "
                "LEFT JOIN app_user u ON u.id = j.user_id "
                "WHERE j.id = %s",
                (job_id,),
            )
            row = await cur.fetchone()
    return row


async def get_cron_job_by_user_name(
    pool: AsyncConnectionPool, user_id: uuid.UUID, name: str
) -> Optional[dict]:
    """按 (user_id, name) 查任务（建/改名去重预检用）。"""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, name FROM cron_job WHERE user_id = %s AND name = %s LIMIT 1",
                (user_id, name),
            )
            return await cur.fetchone()


async def list_cron_jobs_by_user(pool: AsyncConnectionPool, user_id: uuid.UUID) -> list[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_CRON_JOB_COLUMNS} FROM cron_job j "
                "JOIN agent a ON a.id = j.agent_id "
                "LEFT JOIN session s ON s.id = j.session_id "
                "LEFT JOIN app_user u ON u.id = j.user_id "
                "WHERE j.user_id = %s ORDER BY j.created_at DESC",
                (user_id,),
            )
            rows = await cur.fetchall()
    return rows


async def update_cron_job(
    pool: AsyncConnectionPool, job_id: uuid.UUID, fields: dict
) -> Optional[dict]:
    """Dynamic partial update（调用方保证字段合法，空 fields 直接返回当前行）。"""
    if not fields:
        return await get_cron_job(pool, job_id)
    sets = ", ".join(f"{k} = %s" for k in fields)
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            try:
                await cur.execute(
                    f"UPDATE cron_job SET {sets}, updated_at = now() WHERE id = %s",
                    (*fields.values(), job_id),
                )
            except UniqueViolation:
                raise FileExistsError(fields.get("name", ""))
    return await get_cron_job(pool, job_id)


async def delete_cron_job(pool: AsyncConnectionPool, job_id: uuid.UUID) -> bool:
    """Delete job (+ cascaded runs) and soft-archive its dedicated session.

    会话保留为 archived：历史产出仍可浏览（checkpoints 未动），标题随归档释放。
    """
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE session SET status = 'archived', updated_at = now() "
                    "WHERE id = (SELECT session_id FROM cron_job WHERE id = %s) "
                    "AND status = 'active'",
                    (job_id,),
                )
                await cur.execute("DELETE FROM cron_job WHERE id = %s", (job_id,))
                return cur.rowcount > 0


async def due_cron_jobs(pool: AsyncConnectionPool) -> list[dict]:
    """到期任务（scheduler 每 tick 调用）。thread_id 经 JOIN 带出供并发检查。"""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_CRON_JOB_COLUMNS} FROM cron_job j "
                "JOIN agent a ON a.id = j.agent_id "
                "LEFT JOIN session s ON s.id = j.session_id "
                "LEFT JOIN app_user u ON u.id = j.user_id "
                "WHERE j.enabled AND j.next_run_at IS NOT NULL AND j.next_run_at <= now() "
                "ORDER BY j.next_run_at"
            )
            rows = await cur.fetchall()
    return rows


async def claim_cron_job(
    pool: AsyncConnectionPool,
    job_id: uuid.UUID,
    observed_next_run_at,
    new_next_run_at,
) -> Optional[dict]:
    """认领：原子推进 next_run_at 并置 running（乐观并发，竞态返回 None）。

    认领先于执行（claim-then-execute）：崩溃时序上"丢一轮"优于"重复执行一轮"。
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "UPDATE cron_job SET next_run_at = %s, last_run_at = now(), "
                "last_status = 'running', updated_at = now() "
                "WHERE id = %s AND enabled AND next_run_at = %s "
                "RETURNING id",
                (new_next_run_at, job_id, observed_next_run_at),
            )
            row = await cur.fetchone()
    return row


async def skip_cron_job(
    pool: AsyncConnectionPool,
    job_id: uuid.UUID,
    observed_next_run_at,
    new_next_run_at,
) -> bool:
    """跳过一个槽位（overlap / awaiting_approval / busy）：仅推进相位，不写 run 行。"""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE cron_job SET next_run_at = %s, updated_at = now() "
                "WHERE id = %s AND next_run_at = %s",
                (new_next_run_at, job_id, observed_next_run_at),
            )
            return cur.rowcount > 0


async def finish_cron_job_run(
    pool: AsyncConnectionPool,
    job_id: uuid.UUID,
    status: str,
    error: Optional[str],
    max_consecutive_failures: int,
) -> None:
    """运行结局回写：状态 + 熔断计数（连续 error 达阈值自动停用，需人工重启）。"""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE cron_job SET "
                "last_status = %s, "
                "last_error = %s, "
                "consecutive_failures = "
                "    CASE WHEN %s = 'error' THEN consecutive_failures + 1 ELSE 0 END, "
                "enabled = CASE WHEN %s = 'error' AND consecutive_failures + 1 >= %s "
                "    THEN false ELSE enabled END, "
                "updated_at = now() "
                "WHERE id = %s",
                (status, error, status, status, max_consecutive_failures, job_id),
            )


async def sync_cron_job_status(pool: AsyncConnectionPool, session_id: uuid.UUID, status: str) -> None:
    """任务会话上的用户侧结局（HITL resume 完成后）回写任务状态，不产生 run 行。"""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE cron_job SET last_status = %s, updated_at = now() "
                "WHERE session_id = %s",
                (status, session_id),
            )


async def insert_cron_run(
    pool: AsyncConnectionPool, job_id: uuid.UUID, trigger: str = "scheduled"
) -> dict:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "INSERT INTO cron_run (job_id, trigger) VALUES (%s, %s) "
                "RETURNING id, job_id, status, trigger, error, started_at, finished_at",
                (job_id, trigger),
            )
            return await cur.fetchone()


async def finish_cron_run(
    pool: AsyncConnectionPool, run_id: uuid.UUID, status: str, error: Optional[str]
) -> None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE cron_run SET status = %s, error = %s, finished_at = now() "
                "WHERE id = %s",
                (status, error, run_id),
            )


async def list_cron_runs(
    pool: AsyncConnectionPool, job_id: uuid.UUID, limit: int = 50
) -> list[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, job_id, status, trigger, error, started_at, finished_at "
                "FROM cron_run WHERE job_id = %s ORDER BY started_at DESC LIMIT %s",
                (job_id, limit),
            )
            rows = await cur.fetchall()
    return rows


async def pause_cron_jobs_on_non_cron_agents(pool: AsyncConnectionPool) -> int:
    """启动规范化：停用绑定在 chat 型智能体上的存量任务（kind 隔离引入前的遗留）。

    幂等（停用后 enabled=false 不再命中）；停用原因写入 last_error 供用户在
    任务列表看到并处置（换绑 cron 型智能体或重建任务）。
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE cron_job j SET enabled = false, updated_at = now(), "
                "last_error = '绑定的智能体非定时任务类型（kind!=cron），任务已停用；"
                "请换绑 cron 型智能体后重新启用' "
                "FROM agent a WHERE a.id = j.agent_id AND a.kind != 'cron' AND j.enabled"
            )
            return cur.rowcount


async def reactivate_cron_session(pool: AsyncConnectionPool, session_id: uuid.UUID) -> Optional[dict]:
    """复活任务专用会话（任务重新启用时）。

    标题撞车（暂停期间用户手建了同名活跃会话）自动降级为未命名，不让启用失败。
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            try:
                await cur.execute(
                    "UPDATE session SET status = 'active', updated_at = now() "
                    "WHERE id = %s AND status = 'archived' "
                    "RETURNING id, last_agent_id, user_id, thread_id, title, status, kind",
                    (session_id,),
                )
            except UniqueViolation:
                logger.warning(
                    "cron session %s title collides on reactivation; dropping title", session_id
                )
                await cur.execute(
                    "UPDATE session SET status = 'active', title = NULL, updated_at = now() "
                    "WHERE id = %s AND status = 'archived' "
                    "RETURNING id, last_agent_id, user_id, thread_id, title, status, kind",
                    (session_id,),
                )
            return await cur.fetchone()


async def enabled_cron_jobs_with_agent_config(pool: AsyncConnectionPool) -> list[dict]:
    """启用中任务 + 其 agent 的 config_json（启动期 shell 能力扫描用）。

    含 config 树即可在应用层递归判 allow_shell，无需逐任务二次查询。
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT j.id, j.name, j.agent_id, a.config_json "
                "FROM cron_job j JOIN agent a ON a.id = j.agent_id "
                "WHERE j.enabled"
            )
            return await cur.fetchall()


# ============================ Jev decision audit ops =======================


async def insert_jev_decision(
    pool: AsyncConnectionPool,
    *,
    session_id: Optional[uuid.UUID],
    thread_id: str,
    agent_id: uuid.UUID,
    tool_call_id: str,
    mode: str,
    source: str,
    verdict: str,
    command: Optional[str] = None,
    state_rendered: Optional[str] = None,
    answers: Optional[dict] = None,
    latency_ms: Optional[int] = None,
    model: Optional[str] = None,
    decision_type: str = "shell_guard",
) -> None:
    """写入一行 Jev 判决审计（append-only；由 JevAuditSink 后台任务调用）。

    双用途：decision_type='shell_guard'（execute 命令门控，command 必填）/
    'rubric'（grader 分诊，command 为空、tool_call_id 承载判决键
    rubric:{grading_run_id}:{iteration}）。失败由调用方 suppress——审计缺失
    不阻断门控主链路（门控本身 fail-closed）。
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO jev_decision (session_id, thread_id, agent_id, tool_call_id, "
                "decision_type, mode, source, verdict, command, state_rendered, answers, latency_ms, model) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (session_id, thread_id, agent_id, tool_call_id, decision_type, mode,
                 source, verdict, command, state_rendered,
                 Json(answers) if answers is not None else None, latency_ms, model),
            )


# ============================ User / auth ops ==============================

_USER_PUBLIC_COLUMNS = (
    "id, username, nickname, is_manager, state, failed_attempts, locked_until, "
    "last_login_at, password_updated_at, created_at, updated_at"
)


async def get_user_by_username(pool: AsyncConnectionPool, username: str) -> Optional[dict]:
    """按登录名取用户（casefold 比较；含 password_hash，仅供认证路径）。"""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM app_user WHERE lower(username) = lower(%s)", (username,)
            )
            return await cur.fetchone()


async def get_login_user_by_nickname(pool: AsyncConnectionPool, nickname: str) -> Optional[dict]:
    """按昵称取登录候选（casefold 比较；含 password_hash，仅供认证路径）。"""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM app_user WHERE lower(nickname) = lower(%s)", (nickname,)
            )
            return await cur.fetchone()


async def get_user(pool: AsyncConnectionPool, user_id: uuid.UUID) -> Optional[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_USER_PUBLIC_COLUMNS} FROM app_user WHERE id = %s", (user_id,)
            )
            return await cur.fetchone()


async def list_users(pool: AsyncConnectionPool) -> list[dict]:
    """全量用户（不含 password_hash，管理面展示）。"""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_USER_PUBLIC_COLUMNS} FROM app_user ORDER BY created_at"
            )
            return await cur.fetchall()


async def get_user_by_nickname(pool: AsyncConnectionPool, nickname: str) -> Optional[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_USER_PUBLIC_COLUMNS} FROM app_user WHERE lower(nickname) = lower(%s)",
                (nickname,),
            )
            return await cur.fetchone()


async def count_managers(pool: AsyncConnectionPool) -> int:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT count(*) FROM app_user WHERE is_manager")
            row = await cur.fetchone()
    return row[0]


async def insert_user(
    pool: AsyncConnectionPool,
    username: str,
    nickname: str,
    password_hash: str,
    is_manager: bool = False,
) -> dict:
    """建户（引导/审批共用）。UniqueViolation（用户名/昵称任一）→ FileExistsError。"""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            try:
                await cur.execute(
                    "INSERT INTO app_user (username, nickname, password_hash, is_manager) "
                    "VALUES (%s, %s, %s, %s) "
                    f"RETURNING {_USER_PUBLIC_COLUMNS}",
                    (username, nickname, password_hash, is_manager),
                )
                return await cur.fetchone()
            except UniqueViolation:
                raise FileExistsError(username)


async def mark_login_success(pool: AsyncConnectionPool, user_id: uuid.UUID) -> None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE app_user SET failed_attempts = 0, locked_until = NULL, "
                "last_login_at = now() WHERE id = %s",
                (user_id,),
            )


async def mark_login_failure(
    pool: AsyncConnectionPool, user_id: uuid.UUID,
    failed_attempts: int, locked_until: Optional[object],
) -> None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE app_user SET failed_attempts = %s, locked_until = %s WHERE id = %s",
                (failed_attempts, locked_until, user_id),
            )


async def update_user_nickname(
    pool: AsyncConnectionPool, user_id: uuid.UUID, nickname: str
) -> Optional[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            try:
                await cur.execute(
                    "UPDATE app_user SET nickname = %s, updated_at = now() WHERE id = %s "
                    f"RETURNING {_USER_PUBLIC_COLUMNS}",
                    (nickname, user_id),
                )
                return await cur.fetchone()
            except UniqueViolation:
                raise FileExistsError(nickname)


async def update_user_password(
    pool: AsyncConnectionPool, user_id: uuid.UUID, password_hash: str
) -> None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE app_user SET password_hash = %s, password_updated_at = now(), "
                "updated_at = now() WHERE id = %s",
                (password_hash, user_id),
            )


async def set_user_state(pool: AsyncConnectionPool, user_id: uuid.UUID, state: str) -> None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE app_user SET state = %s, updated_at = now() WHERE id = %s",
                (state, user_id),
            )


async def unlock_user(pool: AsyncConnectionPool, user_id: uuid.UUID) -> None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE app_user SET failed_attempts = 0, locked_until = NULL "
                "WHERE id = %s",
                (user_id,),
            )


# ---- 注册申请 ----

_REQUEST_COLUMNS = (
    "id, type, payload, status, reason, reviewed_by, created_at, reviewed_at"
)


async def insert_auth_request(pool: AsyncConnectionPool, payload: dict) -> dict:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "INSERT INTO auth_request (payload) VALUES (%s) "
                f"RETURNING {_REQUEST_COLUMNS}",
                (Json(payload),),
            )
            return await cur.fetchone()


async def get_auth_request(
    pool: AsyncConnectionPool, request_id: uuid.UUID
) -> Optional[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_REQUEST_COLUMNS} FROM auth_request WHERE id = %s",
                (request_id,),
            )
            return await cur.fetchone()


async def list_auth_requests(
    pool: AsyncConnectionPool, status: Optional[str] = None
) -> list[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            where = " WHERE status = %s" if status else ""
            await cur.execute(
                f"SELECT {_REQUEST_COLUMNS} FROM auth_request{where} "
                "ORDER BY created_at DESC" + ("" if status else ""),
                (status,) if status else (),
            )
            return await cur.fetchall()


async def find_auth_requests_by_username(
    pool: AsyncConnectionPool, username: str
) -> list[dict]:
    """按 payload.username 查申请（判重：pending 占位 / rejected 占位均需可见）。"""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_REQUEST_COLUMNS} FROM auth_request "
                "WHERE payload->>'username' = %s ORDER BY created_at DESC",
                (username,),
            )
            return await cur.fetchall()


async def approve_register_request(
    pool: AsyncConnectionPool, request_id: uuid.UUID, reviewer_id: uuid.UUID
) -> dict:
    """原子审批：认领申请（pending → approved）+ 建户，任一步失败整体回滚。

    KeyError=申请不存在/已审；FileExistsError=用户名/昵称已被占用（审批期间
    被抢先）。返回新建用户行。
    """
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "UPDATE auth_request SET status = 'approved', reviewed_by = %s, "
                    "reviewed_at = now() WHERE id = %s AND status = 'pending' "
                    f"RETURNING {_REQUEST_COLUMNS}",
                    (reviewer_id, request_id),
                )
                req = await cur.fetchone()
                if req is None:
                    raise KeyError(f"申请不存在或已处理: {request_id}")
                p = req["payload"]
                try:
                    await cur.execute(
                        "INSERT INTO app_user (username, nickname, password_hash) "
                        f"VALUES (%s, %s, %s) RETURNING {_USER_PUBLIC_COLUMNS}",
                        (p["username"], p["nickname"], p["password_hash"]),
                    )
                    return await cur.fetchone()
                except UniqueViolation:
                    raise FileExistsError(p["username"])


async def reject_register_request(
    pool: AsyncConnectionPool, request_id: uuid.UUID, reviewer_id: uuid.UUID,
    reason: Optional[str],
) -> Optional[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "UPDATE auth_request SET status = 'rejected', reason = %s, "
                "reviewed_by = %s, reviewed_at = now() "
                "WHERE id = %s AND status = 'pending' "
                f"RETURNING {_REQUEST_COLUMNS}",
                (reason, reviewer_id, request_id),
            )
            return await cur.fetchone()


async def delete_rejected_auth_request(
    pool: AsyncConnectionPool, request_id: uuid.UUID
) -> Optional[dict]:
    """删除被拒申请行（状态卫兵内嵌 SQL，防 get/delete 间隙并发变更）。"""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "DELETE FROM auth_request WHERE id = %s AND status = 'rejected' "
                f"RETURNING {_REQUEST_COLUMNS}",
                (request_id,),
            )
            return await cur.fetchone()


# ---- 登录会话令牌 ----

async def insert_auth_session(
    pool: AsyncConnectionPool, token_hash: str, user_id: uuid.UUID, expires_at
) -> None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO auth_session (token_hash, user_id, expires_at) "
                "VALUES (%s, %s, %s)",
                (token_hash, user_id, expires_at),
            )


async def get_auth_session_user(
    pool: AsyncConnectionPool, token_hash: str
) -> Optional[dict]:
    """按令牌哈希取有效会话的用户行（过期行顺手清理）。"""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT u.id, u.username, u.nickname, u.is_manager, u.state, "
                "u.failed_attempts, u.locked_until, u.last_login_at, "
                "u.password_updated_at, u.created_at, u.updated_at, "
                "s.token_hash AS _token_hash, s.expires_at AS _expires_at "
                "FROM auth_session s JOIN app_user u ON u.id = s.user_id "
                "WHERE s.token_hash = %s",
                (token_hash,),
            )
            row = await cur.fetchone()
            if row is not None and row["_expires_at"] <= datetime.now(timezone.utc):
                await cur.execute(
                    "DELETE FROM auth_session WHERE token_hash = %s", (token_hash,)
                )
                return None
            return row


async def delete_auth_session(pool: AsyncConnectionPool, token_hash: str) -> bool:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM auth_session WHERE token_hash = %s", (token_hash,)
            )
            return cur.rowcount > 0


async def delete_user_sessions(pool: AsyncConnectionPool, user_id: uuid.UUID) -> int:
    """吊销该用户全部会话（改密/停用/管理员重置密码时调用）。"""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM auth_session WHERE user_id = %s", (user_id,)
            )
            return cur.rowcount


# ============================ 存量 user_id 对齐迁移 =========================
# 一次性迁移：session/upload/cron_job.user_id VARCHAR(客户端字符串) → UUID FK。
# 幂等探测：session.user_id 列类型为 uuid 即跳过（新装 DDL 直接建 uuid）。
# fail-visible：存在未映射旧值即 RuntimeError（列出值），绝不静默丢行。


async def _session_user_id_is_uuid(pool: AsyncConnectionPool) -> bool:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = 'session' AND column_name = 'user_id'"
            )
            row = await cur.fetchone()
    return bool(row) and row[0] == "uuid"


async def _distinct_legacy_user_ids(pool: AsyncConnectionPool) -> list[str]:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT DISTINCT user_id FROM session "
                "UNION SELECT DISTINCT user_id FROM upload "
                "UNION SELECT DISTINCT user_id FROM cron_job"
            )
            rows = await cur.fetchall()
    return sorted(r[0] for r in rows)


def _mapping_values_clause(mapping: dict[str, uuid.UUID]) -> tuple[str, list]:
    """(VALUES ('old','uuid'),...) 的 SQL 片段 + 参数列表。"""
    parts = ", ".join(["(%s, %s::uuid)"] * len(mapping))
    params: list = []
    for old, new in mapping.items():
        params.extend([old, str(new)])
    return parts, params


async def migrate_legacy_user_ids(
    pool: AsyncConnectionPool,
    mapping: dict[str, uuid.UUID],
    workspace_root,
) -> None:
    """列类型对齐 + 记忆命名空间/工作区目录跟随。

    mapping: 旧 user_id 字符串 → 目标 app_user uuid。合并冲突的保全规则：
    - 同标题活跃会话：保留 updated_at 最新，其余归档（行与 checkpoint 保留）；
    - 记忆同 key（store prefix 合并）：保留最新，其余 key 加 .merged.<id前8> 后缀；
    - 同名 cron 任务：旧任务改名 + 停用（防双触发）。
    工作区目录 rename 在 DB 事务提交后执行（best-effort，失败仅告警）。
    """
    import logging as _logging
    import shutil as _shutil
    from pathlib import Path as _Path

    log = _logging.getLogger("goduck.migrate")
    if await _session_user_id_is_uuid(pool):
        return
    if not mapping:
        raise RuntimeError(
            "session.user_id 仍为 VARCHAR（存量库），但未配置 LEGACY_USER_MAP —— "
            "请在 .env 提供旧值→用户名映射后重启"
        )
    legacy_values = await _distinct_legacy_user_ids(pool)
    unmapped = [v for v in legacy_values if v not in mapping]
    if unmapped:
        raise RuntimeError(
            f"存量 user_id 未在 LEGACY_USER_MAP 中映射: {unmapped} —— 补齐映射后重启"
        )

    values_sql, values_params = _mapping_values_clause(mapping)
    log.info("aligning legacy user_id columns: %s", mapping)

    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                # —— 合并冲突保全（须在列类型切换前，按旧值分组判定）——
                await cur.execute(
                    "WITH m(old, new) AS (VALUES " + values_sql + "), "
                    "ranked AS ("
                    "  SELECT s.id, row_number() OVER ("
                    "    PARTITION BY m.new, s.title ORDER BY s.updated_at DESC, s.id) rn"
                    "  FROM session s JOIN m ON s.user_id = m.old"
                    "  WHERE s.status = 'active' AND s.title IS NOT NULL AND s.title <> ''"
                    ") "
                    "UPDATE session SET status = 'archived', updated_at = now() "
                    "WHERE id IN (SELECT id FROM ranked WHERE rn > 1)",
                    values_params,
                )
                if cur.rowcount:
                    log.info("archived %d title-colliding session(s) (keep newest)", cur.rowcount)
                await cur.execute(
                    "WITH m(old, new) AS (VALUES " + values_sql + "), "
                    "ranked AS ("
                    "  SELECT j.id, row_number() OVER ("
                    "    PARTITION BY m.new, j.name ORDER BY j.updated_at DESC, j.id) rn"
                    "  FROM cron_job j JOIN m ON j.user_id = m.old"
                    ") "
                    "UPDATE cron_job SET name = name || '.merged.' || substr(id::text, 1, 8), "
                    "enabled = false, updated_at = now() "
                    "WHERE id IN (SELECT id FROM ranked WHERE rn > 1)",
                    values_params,
                )
                if cur.rowcount:
                    log.warning(
                        "renamed+disabled %d name-colliding cron job(s) (keep newest)",
                        cur.rowcount,
                    )
                # store 主键 = (prefix, key)：同 key 合并保留 updated_at 最新，
                # 旧行 key 追加 .merged.<序号>（内容保留、文件可见）
                await cur.execute(
                    "WITH m(old, new) AS (VALUES " + values_sql + "), "
                    "srow AS ("
                    "  SELECT st.prefix, st.key, st.updated_at,"
                    "         m.new || substr(st.prefix, length(m.old) + 1) AS tgt"
                    "  FROM store st JOIN m ON st.prefix LIKE m.old || '.%%'"
                    "), "
                    "ranked AS ("
                    "  SELECT prefix, key, row_number() OVER ("
                    "    PARTITION BY tgt, key ORDER BY updated_at DESC) rn"
                    "  FROM srow"
                    ") "
                    "UPDATE store SET key = store.key || '.merged.' || ranked.rn "
                    "FROM ranked "
                    "WHERE store.prefix = ranked.prefix AND store.key = ranked.key "
                    "AND ranked.rn > 1",
                    values_params,
                )
                if cur.rowcount:
                    log.info("renamed %d key-colliding memory entr(y/ies)", cur.rowcount)
                await cur.execute(
                    "UPDATE store SET prefix = m.new || substr(store.prefix, length(m.old) + 1) "
                    "FROM (VALUES " + values_sql + ") AS m(old, new) "
                    "WHERE store.prefix LIKE m.old || '.%%'",
                    values_params,
                )

                # —— 列类型切换：三表统一"加新列→映射→断言→换列→FK/索引重建"
                for table in ("session", "upload", "cron_job"):
                    await cur.execute(
                        f"ALTER TABLE {table} RENAME COLUMN user_id TO user_id_legacy"
                    )
                    await cur.execute(f"ALTER TABLE {table} ADD COLUMN user_id UUID")
                    await cur.execute(
                        f"UPDATE {table} t SET user_id = m.new "
                        f"FROM (VALUES {values_sql}) AS m(old, new) "
                        f"WHERE t.user_id_legacy = m.old",
                        values_params,
                    )
                    await cur.execute(
                        f"SELECT DISTINCT user_id_legacy FROM {table} WHERE user_id IS NULL"
                    )
                    leftover = [r[0] for r in await cur.fetchall()]
                    if leftover:
                        raise RuntimeError(
                            f"{table}.user_id 存在未映射旧值 {leftover}（事务回滚，未产生任何变更）"
                        )
                    await cur.execute(
                        f"ALTER TABLE {table} ALTER COLUMN user_id SET NOT NULL"
                    )
                    await cur.execute(f"ALTER TABLE {table} DROP COLUMN user_id_legacy")

                # DROP legacy 列会连带删掉建在其上的索引/唯一约束，按新列重建
                await cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_session_user ON session(user_id)"
                )
                await cur.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_session_user_title_active "
                    "ON session(user_id, title) "
                    "WHERE status = 'active' AND title IS NOT NULL AND title <> ''"
                )
                await cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_upload_user ON upload(user_id)"
                )
                await cur.execute(
                    "ALTER TABLE cron_job DROP CONSTRAINT IF EXISTS cron_job_user_id_name_key"
                )
                await cur.execute(
                    "ALTER TABLE cron_job ADD CONSTRAINT cron_job_user_id_name_key "
                    "UNIQUE (user_id, name)"
                )
                for table in ("session", "upload", "cron_job"):
                    await cur.execute(
                        f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_user_id_fkey"
                    )
                    await cur.execute(
                        f"ALTER TABLE {table} ADD CONSTRAINT {table}_user_id_fkey "
                        f"FOREIGN KEY (user_id) REFERENCES app_user(id)"
                    )

    # —— 工作区目录跟随（文件系统，事务外 best-effort）——
    root = _Path(workspace_root)
    for old, new in mapping.items():
        src, dst = root / old, root / str(new)
        if src.exists() and src != dst:
            if dst.exists():
                log.warning(
                    "workspace dir clash: %s and %s both exist; keep both (rename manually)",
                    src, dst,
                )
                continue
            _shutil.move(str(src), str(dst))
            log.info("workspace dir renamed: %s -> %s", src.name, dst.name)
    log.info("legacy user_id alignment done")
