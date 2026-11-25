/* 系统管理页（管理员）—— 注册申请审批 + 用户管理。
    - 注册申请：平铺列表（状态过滤），批准即建户可登录，驳回需填原因（可选）；
      rejected 行保留 = 用户名占位防骚扰；管理员可删除被拒记录释放用户名
    - 用户管理：停用/启用（即时吊销会话）、解锁（爆破锁）、重置密码
      （二次输入；原密码哈希不可见） */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { App as AntApp, Button, Flex, Form, Input, Modal, Popconfirm, Segmented, Table, Tag, Tooltip, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useState, type ReactNode } from 'react'
import { Check, KeyRound, LockOpen, ShieldOff, Trash2, X } from 'lucide-react'
import { authApi } from '@/api/auth'
import { PageContainer } from '@/components/layout/PageContainer'
import { EmptyState, ErrorState, PageLoading } from '@/components/feedback/states'
import { SecretInput } from '@/components/form/SecretInput'
import { formatRelative } from '@/lib/format'
import { toastSuccess } from '@/lib/antdStatic'
import type { AdminUser, AuthRequestOut } from '@/types/auth'

const AUTH_QK = ['auth', 'requests'] as const
const USERS_QK = ['auth', 'users'] as const

const REQUEST_STATUS_TAG: Record<string, { color: string; text: string }> = {
  pending: { color: 'gold', text: '待审批' },
  approved: { color: 'green', text: '已批准' },
  rejected: { color: 'red', text: '已拒绝' },
}

function RequestsPanel(): ReactNode {
  const qc = useQueryClient()
  const [statusFilter, setStatusFilter] = useState<string>('pending')
  const [rejectTarget, setRejectTarget] = useState<AuthRequestOut | null>(null)
  const [rejectForm] = Form.useForm<{ reason: string }>()

  const reqs = useQuery({
    queryKey: [...AUTH_QK, statusFilter],
    queryFn: () => authApi.listRequests(statusFilter === 'all' ? undefined : statusFilter),
  })
  const invalidate = () => qc.invalidateQueries({ queryKey: AUTH_QK })

  const approve = useMutation({
    mutationFn: (id: string) => authApi.approveRequest(id),
    onSuccess: () => {
      toastSuccess('已批准，该用户即可登录')
      void invalidate()
    },
  })

  const reject = useMutation({
    mutationFn: ({ id, reason }: { id: string; reason?: string }) =>
      authApi.rejectRequest(id, reason),
    onSuccess: () => {
      toastSuccess('已拒绝该申请')
      setRejectTarget(null)
      rejectForm.resetFields()
      void invalidate()
    },
  })

  const remove = useMutation({
    mutationFn: (id: string) => authApi.deleteRequest(id),
    onSuccess: () => {
      toastSuccess('记录已删除，用户名已释放')
      void invalidate()
    },
  })

  // 驳回原因仅在有被拒记录的视图出现（待审批/已批准下无信息量）
  const reasonCol: ColumnsType<AuthRequestOut>[number] = {
    title: '驳回原因',
    dataIndex: 'reason',
    key: 'reason',
    align: 'left',
    ellipsis: true,
    render: (v: string | null) =>
      v ?? <span style={{ color: 'var(--color-text-tertiary)' }}>—</span>,
  }
  const showReason = statusFilter === 'rejected' || statusFilter === 'all'

  const columns: ColumnsType<AuthRequestOut> = [
    { title: '用户名', dataIndex: 'username', key: 'username', width: 140 },
    { title: '昵称', dataIndex: 'nickname', key: 'nickname', width: 120 },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 90,
      render: (v: string) => {
        const t = REQUEST_STATUS_TAG[v] ?? { color: 'default', text: v }
        return <Tag color={t.color}>{t.text}</Tag>
      },
    },
    {
      title: '申请时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 140,
      render: formatRelative,
    },
    ...(showReason ? [reasonCol] : []),
    {
      title: '操作',
      key: 'actions',
      width: 150,
      render: (_, r) =>
        r.status === 'pending' ? (
          <Flex gap={4} justify="center">
            <Popconfirm
              title="批准该注册申请？"
              onConfirm={() => approve.mutate(r.id)}
              okText="批准"
              cancelText="取消"
            >
              <Button type="link" size="small" icon={<Check size={14} />}>
                批准
              </Button>
            </Popconfirm>
            <Button
              type="link"
              size="small"
              danger
              icon={<X size={14} />}
              onClick={() => setRejectTarget(r)}
            >
              拒绝
            </Button>
          </Flex>
        ) : r.status === 'rejected' ? (
          <Popconfirm
            title="删除该被拒记录？"
            description={`删除后用户名「${r.username}」可重新注册，拒绝历史将一并清除`}
            onConfirm={() => remove.mutate(r.id)}
            okText="删除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
          >
            <Button type="link" size="small" danger icon={<Trash2 size={14} />}>
              删除记录
            </Button>
          </Popconfirm>
        ) : (
          <span style={{ color: 'var(--color-text-tertiary)', fontSize: 12 }}>已处理</span>
        ),
    },
  ]

  return (
    <>
      <Flex justify="space-between" align="center" style={{ marginBottom: 12 }}>
        <Segmented
          value={statusFilter}
          onChange={(v) => setStatusFilter(v as string)}
          options={[
            { label: '待审批', value: 'pending' },
            { label: '已批准', value: 'approved' },
            { label: '已拒绝', value: 'rejected' },
            { label: '全部', value: 'all' },
          ]}
        />
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          被拒记录保留用户名占位防骚扰；删除记录后该用户名即可重新注册
        </Typography.Text>
      </Flex>
      {reqs.isLoading ? (
        <PageLoading />
      ) : reqs.isError ? (
        <ErrorState onRetry={() => reqs.refetch()} />
      ) : (
        <Table
          rowKey="id"
          size="small"
          columns={columns}
          dataSource={reqs.data}
          pagination={false}
          locale={{
            emptyText: <EmptyState title="暂无申请" description="新用户在登录页提交注册后将出现在这里" />,
          }}
        />
      )}
      <Modal
        title={`拒绝「${rejectTarget?.username ?? ''}」的注册申请`}
        open={!!rejectTarget}
        onCancel={() => setRejectTarget(null)}
        onOk={async () => {
          const v = await rejectForm.validateFields()
          reject.mutate({ id: rejectTarget!.id, reason: v.reason?.trim() || undefined })
        }}
        okText="确认拒绝"
        okButtonProps={{ danger: true }}
        confirmLoading={reject.isPending}
        destroyOnHidden
      >
        <Form form={rejectForm} layout="vertical">
          <Form.Item name="reason" label="驳回原因（可选，申请人不可见）">
            <Input.TextArea rows={2} maxLength={200} placeholder="如：信息不全 / 身份待确认" />
          </Form.Item>
        </Form>
      </Modal>
    </>
  )
}

