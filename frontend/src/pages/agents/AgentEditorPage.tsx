/* 智能体编辑器（DESIGN §10.1）—— AgentConfig 树形编辑 + 实时 validate + mermaid 预览 + 历史/还原。
   草稿由 agentEditorStore 持有（准则 F8）。保存：新建 POST /agents；更新 PUT（二次确认级联删会话）。 */
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Drawer,
  Empty,
  Flex,
  Input,
  List,
  Select,
  Space,
  Tag,
  Typography,
} from 'antd'
import { type ReactNode, useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, History, RotateCcw, Save, Sparkles } from 'lucide-react'
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
import { NodeEditor } from '@/features/agent-editor/NodeEditor'
import { MermaidPreview } from '@/features/topology/MermaidPreview'
import { PageLoading } from '@/components/feedback/states'
import { formatDateTime } from '@/lib/format'
import { toastError } from '@/lib/antdStatic'
import type { UUID } from '@/types'

const USER_KEY = 'goduck.user_id'
const currentUser = () => localStorage.getItem(USER_KEY) || 'admin'

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

  const config = useAgentEditorStore((s) => s.config)
  const name = useAgentEditorStore((s) => s.name)
  const description = useAgentEditorStore((s) => s.description)
  const modelId = useAgentEditorStore((s) => s.modelId)
  const errors = useAgentEditorStore((s) => s.validationErrors)
  const mermaid = useAgentEditorStore((s) => s.mermaidPreview)
  const initNew = useAgentEditorStore((s) => s.initNew)
  const loadFromAgent = useAgentEditorStore((s) => s.loadFromAgent)
  const setName = useAgentEditorStore((s) => s.setName)
  const setDescription = useAgentEditorStore((s) => s.setDescription)
  const setModelId = useAgentEditorStore((s) => s.setModelId)
  const setValidation = useAgentEditorStore((s) => s.setValidation)

  const [histOpen, setHistOpen] = useState(false)

  // —— 初始化草稿 ——
  useEffect(() => {
    if (isCreate) initNew()
  }, [isCreate, initNew])

  useEffect(() => {
    if (!isCreate && agentQ.data) loadFromAgent(agentQ.data)
  }, [isCreate, agentQ.data, loadFromAgent])

  // —— 实时校验（防抖 600ms）——
  useEffect(() => {
    if (!config) return
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
  }, [config, validate, setValidation])

  const handleSave = () => {
    if (!config) return
    if (!name.trim()) return toastError('请填写智能体名称')
    if (!modelId) return toastError('请绑定模型')
    const user = currentUser()

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
          body: { history_id: historyId, operator: currentUser() },
        })
        await agentQ.refetch()
        setHistOpen(false)
      },
    })
  }

  if (!isCreate && agentQ.isLoading) return <PageLoading />
  if (!config) return <PageLoading />

  const modelOptions = (models.data ?? []).map((m) => ({
    value: m.id,
    label: `${m.name}（${m.model_type}）${m.supports_vision ? ' · 视觉' : ''}`,
    disabled: !m.enabled,
  }))

  return (
    <Flex vertical gap={16} style={{ height: '100%' }}>
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

      <Flex gap={16} style={{ flex: 1, minHeight: 0 }}>
        {/* 左：表单 */}
        <div style={{ flex: 1, minWidth: 0, overflow: 'auto', paddingRight: 4 }}>
          <Card title="基本信息" size="small" style={{ marginBottom: 12 }}>
            <Flex vertical gap={12}>
              <FieldRow label="名称（唯一）">
                <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="如 代码评审助手" />
              </FieldRow>
              <FieldRow label="描述">
                <Input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="一句话说明" />
              </FieldRow>
              <FieldRow label="绑定模型（必选）">
                <Select
                  style={{ width: '100%' }}
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

          <Card title="配置树（AgentConfig）" size="small">
            <NodeEditor path={[]} defaultOpen />
          </Card>
        </div>

        {/* 右：实时预览 + 校验 */}
        <div style={{ width: 380, flexShrink: 0, overflow: 'auto' }}>
          <Card
            size="small"
            title={
              <span>
                <Sparkles size={14} style={{ verticalAlign: '-2px', marginRight: 6 }} />
                拓扑预览 & 校验
              </span>
            }
          >
            {errors.length ? (
              <Alert
                type="error"
                style={{ marginBottom: 12 }}
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
              <Alert type="success" message="配置校验通过" style={{ marginBottom: 12 }} />
            )}
            <MermaidPreview code={mermaid} />
          </Card>
        </div>
      </Flex>

      {/* 配置历史 / 还原 */}
      <Drawer
        title="配置历史（可还原）"
        open={histOpen}
        onClose={() => setHistOpen(false)}
        width={440}
      >
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
                  <Button
                    key="restore"
                    size="small"
                    icon={<RotateCcw size={13} />}
                    onClick={() => handleRestore(item.id)}
                  >
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
    <Flex align="center" gap={12}>
      <div style={{ width: 110, flexShrink: 0, fontSize: 13, color: 'var(--color-text-secondary)' }}>{label}</div>
      <div style={{ flex: 1 }}>{children}</div>
    </Flex>
  )
}
