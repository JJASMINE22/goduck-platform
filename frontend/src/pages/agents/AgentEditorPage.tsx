/* 智能体编辑器（画布版）—— 三栏：模块面板 | 节点画布 | 节点详情面板。
   草稿由 agentEditorStore（归一化 nodes）持有（准则 F8）。保存前 toConfigTree(nodes) 序列化为
   AgentConfig 树：新建 POST /agents；更新 PUT（二次确认级联删会话）。后端契约不变。
   实时校验走 POST /agents/validate（防抖 600ms，准则 F6）。 */
import { App as AntApp, Button, Drawer, Empty, Input, List, Popover, Select, Space, Tag } from 'antd'
import { type ReactNode, useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, History, RotateCcw, Save } from 'lucide-react'
import {
  useAgent,
  useAgentHistory,
  useCreateAgent,
  useRestoreAgent,
  useUpdateAgent,
  useValidateAgent,
} from '@/hooks/useAgents'
import { useModels, useVisionModels } from '@/hooks/useModels'
import { useDragResize } from '@/hooks/useDragResize'
import { useAgentEditorStore } from '@/store/agentEditorStore'
import { usePageHeaderStore } from '@/store/pageHeaderStore'
import { useCurrentUser } from '@/store/userStore'
import { toConfigTree } from '@/features/agent-editor/graphModel'
import { NodePalette } from '@/features/agent-editor/canvas/NodePalette'
import { AgentCanvas } from '@/features/agent-editor/canvas/AgentCanvas'
import { NodeDetailPanel } from '@/features/agent-editor/NodeDetailPanel'
import { PageLoading } from '@/components/feedback/states'
import { formatDateTime } from '@/lib/format'
import { toastError } from '@/lib/antdStatic'
import type { AgentConfig, UUID } from '@/types'

