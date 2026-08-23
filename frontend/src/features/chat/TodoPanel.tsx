/* 任务规划清单面板 —— 订阅 chatStore.todosByAgent，按代理分组渲染实时清单进度。
   数据源：todos SSE 事件（后端 TodoListMiddleware / write_todos，per-node task_planning 开关）。
   形态：吸顶可折叠标签（sticky chip）—— 默认收起为一行胶囊（图标 + 进度 +
   当前执行中任务预览），随消息流滚动常驻顶部，点击展开为浮层清单卡。
   回合作用域：仅当任一代理存在清单时出现；pushUser 新回合 / cancel 手动中止
   即清空（被中断回合的旧计划不悬挂、不跨回合累计），HITL resume 同回合继续
   保留；重进会话不回放（历史接口不含 todos，待该代理下次 write_todos 再现）。 */
import { Flex, Typography } from 'antd'
import { useState, type ReactNode } from 'react'
import { ChevronDown, ChevronUp, Circle, CircleCheck, CircleDot, ListChecks } from 'lucide-react'
import { useChatStore } from '@/store/chatStore'
import type { TodoItem } from '@/types'

/** 与 MessageList.agentMeta 一致的主/子代理标签 */
function agentLabel(path: string): { label: string; color: string } {
  if (path === 'root') return { label: '主代理', color: 'var(--color-role-agent-main)' }
  const seg = path.split('/').pop() ?? path
  return { label: `子代理 · ${seg}`, color: 'var(--color-role-agent-sub)' }
}

function TodoRow({ item }: { item: TodoItem }): ReactNode {
  const done = item.status === 'completed'
  const active = item.status === 'in_progress'
  const color = done ? 'var(--color-success, #52c41a)' : active ? 'var(--color-primary)' : 'var(--color-text-tertiary, #999)'
  const Icon = done ? CircleCheck : active ? CircleDot : Circle
  return (
    <Flex align="flex-start" gap={8} style={{ padding: '3px 0' }}>
      <Icon size={14} style={{ color, marginTop: 2, flexShrink: 0 }} />
      <Typography.Text
        type={done ? 'secondary' : undefined}
        delete={done}
        style={{ fontSize: 13, lineHeight: '20px', wordBreak: 'break-word' }}
      >
        {item.content}
      </Typography.Text>
    </Flex>
  )
}

export function TodoPanel(): ReactNode {
  const todosByAgent = useChatStore((s) => s.todosByAgent)
  const [open, setOpen] = useState(false)
  const entries = Object.entries(todosByAgent).filter(([, todos]) => todos.length > 0)
  if (!entries.length) return null

  // 聚合进度（芯片摘要）：跨代理合计完成数/总数；预览取第一个执行中任务
  const allTodos = entries.flatMap(([, todos]) => todos)
  const doneCount = allTodos.filter((t) => t.status === 'completed').length
  const allDone = doneCount === allTodos.length
  const current = allTodos.find((t) => t.status === 'in_progress')
  const mainColor = agentLabel(entries[0][0]).color
  const name =
    entries.length === 1 ? `${agentLabel(entries[0][0]).label} · 任务清单` : `任务清单 · ${entries.length} 个代理`

  return (
    <div className="todo-panel">
      <button type="button" className="todo-panel__chip" onClick={() => setOpen((v) => !v)}>
        <ListChecks size={14} style={{ color: mainColor, flexShrink: 0 }} />
        <span className="todo-panel__name">{name}</span>
        <span className={`todo-panel__progress${allDone ? ' is-done' : ''}`}>
          {doneCount}/{allTodos.length}
        </span>
        {current ? <span className="todo-panel__current">{current.content}</span> : null}
        {open ? <ChevronUp size={13} style={{ flexShrink: 0 }} /> : <ChevronDown size={13} style={{ flexShrink: 0 }} />}
      </button>
      {open ? (
        <div className="todo-panel__body">
          {entries.map(([agentPath, todos]) => {
            const { label, color } = agentLabel(agentPath)
            const agentDone = todos.filter((t) => t.status === 'completed').length
            return (
              <div key={agentPath} style={{ marginBottom: 6 }}>
                {entries.length > 1 ? (
                  <div className="todo-panel__section-title">
                    <ListChecks size={12} style={{ color }} />
                    {label}
                    <span style={{ color: 'var(--color-text-tertiary)', fontWeight: 400 }}>
                      {agentDone}/{todos.length}
                    </span>
                  </div>
                ) : null}
                {todos.map((t, i) => (
                  <TodoRow key={i} item={t} />
                ))}
              </div>
            )
          })}
        </div>
      ) : null}
    </div>
  )
}
