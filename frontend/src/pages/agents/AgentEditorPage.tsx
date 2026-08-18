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
import { useModels } from '@/hooks/useModels'
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
  const errors = useAgentEditorStore((s) => s.validationErrors)
  const configDirty = useAgentEditorStore((s) => s.configDirty)
  const initNew = useAgentEditorStore((s) => s.initNew)
  const loadFromAgent = useAgentEditorStore((s) => s.loadFromAgent)
  const setName = useAgentEditorStore((s) => s.setName)
  const setDescription = useAgentEditorStore((s) => s.setDescription)
  const setModelId = useAgentEditorStore((s) => s.setModelId)
  const setValidation = useAgentEditorStore((s) => s.setValidation)

  const [histOpen, setHistOpen] = useState(false)
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
  useEffect(() => {
    if (!nodes || !configDirty) return
    let config: AgentConfig
    try {
      config = toConfigTree(nodes)
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
  }, [nodes, configDirty, validate, setValidation])

  const handleSave = () => {
    if (!nodes) return
    let config: AgentConfig
    try {
      config = toConfigTree(nodes)
    } catch {
      return toastError('配置无效：缺少主代理节点')
    }
    if (!name.trim()) return toastError('请填写智能体名称')
    if (!modelId) return toastError('请绑定模型')
    const user = currentUserId

    const doUpdate = () =>
      updateAgent.mutateAsync({
        id: id as UUID,
        body: {
          config,
          model_id: modelId,
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
          creator: user,
        })
        .then((detail) => navigate(`/agents/${detail.id}`))
      return
    }
    modal.confirm({
      title: '确认更新智能体？',
      content: '更新将级联删除该智能体的全部旧会话（配置历史保留可还原）。',
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
          <div className="editor-field">
            <span className="editor-field__label">描述</span>
            <Input
              variant="filled"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="一句话说明（可选）"
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
