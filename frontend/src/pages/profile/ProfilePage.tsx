/* 个人中心 —— 已登录态的自助资料修改（默认填入当前值）。
    - 用户名只读展示（登录锚点不可改，disabled 输入框视觉传达）
    - 昵称：即时查重 + 保留词黑名单（后端本体），提交即生效
    - 密码：验旧密 + 两次新密确认；成功即吊销全部会话 → 回登录页重新认证 */
import { App as AntApp, Button, Card, Col, Form, Input, Row, Tag, Typography } from 'antd'
import { useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { KeyRound, UserRound } from 'lucide-react'
import { authApi } from '@/api/auth'
import { ApiError } from '@/api/client'
import { PageContainer } from '@/components/layout/PageContainer'
import { useCurrentUser } from '@/store/userStore'

type NicknameForm = { nickname: string }
type PasswordForm = { old_password: string; new_password: string; confirm_password: string }

export function ProfilePage(): ReactNode {
  const navigate = useNavigate()
  const user = useCurrentUser((s) => s.user)
  const setUser = useCurrentUser((s) => s.setUser)
  const { message } = AntApp.useApp()
  const [nicknameForm] = Form.useForm<NicknameForm>()
  const [passwordForm] = Form.useForm<PasswordForm>()
  const [savingNickname, setSavingNickname] = useState(false)
  const [savingPassword, setSavingPassword] = useState(false)

  if (!user) return null

  const handleNickname = async () => {
    const v = await nicknameForm.validateFields()
    setSavingNickname(true)
    try {
      const updated = await authApi.updateNickname(v.nickname.trim())
      setUser(updated)
      message.success('昵称已更新')
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : '修改失败，请重试')
    } finally {
      setSavingNickname(false)
    }
  }

  const handlePassword = async () => {
    const v = await passwordForm.validateFields()
    setSavingPassword(true)
    try {
      const res = await authApi.changePassword(v)
      message.success(res.message || '密码已修改，请重新登录')
      // 改密吊销全部会话（含当前）：清身份回登录页重新认证
      useCurrentUser.getState().clearUser()
      navigate('/login', { replace: true })
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : '修改失败，请重试')
    } finally {
      setSavingPassword(false)
    }
  }

  return (
    <PageContainer
      title="个人中心"
      subtitle="用户名不可修改；昵称即时生效；修改密码后需重新登录"
    >
      <Row gutter={[12, 12]} style={{ height: '100%', alignContent: 'start' }}>
        <Col xs={24} md={12}>
          <Card size="small" title={<span><UserRound size={14} style={{ marginRight: 6, verticalAlign: -2 }} />基本信息</span>}>
            <Form layout="vertical" requiredMark={false}>
              <Form.Item label="用户名（登录用，不可修改）" extra="身份与昵称解耦：改名不影响任何会话、任务与记忆归属">
                <Input value={user.username} disabled />
              </Form.Item>
              <Form.Item label="角色">
                {user.is_manager ? <Tag color="geekblue">管理员</Tag> : <Tag>普通用户</Tag>}
              </Form.Item>
            </Form>
            <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 12 }}>
              修改昵称（即时查重，无需审批）
            </Typography.Paragraph>
            <Form
              form={nicknameForm}
              layout="vertical"
              requiredMark={false}
              initialValues={{ nickname: user.nickname }}
              onFinish={handleNickname}
            >
              <Form.Item
                name="nickname"
                rules={[
                  { required: true, message: '请输入昵称' },
                  { max: 32, message: '最长 32 个字符' },
                ]}
              >
                <Input placeholder="展示昵称" allowClear />
              </Form.Item>
              <Button type="primary" htmlType="submit" loading={savingNickname}>
                保存昵称
              </Button>
            </Form>
          </Card>
        </Col>
        <Col xs={24} md={12}>
          <Card size="small" title={<span><KeyRound size={14} style={{ marginRight: 6, verticalAlign: -2 }} />修改密码</span>}>
            <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
              需验证当前密码；修改成功后所有登录将失效，请重新登录。
            </Typography.Paragraph>
            <Form form={passwordForm} layout="vertical" requiredMark={false} onFinish={handlePassword}>
              <Form.Item
                name="old_password"
                label="当前密码"
                rules={[{ required: true, message: '请输入当前密码' }]}
              >
                <Input.Password placeholder="当前密码" autoComplete="current-password" />
              </Form.Item>
              <Form.Item
                name="new_password"
                label="新密码"
                rules={[
                  { required: true, message: '请输入新密码' },
                  { min: 6, message: '密码不得少于 6 位' },
                ]}
              >
                <Input.Password placeholder="至少 6 位" autoComplete="new-password" />
              </Form.Item>
              <Form.Item
                name="confirm_password"
                label="确认新密码"
                dependencies={['new_password']}
                rules={[
                  { required: true, message: '请再次输入新密码' },
                  ({ getFieldValue }) => ({
                    validator: (_, v) =>
                      !v || v === getFieldValue('new_password')
                        ? Promise.resolve()
                        : Promise.reject(new Error('两次输入的新密码不一致')),
                  }),
                ]}
              >
                <Input.Password placeholder="再次输入新密码" autoComplete="new-password" />
              </Form.Item>
              <Button type="primary" htmlType="submit" danger loading={savingPassword}>
                确认修改密码
              </Button>
            </Form>
          </Card>
        </Col>
      </Row>
    </PageContainer>
  )
}