function UsersPanel(): ReactNode {
  const qc = useQueryClient()
  const { modal } = AntApp.useApp()
  const [resetTarget, setResetTarget] = useState<AdminUser | null>(null)
  const [resetForm] = Form.useForm<{ password: string; confirm: string }>()

  const users = useQuery({ queryKey: USERS_QK, queryFn: () => authApi.listUsers() })

  const setState = useMutation({
    mutationFn: ({ id, state }: { id: string; state: 'active' | 'disabled' }) =>
      authApi.setUserState(id, state),
    onSuccess: (_d, v) => {
      toastSuccess(v.state === 'disabled' ? '已停用（会话已吊销）' : '已启用')
      void qc.invalidateQueries({ queryKey: USERS_QK })
    },
  })

  const unlock = useMutation({
    mutationFn: (id: string) => authApi.unlockUser(id),
    onSuccess: () => {
      toastSuccess('已解锁')
      void qc.invalidateQueries({ queryKey: USERS_QK })
    },
  })

  const reset = useMutation({
    mutationFn: ({ id, password }: { id: string; password: string }) =>
      authApi.resetPassword(id, password),
    onSuccess: () => {
      toastSuccess('密码已重置（该用户全部会话已吊销）')
      setResetTarget(null)
      resetForm.resetFields()
      void qc.invalidateQueries({ queryKey: USERS_QK })
    },
  })

  const columns: ColumnsType<AdminUser> = [
    { title: '用户名', dataIndex: 'username', key: 'username', width: 130 },
    { title: '昵称', dataIndex: 'nickname', key: 'nickname', width: 110 },
    {
      title: '角色',
      dataIndex: 'is_manager',
      key: 'is_manager',
      width: 90,
      render: (v: boolean) => (v ? <Tag color="geekblue">管理员</Tag> : <Tag>用户</Tag>),
    },
    {
      title: '状态',
      dataIndex: 'state',
      key: 'state',
      width: 90,
      render: (v: string, r) =>
        v === 'active' ? (
          r.locked_until && new Date(r.locked_until) > new Date() ? (
            <Tag color="orange">锁定中</Tag>
          ) : (
            <Tag color="green">正常</Tag>
          )
        ) : (
          <Tag color="red">已停用</Tag>
        ),
    },
    {
      title: '最近登录',
      dataIndex: 'last_login_at',
      key: 'last_login_at',
      width: 140,
      render: (v: string | null) =>
        v ? formatRelative(v) : <span style={{ color: 'var(--color-text-tertiary)' }}>从未</span>,
    },
    {
      title: '操作',
      key: 'actions',
      width: 230,
      render: (_, r) => (
        <Flex gap={4} justify="center">
          {r.state === 'active' ? (
            <Button
              type="link"
              size="small"
              danger
              icon={<ShieldOff size={14} />}
              onClick={() =>
                modal.confirm({
                  title: `停用「${r.nickname}」？`,
                  content: '停用后该用户无法登录，全部会话立即吊销；可随时重新启用。',
                  okType: 'danger',
                  okText: '停用',
                  cancelText: '取消',
                  onOk: () => setState.mutate({ id: r.id, state: 'disabled' }),
                })
              }
            >
              停用
            </Button>
          ) : (
            <Button
              type="link"
              size="small"
              onClick={() => setState.mutate({ id: r.id, state: 'active' })}
            >
              启用
            </Button>
          )}
          {r.locked_until && new Date(r.locked_until) > new Date() ? (
            <Tooltip title="解除登录失败锁定">
              <Button type="link" size="small" icon={<LockOpen size={14} />} onClick={() => unlock.mutate(r.id)}>
                解锁
              </Button>
            </Tooltip>
          ) : null}
          <Button
            type="link"
            size="small"
            icon={<KeyRound size={14} />}
            onClick={() => setResetTarget(r)}
          >
            重置密码
          </Button>
        </Flex>
      ),
    },
  ]

  return (
    <>
      {users.isLoading ? (
        <PageLoading />
      ) : users.isError ? (
        <ErrorState onRetry={() => users.refetch()} />
      ) : (
        <Table rowKey="id" size="small" columns={columns} dataSource={users.data} pagination={false} />
      )}
      <Modal
        title={`重置「${resetTarget?.nickname ?? ''}」的密码`}
        open={!!resetTarget}
        onCancel={() => setResetTarget(null)}
        onOk={async () => {
          // 表单校验失败（validateFields reject）为 Error 对象静默；接口错误由全局提示
          const v = await resetForm.validateFields().catch(() => null)
          if (v) reset.mutate({ id: resetTarget!.id, password: v.password })
        }}
        okText="确认重置"
        confirmLoading={reset.isPending}
        destroyOnHidden
      >
        <Typography.Paragraph type="secondary" style={{ fontSize: 13 }}>
          原密码不可见（仅存哈希）；重置后请将新密码线下告知该用户，其全部会话将立即吊销。
        </Typography.Paragraph>
        <Form form={resetForm} layout="vertical" requiredMark={false}>
          <Form.Item
            name="password"
            label="新密码"
            rules={[
              { required: true, message: '请输入新密码' },
              { min: 6, message: '密码不得少于 6 位' },
            ]}
          >
            <SecretInput placeholder="至少 6 位" autoComplete="new-password" />
          </Form.Item>
          <Form.Item
            name="confirm"
            label="确认新密码"
            dependencies={['password']}
            rules={[
              { required: true, message: '请再次输入新密码' },
              ({ getFieldValue }) => ({
                validator: (_, v) =>
                  !v || v === getFieldValue('password')
                    ? Promise.resolve()
                    : Promise.reject(new Error('两次输入的密码不一致')),
              }),
            ]}
          >
            <SecretInput placeholder="再次输入新密码" autoComplete="new-password" />
          </Form.Item>
        </Form>
      </Modal>
    </>
  )
}

export function AdminPage(): ReactNode {
  const [tab, setTab] = useState<'requests' | 'users'>('requests')
  return (
    <PageContainer title="系统管理" subtitle="注册审批与用户账号管理（仅管理员可见）">
      <div className="page-body" style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        <Segmented
          value={tab}
          onChange={(v) => setTab(v as 'requests' | 'users')}
          options={[
            { label: '注册申请', value: 'requests' },
            { label: '用户管理', value: 'users' },
          ]}
        />
        {tab === 'requests' ? <RequestsPanel /> : <UsersPanel />}
      </div>
    </PageContainer>
  )
}
