/* 智能体编辑器（画布版）—— 三栏：模块面板 | 节点画布 | 节点详情面板。
   草稿由 agentEditorStore（归一化 nodes）持有（准则 F8）。保存前 toConfigTree(nodes) 序列化为
   AgentConfig 树：新建 POST /agents；更新 PUT（二次确认级联删会话）。后端契约不变。
   实时校验走 POST /agents/validate（防抖 600ms，准则 F6）。 */
import { Alert, App as AntApp, Button, Card, Drawer, Empty, Flex, Input, List, Select, Space, Tag, Typography } from 'antd'
import { type MouseEvent as ReactMouseEvent, type ReactNode, useEffect, useRef, useState } from 'react'
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
import { useAgentEditorStore } from '@/store/agentEditorStore'
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
  const [detailWidth, setDetailWidth] = useState(360)
  const draggingRef = useRef(false)

  const onResizerMouseDown = (e: ReactMouseEvent) => {
    e.preventDefault()
    draggingRef.current = true
    const startX = e.clientX
    const startWidth = detailWidth
    const onMove = (ev: MouseEvent) => {
      if (!draggingRef.current) return
      // 向左拖(clientX 减小) → 详情面板变宽
      setDetailWidth(Math.min(720, Math.max(280, startWidth + (startX - ev.clientX))))
    }
    const onUp = () => {
      draggingRef.current = false
      document.body.style.cursor = ''
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
    }
    document.body.style.cursor = 'col-resize'
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }

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
        body: { config, model_id: modelId, description: description.trim() || null, updater: user },
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

  if (!isCreate && agentQ.isLoading) return <PageLoading />
  if (!nodes) return <PageLoading />

  const modelOptions = (models.data ?? []).map((m) => ({
    value: m.id,
    label: `${m.name}（${m.model_type}）${m.supports_vision ? ' · 视觉' : ''}`,
    disabled: !m.enabled,
  }))

  return (
    <Flex vertical gap={12} style={{ height: '100%', padding: '16px 20px', overflow: 'auto' }}>
      <Flex justify="space-between" align="center">
        <Space>
          <Button icon={<ArrowLeft size={14} />} onClick={() => navigate('/agents')}>
            返回列表
          </Button>
          <Typography.Text strong>{isCreate ? '新建智能体' : `编辑：${name}`}</Typography.Text>
        </Space>
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
      </Flex>

      {/* 基本信息 + 绑定模型 */}
      <Card size="small">
        <Flex gap={24} wrap="wrap" align="center">
          <FieldRow label="名称（唯一）">
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="如 代码评审助手" style={{ width: 220 }} />
          </FieldRow>
          <FieldRow label="描述">
            <Input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="一句话说明" style={{ width: 240 }} />
          </FieldRow>
          <FieldRow label="绑定模型（必选）">
            <Select
              style={{ width: 260 }}
              placeholder="选择已注册模型"
              value={modelId ?? undefined}
              onChange={(v) => setModelId(v)}
              options={modelOptions}
              loading={models.isLoading}
              showSearch
              optionFilterProp="label"
            />
          </FieldRow>
        </Flex>
      </Card>

      {/* 校验状态：未改动配置前保持中性提示，避免空表单红/绿闪烁；改动后才红/绿 */}
      {!configDirty ? (
        <Alert type="info" showIcon message="编辑配置后将自动校验（配置树改动后触发）" />
      ) : errors.length ? (
        <Alert
          type="error"
          showIcon
          message="配置校验未通过"
          description={
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {errors.map((e, i) => (
                <li key={i}>{e}</li>
              ))}
            </ul>
          }
        />
      ) : (
        <Alert type="success" showIcon message="配置校验通过" />
      )}

      {/* 三栏：模块面板 | 画布 | 详情（详情宽度可拖拽） */}
      <Flex gap={12} style={{ flex: 1, minHeight: 320 }}>
        <NodePalette />
        <AgentCanvas />
        <div className="panel-resizer" onMouseDown={onResizerMouseDown} />
        <div style={{ width: detailWidth, flexShrink: 0 }}>
          <NodeDetailPanel />
        </div>
      </Flex>

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
                      <Tag color={item.action === 'create' ? 'blue' : 'green'}>{item.action}</Tag>
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
    </Flex>
  )
}

function FieldRow({ label, children }: { label: string; children: ReactNode }): ReactNode {
  return (
    <Flex align="center" gap={8}>
      <div style={{ width: 90, flexShrink: 0, fontSize: 13, color: 'var(--color-text-secondary)' }}>{label}</div>
      {children}
    </Flex>
  )
}
