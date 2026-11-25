/* 登录页 —— 平台第二道闸的用户入口。
    - 登录：点选锚点（用户名 | 昵称）+ 密码；单命名空间解析，后端不跨域
      回退；错误按后端状态化提示（凭据失败按锚点统一文案防枚举）
    - 注册：弹窗提交（审批制，提交后等待管理员批准方可登录）
    - 欢迎过渡层：登录成功后品牌全屏一瞥（"xxx 欢迎使用 / xxx 管理员，你好"），
      淡出后回来源页；视觉沿用平台玻璃卡片 + 品牌语言
    - 密码输入统一 SecretInput：* 紧凑掩码替代浏览器大间距圆点 */
import { App as AntApp, Button, Flex, Form, Input, Modal, Segmented, Typography } from 'antd'
import { useEffect, useState, type ReactNode } from 'react'
import { useLocation, useNavigate, Navigate } from 'react-router-dom'
import { LogIn, UserPlus } from 'lucide-react'
import { authApi } from '@/api/auth'
import { ApiError } from '@/api/client'
import { SecretInput } from '@/components/form/SecretInput'
import { useCurrentUser } from '@/store/userStore'
import type { LoginMode } from '@/types/auth'

/** 欢迎过渡层：品牌光晕 + 称呼淡入淡出（~1.6s），落幕后导航 */
function WelcomeOverlay({ nickname, isManager, onDone }: { nickname: string; isManager: boolean; onDone: () => void }) {
  const [leaving, setLeaving] = useState(false)
  useEffect(() => {
    const t1 = setTimeout(() => setLeaving(true), 1000)
    const t2 = setTimeout(onDone, 1600)
    return () => {
      clearTimeout(t1)
      clearTimeout(t2)
    }
  }, [onDone])
  return (
    <div
      className="auth-welcome"
      style={{ opacity: leaving ? 0 : 1, transition: 'opacity .55s ease' }}
      aria-hidden="true"
    >
      <div className="auth-welcome-inner">
        <img src="/logo-transparent.png" alt="goduck" style={{ height: 64, objectFit: 'contain' }} />
        <Typography.Title level={3} style={{ margin: 0, color: '#fff' }}>
          {isManager ? `${nickname} 管理员，你好` : `${nickname}，欢迎使用`}
        </Typography.Title>
        <Typography.Text style={{ color: 'rgba(255,255,255,.72)' }}>正在进入平台…</Typography.Text>
      </div>
    </div>
  )
}

type LoginForm = { username: string; password: string }
type RegisterForm = { username: string; nickname: string; password: string; confirm: string }

/** 记住上次登录锚点（localStorage；非法值回落 username） */
function readStoredMode(): LoginMode {
  try {
    return localStorage.getItem('goduck_login_mode') === 'nickname' ? 'nickname' : 'username'
  } catch {
    return 'username'
  }
}