export function AgentEditorPage(): ReactNode {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const isCreate = !id

  const agentQ = useAgent(id ?? null)
  const models = useModels()
  const visionModels = useVisionModels()
  const validate = useValidateAgent()
  const createAgent = useCreateAgent()
  const updateAgent = useUpdateAgent()
  const restoreAgent = useRestoreAgent()
  const historyQ = useAgentHistory(id ?? null)
  const { modal } = AntApp.useApp()
  const currentUserId = useCurrentUser((s) => s.userId)
  const setHeader = usePageHeaderStore((s) => s.setHeader)

  const nodes = useAgentEditorStore((s) => s.nodes)
  const agentIdInStore = useAgentEditorStore((s) => s.agentId)
  const name = useAgentEditorStore((s) => s.name)
  const description = useAgentEditorStore((s) => s.description)
  const modelId = useAgentEditorStore((s) => s.modelId)
  const visionModelId = useAgentEditorStore((s) => s.visionModelId)
  const kind = useAgentEditorStore((s) => s.kind)
  const errors = useAgentEditorStore((s) => s.validationErrors)
  const configDirty = useAgentEditorStore((s) => s.configDirty)
  const initNew = useAgentEditorStore((s) => s.initNew)
  const loadFromAgent = useAgentEditorStore((s) => s.loadFromAgent)
  const setName = useAgentEditorStore((s) => s.setName)
  const setDescription = useAgentEditorStore((s) => s.setDescription)
  const setModelId = useAgentEditorStore((s) => s.setModelId)
  const setVisionModelId = useAgentEditorStore((s) => s.setVisionModelId)
  const setKind = useAgentEditorStore((s) => s.setKind)
  const clearCronIncompatibleCapabilities = useAgentEditorStore(
    (s) => s.clearCronIncompatibleCapabilities,
  )
  const setValidation = useAgentEditorStore((s) => s.setValidation)

  const [histOpen, setHistOpen] = useState(false)
  // 主代理（根节点）附件开关：视觉下挂的渲染与保存共同前置
  //（三门均先查 allow_attachments，未开启时下挂永不可达）
  const rootAllowAttachments = !!nodes?.find((n) => n.parentId === null)?.allow_attachments
  // 右侧详情面板宽度：手柄在其左缘（edge='left'，向左拖变宽）；双击重置；宽度持久化
  const {
    size: detailWidth,
    onResizeStart: onDetailResizeStart,
    reset: resetDetailWidth,
  } = useDragResize({
    initial: 360,
    min: 280,
    max: 720,
    edge: 'left',
    storageKey: 'agent-detail-width',
  })

  // —— 初始化草稿（保留跨页面切换的进度）——
  // 新建：仅当无草稿、或草稿属于已存在智能体时才初始化，避免切到设置页再回来被清空
  useEffect(() => {
    if (isCreate && (nodes === null || agentIdInStore !== null)) initNew()
  }, [isCreate, nodes, agentIdInStore, initNew])

  // 编辑：仅当尚未加载该智能体时才灌入，避免覆盖未保存的编辑
  useEffect(() => {
    if (!isCreate && agentQ.data && agentIdInStore !== id) loadFromAgent(agentQ.data)
  }, [isCreate, id, agentQ.data, agentIdInStore, loadFromAgent])

  // —— 实时校验（防抖 600ms）：nodes → AgentConfig → /validate ——
  // 仅在「配置树已被改动」后触发：顶层名称/描述/模型改动不算配置改动，不打校验请求；
  // 未填 system_prompt 的空草稿不会即时弹红条，校验作为保存门禁而非持续报警（准则 F6）。
  // usage 随 kind 盖章进根节点：后端「usage=cron ∧ 全树 allow_shell」校验需看到它。
  useEffect(() => {
    if (!nodes || !configDirty) return
    let config: AgentConfig
    try {
      config = { ...toConfigTree(nodes), usage: kind }
    } catch {
      return
    }
    const t = setTimeout(() => {
      validate
        .mutateAsync({ config })
        .then((res) =>
          setValidation(res.valid ? [] : res.errors ?? [], res.valid ? res.mermaid ?? null : null),
        )
        .catch(() => {
          /* 网络异常由全局提示 */
        })
    }, 600)
    return () => clearTimeout(t)
  }, [nodes, configDirty, kind, validate, setValidation])

  // 切换为定时任务用途：既有配置含 cron 不兼容能力（shell/工具审批/附件/知识库）
  // 时确认后预清除（后端 validator 仍为最终权威，覆盖 API 直写路径）
  const handleKindChange = (v: 'chat' | 'cron') => {
    const root = nodes?.find((n) => n.parentId === null)
    const incompatible =
      (nodes ?? []).some((n) => n.allow_shell || n.interrupt_on) ||
      !!(root?.allow_attachments || root?.allow_knowledge)
    if (v === 'cron' && incompatible) {
      modal.confirm({
        title: '切换为定时任务智能体？',
        content:
          '将关闭全部节点的 shell 与工具审批能力，并关闭附件、知识库能力（定时任务无人值守，无审批通道；附件/知识库暂不支持）。',
        okText: '关闭这些能力',
        cancelText: '取消',
        onOk: () => {
          clearCronIncompatibleCapabilities()
          setKind(v)
        },
      })
      return
    }
    setKind(v)
  }

  const handleSave = () => {
    if (!nodes) return
    let config: AgentConfig
    try {
      config = { ...toConfigTree(nodes), usage: kind }
    } catch {
      return toastError('配置无效：缺少主代理节点')
    }
    if (!name.trim()) return toastError('请填写智能体名称')
    if (!modelId) return toastError('请绑定模型')
    // 前端防护（后端保存门为本体）：基座自带视觉、或主代理未开启附件能力时
    // 强制清空下挂（下拉隐藏后 store 可能残留旧值，保存时自愈为 null）
    const baseModel = (models.data ?? []).find((m) => m.id === modelId)
    const effectiveVision =
      baseModel?.supports_vision || !rootAllowAttachments ? null : visionModelId
    const user = currentUserId

    const doUpdate = () =>
      updateAgent.mutateAsync({
        id: id as UUID,
        body: {
          config,
          model_id: modelId,
          vision_model_id: effectiveVision,
          name: name.trim(),
          description: description.trim() || null,
          updater: user,
        },
      })

    if (isCreate) {
      createAgent
        .mutateAsync({
          name: name.trim(),
          description: description.trim() || null,
          config,
          model_id: modelId,
          vision_model_id: effectiveVision,
          kind,
          creator: user,
        })
        .then((detail) => navigate(`/agents/${detail.id}`))
      return
    }
    // 更新不携带 kind：用途类型创建后不可变（后端 AgentUpdate 无此字段）
    modal.confirm({
      title: '确认更新智能体？',
      content:
        kind === 'cron'
          ? '更新将级联删除该智能体的全部旧会话，并自动停用绑定它的定时任务（确认后可重新启用）。配置历史保留可还原。'
          : '更新将级联删除该智能体的全部旧会话（配置历史保留可还原）。',
      okText: '确认更新',
      cancelText: '取消',
      onOk: doUpdate,
    })
  }

  const handleRestore = (historyId: UUID) => {
    modal.confirm({
      title: '还原到此历史版本？',
      content: '将用历史快照覆盖当前配置，并级联删除旧会话。',
      okText: '还原',
      cancelText: '取消',
      onOk: async () => {
        await restoreAgent.mutateAsync({
          id: id as UUID,
          body: { history_id: historyId, operator: currentUserId },
        })
        await agentQ.refetch()
        setHistOpen(false)
      },
    })
  }

  // 标题/动作上提顶部 Header 卡（与列表页/会话页一致的模块化布局）。
  // 每次渲染重注册：名称输入即时反映到标题，保存按钮回调闭包保持新鲜。
  useEffect(() => {
    setHeader({
      title: (
        <Space>
          <Button icon={<ArrowLeft size={14} />} onClick={() => navigate('/agents')}>
            返回
          </Button>
          <span style={{ fontSize: 15, fontWeight: 600 }}>
            {isCreate ? '新建智能体' : `编辑智能体：${name || '未命名'}`}
          </span>
        </Space>
      ),
      extra: (
        <Space>
          {!isCreate ? (
            <Button
              icon={<History size={14} />}
              onClick={() => {
                historyQ.refetch()
                setHistOpen(true)
              }}
            >
              配置历史
            </Button>
          ) : null}
          <Button
            type="primary"
            icon={<Save size={14} />}
            loading={createAgent.isPending || updateAgent.isPending}
            onClick={handleSave}
          >
            保存
          </Button>
        </Space>
      ),
    })
    return () => setHeader(null)
  })

  if (!isCreate && agentQ.isLoading) return <PageLoading />
  if (!nodes) return <PageLoading />

  const modelOptions = (models.data ?? []).map((m) => ({
    value: m.id,
    label: `${m.name}（${m.model_type}）${m.supports_vision ? ' · 视觉' : ''}`,
    disabled: !m.enabled,
  }))

  // 视觉下挂选择器：仅在「主代理已开启附件能力 + 已选基座 + 基座不支持视觉」
  // 三条件齐备后渲染（选基座前不渲染；基座自带视觉互斥隐藏；未开附件能力时
  // 下挂不可达同样隐藏——后端保存门为本体强制）。候选走
  // GET /models?supports_vision=true&enable_thinking=false（已启用 + 视觉 +
  // 显式关思考——描述任务约定，注册表单有对应提示）。
  const baseModel = (models.data ?? []).find((m) => m.id === modelId)
  const showVisionMount = rootAllowAttachments && !!baseModel && !baseModel.supports_vision
  const visionModelOptions = (visionModels.data ?? []).map((m) => ({
    value: m.id,
    label: `${m.name}（${m.model_type}）`,
  }))

  // 校验状态收敛为工具条上的 Tag：未改动保持中性；未通过时 Popover 展开错误清单
  const validationTag = !configDirty ? (
    <Tag style={{ marginInlineEnd: 0 }}>未校验</Tag>
  ) : errors.length ? (
    <Popover
      trigger={['hover', 'click']}
      placement="bottomLeft"
      title="配置校验未通过"
      content={
        <ul style={{ margin: 0, paddingLeft: 18, maxWidth: 360 }}>
          {errors.map((e, i) => (
            <li key={i}>{e}</li>
          ))}
        </ul>
      }
    >
      <Tag color="error" style={{ marginInlineEnd: 0, cursor: 'pointer' }}>
        校验未通过 · {errors.length}
      </Tag>
    </Popover>
  ) : (
    <Tag color="success" style={{ marginInlineEnd: 0 }}>
      校验通过
    </Tag>
  )

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      {/* 单一工作区卡：顶部基本信息条（外置标签 + 内联编辑 + 校验状态）+ 三区
          （模块面板 | 画布 | 详情）。标题/历史/保存已上提 Header 卡。 */}
      <div className="editor-workspace">
        <div className="editor-workspace__fields">
          {/* 外置标签：值填入后仍可见字段含义（placeholder 会被值顶掉，
              编辑已有智能体时无法仅靠 placeholder 判断输入类型） */}
          <div className="editor-field editor-field--required">
            <span className="editor-field__label">名称</span>
            <Input
              variant="filled"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="如 代码评审助手"
              style={{ width: 176, fontWeight: 600 }}
            />
          </div>
          {/* 用途类型：仅新建可选（创建后不可变）；编辑态显示徽标提示。
              cron 型才可绑定定时任务，chat 型才可建聊天会话（绑定面互斥）；
              切 cron 时若全树含 allow_shell 先确认再预清除 */}
          <div className="editor-field">
            <span className="editor-field__label">用途</span>
            {isCreate ? (
              <Select
                style={{ width: 150 }}
                value={kind}
                onChange={(v) => handleKindChange(v)}
                options={[
                  { value: 'chat', label: '聊天会话' },
                  { value: 'cron', label: '定时任务' },
                ]}
              />
            ) : (
              <Tag color={kind === 'cron' ? 'geekblue' : 'green'} style={{ marginInlineEnd: 0 }}>
                {kind === 'cron' ? '定时任务（不可改）' : '聊天会话（不可改）'}
              </Tag>
            )}
          </div>
          <div className="editor-field">
            <span className="editor-field__label">描述</span>
            {/* 单行起步、原生双向拖拉（右下角，宽×高）——较长描述无需在窄框内滚动查看。
                此处维持原设计（不加抓手条）：字段条空间紧凑，抓手条会呈"下拉"误语义 */}
            <Input.TextArea
              variant="filled"
              rows={1}
              className="drag-ta drag-ta--bar"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="一句话说明（可选，较长可拖右下角调大）"
              style={{ width: 184 }}
            />
          </div>
          <div className="editor-field editor-field--required">
            <span className="editor-field__label">模型</span>
            <Select
              style={{ width: 208 }}
              placeholder="选择已注册模型"
              value={modelId ?? undefined}
              onChange={(v) => setModelId(v)}
              options={modelOptions}
              loading={models.isLoading}
              showSearch
              optionFilterProp="label"
            />
          </div>
          {showVisionMount ? (
            <div className="editor-field">
              <span className="editor-field__label">视觉下挂</span>
              <Select
                style={{ width: 208 }}
                placeholder="无（不能上传图像）"
                value={visionModelId ?? undefined}
                onChange={(v) => setVisionModelId(v ?? null)}
                onClear={() => setVisionModelId(null)}
                options={visionModelOptions}
                allowClear
                loading={visionModels.isLoading}
                showSearch
                optionFilterProp="label"
              />
            </div>
          ) : null}
          {validationTag}
        </div>

        {/* 三区：去卡壳靠背景对比分区；详情宽度可拖拽（覆盖式手柄），双击重置 */}
        <div className="editor-workspace__main">
          <NodePalette />
          <AgentCanvas />
          <div
            style={{
              width: detailWidth,
              flexShrink: 0,
              position: 'relative',
              minWidth: 0,
              display: 'flex',
              flexDirection: 'column',
            }}
          >
            <div
              className="panel-resizer panel-resizer--overlay"
              onMouseDown={onDetailResizeStart}
              onDoubleClick={resetDetailWidth}
              title="拖拽调整详情面板宽度（双击重置）"
            />
            <NodeDetailPanel />
          </div>
        </div>
      </div>

      {/* 配置历史 / 还原 */}
      <Drawer title="配置历史（可还原）" open={histOpen} onClose={() => setHistOpen(false)} width={440}>
        {historyQ.isLoading ? (
          <PageLoading />
        ) : !historyQ.data?.length ? (
          <Empty description="暂无历史" />
        ) : (
          <List
            dataSource={historyQ.data}
            renderItem={(item) => (
              <List.Item
                actions={[
                  <Button key="restore" size="small" icon={<RotateCcw size={13} />} onClick={() => handleRestore(item.id)}>
                    还原
                  </Button>,
                ]}
              >
                <List.Item.Meta
                  title={
                    <Space>
                      <Tag color={item.action === 'create' ? 'blue' : 'green'}>{item.action === 'create' ? '创建' : '更新'}</Tag>
                      <span>{item.operator}</span>
                    </Space>
                  }
                  description={formatDateTime(item.created_at)}
                />
              </List.Item>
            )}
          />
        )}
      </Drawer>
    </div>
  )
}