export function LoginPage(): ReactNode {
  const navigate = useNavigate()
  const location = useLocation()
  const setUser = useCurrentUser((s) => s.setUser)
  const status = useCurrentUser((s) => s.status)
  const { message } = AntApp.useApp()
  const [submitting, setSubmitting] = useState(false)
  const [welcome, setWelcome] = useState<{ nickname: string; isManager: boolean } | null>(null)
  const [registerOpen, setRegisterOpen] = useState(false)
  const [registerSubmitting, setRegisterSubmitting] = useState(false)
  const [loginMode, setLoginMode] = useState<LoginMode>(readStoredMode)
  const [loginForm] = Form.useForm<LoginForm>()
  const [registerForm] = Form.useForm<RegisterForm>()

  const from = (location.state as { from?: string } | null)?.from ?? '/agents'

  const switchLoginMode = (mode: LoginMode) => {
    setLoginMode(mode)
    try {
      localStorage.setItem('goduck_login_mode', mode)
    } catch {
      /* 隐私模式等存储不可用：仅不记忆，不影响当次选择 */
    }
  }

  // 已登录态直达 /login：直接回来源页（无欢迎层）
  if (status === 'authenticated') {
    return <Navigate to={from} replace />
  }

  const handleLogin = async () => {
    const v = await loginForm.validateFields()
    setSubmitting(true)
    try {
      const user = await authApi.login({ username: v.username.trim(), password: v.password, mode: loginMode })
      setUser(user)
      setWelcome({ nickname: user.nickname, isManager: user.is_manager })
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : '登录失败，请重试')
    } finally {
      setSubmitting(false)
    }
  }

  const handleRegister = async () => {
    const v = await registerForm.validateFields()
    setRegisterSubmitting(true)
    try {
      const res = await authApi.register({
        username: v.username.trim(),
        nickname: v.nickname.trim(),
        password: v.password,
      })
      message.success(res.message || '注册申请已提交，请等待管理员审批')
      setRegisterOpen(false)
      registerForm.resetFields()
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : '提交失败，请重试')
    } finally {
      setRegisterSubmitting(false)
    }
  }

  return (
    <Flex
      align="center"
      justify="center"
      style={{ minHeight: '100vh', position: 'relative', overflow: 'hidden' }}
    >
      <div className="app-ambient" aria-hidden="true" />
      <div className="app-glass" style={{ width: 380, padding: '36px 32px 28px', borderRadius: 16 }}>
        <Flex vertical align="center" gap={6} style={{ marginBottom: 24 }}>
          <img src="/logo-transparent.png" alt="goduck" style={{ height: 76, objectFit: 'contain' }} />
          <Typography.Text type="secondary" style={{ fontSize: 13 }}>
            智能体编排 · 会话 · 定时任务
          </Typography.Text>
        </Flex>
        <Segmented
          block
          value={loginMode}
          onChange={(v) => switchLoginMode(v as LoginMode)}
          options={[
            { label: '用户名登录', value: 'username' },
            { label: '昵称登录', value: 'nickname' },
          ]}
          style={{ marginBottom: 20 }}
        />
        <Form form={loginForm} layout="vertical" onFinish={handleLogin} requiredMark={false}>
          <Form.Item
            name="username"
            label={loginMode === 'username' ? '用户名' : '昵称'}
            rules={[{ required: true, message: loginMode === 'username' ? '请输入用户名' : '请输入昵称' }]}
          >
            <Input
              placeholder={loginMode === 'username' ? '登录用户名' : '登录昵称'}
              autoComplete="username"
              size="large"
              allowClear
            />
          </Form.Item>
          <Form.Item name="password" label="密码" rules={[{ required: true, message: '请输入密码' }]}>
            <SecretInput placeholder="密码" autoComplete="current-password" size="large" />
          </Form.Item>
          <Button
            type="primary"
            htmlType="submit"
            size="large"
            block
            icon={<LogIn size={16} />}
            loading={submitting}
          >
            登录
          </Button>
        </Form>
        <Button type="link" block icon={<UserPlus size={15} />} onClick={() => setRegisterOpen(true)} style={{ marginTop: 4 }}>
          注册账号
        </Button>
      </div>

      <Modal
        title="注册账号"
        open={registerOpen}
        onCancel={() => setRegisterOpen(false)}
        onOk={handleRegister}
        okText="提交申请"
        cancelText="取消"
        confirmLoading={registerSubmitting}
        okButtonProps={{ icon: <UserPlus size={14} /> }}
      >
        <Typography.Paragraph type="secondary" style={{ fontSize: 13 }}>
          注册需管理员审批通过后方可登录；用户名创建后不可修改。
        </Typography.Paragraph>
        <Form form={registerForm} layout="vertical" requiredMark={false}>
          <Form.Item
            name="username"
            label="用户名"
            rules={[
              { required: true, message: '请输入用户名' },
              { min: 2, max: 32, message: '长度 2-32 个字符' },
            ]}
          >
            <Input placeholder="登录用户名（不可修改）" autoComplete="off" />
          </Form.Item>
          <Form.Item
            name="nickname"
            label="昵称"
            rules={[
              { required: true, message: '请输入昵称' },
              { max: 32, message: '最长 32 个字符' },
            ]}
          >
            <Input placeholder="展示昵称（登录后可修改）" />
          </Form.Item>
          <Form.Item
            name="password"
            label="密码"
            rules={[
              { required: true, message: '请输入密码' },
              { min: 6, message: '密码不得少于 6 位' },
            ]}
          >
            <SecretInput placeholder="至少 6 位" autoComplete="new-password" />
          </Form.Item>
          <Form.Item
            name="confirm"
            label="确认密码"
            dependencies={['password']}
            rules={[
              { required: true, message: '请再次输入密码' },
              ({ getFieldValue }) => ({
                validator: (_, v) =>
                  !v || v === getFieldValue('password')
                    ? Promise.resolve()
                    : Promise.reject(new Error('两次输入的密码不一致')),
              }),
            ]}
          >
            <SecretInput placeholder="再次输入密码" autoComplete="new-password" />
          </Form.Item>
        </Form>
      </Modal>

      {welcome ? (
        <WelcomeOverlay
          nickname={welcome.nickname}
          isManager={welcome.isManager}
          onDone={() => navigate(from, { replace: true })}
        />
      ) : null}
    </Flex>
  )
}
