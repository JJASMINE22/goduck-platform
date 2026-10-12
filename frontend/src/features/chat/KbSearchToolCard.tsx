/* kb_search 工具专属卡（聊天时间线）—— 检索结果的结构化摘要 + 证据锚定入口。
   解决两个观测问题（对比通用 ToolCallCard 的平铺 JSON）：
   ① 平铺难观测：渲染为「检索 query · N 命中」头 + 逐命中紧凑行（页码/文件/
      相似度 + 两行摘要），原始 JSON 收进次级折叠（保留调试观测性）；
   ② 图像预览位置：命中行内联资产缩略图（hover 图注），「原文定位」打开
      共享 KbEvidenceViewer（预签名原始 PDF + SVG 叠框）。

   数据流：工具结果（ToolMessage.content 持久化，实时 SSE 与历史回放同形）→
   解析 hits → 持 (kb_id, doc_id, chunk_index) 键组懒取 anchor-cards（backend
   持有 MinIO，懒签规避持久化结果中 URL 过期）→ 行内缩略图 + 定位按钮。
   legacy / docx 直解命中锚定卡为 null → 降级纯文本行（B2 可空契约）。

   结果三态（判定序 hasResult → parse → result 字段）：
   ① result=ok → 绿「N 命中」（hits 可为空，正文显示未检索到）；
   ② parse 成功 ∧ result=error → 橙「检索失败」+ message（ES 不可达等真实故障）；
   ③ parse 失败（payload=null，工具异常文本/格式漂移）→ 红「结果解析失败」，
     与检索失败严格区分——曾因后端块列表 str() 序列化为 repr 而整体误报②。
   jevKb==='unavailable'（门控观测标记）仅在①叠加灰「jev 未介入」：判决
   不可用结果照常透传，检索与预览不受影响。 */
import { BookOpen, Check, Crosshair, FileText, Wrench } from 'lucide-react'
import { Button, Collapse, Flex, Tag, Tooltip, Typography } from 'antd'
import { memo, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { knowledgeApi } from '@/api/knowledge'
import { toastError } from '@/lib/antdStatic'
import { sanitizeKbText } from '@/lib/format'
import { KbEvidenceViewer, type EvidenceAnchor } from '@/features/knowledge/KbEvidenceViewer'
import { parseKbSearchPayload } from './kbSearchTool'
import type { ToolCallEntry } from '@/store/chatStore'
import type { KbAnchorCard, KbSearchToolPayload, UUID } from '@/types'

/** 解包为强类型载荷（null = 未返回/流式半截） */
function parsePayload(raw: string): KbSearchToolPayload | null {
  return parseKbSearchPayload(raw) as KbSearchToolPayload | null
}

function scorePct(score: number): string {
  return `${Math.max(0, Math.min(100, Math.round(score * 100)))}%`
}

const cardKey = (docId: UUID | null | undefined, chunkIndex: number | null | undefined): string =>
  `${docId ?? ''}:${chunkIndex ?? ''}`

export const KbSearchToolCard = memo(function KbSearchToolCard({
  entry,
}: {
  entry: ToolCallEntry
}): ReactNode {
  const payload = useMemo(
    () => (entry.hasResult ? parsePayload(entry.result ?? '') : null),
    [entry.hasResult, entry.result],
  )
  const hits = payload?.hits ?? []
  const okPayload = payload?.result === 'ok'

  // 懒取锚定卡：结果落地后一次性批量取（≤50；error/空命中不取）
  const [cards, setCards] = useState<Record<string, KbAnchorCard | null>>({})
  const fetchedRef = useRef(false)
  useEffect(() => {
    if (!entry.hasResult || !okPayload || fetchedRef.current) return
    const items = hits
      .filter((h) => h.kb_id && h.doc_id && h.chunk_index != null)
      .map((h) => ({ kb_id: h.kb_id!, doc_id: h.doc_id!, chunk_index: h.chunk_index! }))
    if (!items.length) return
    let cancelled = false
    knowledgeApi
      .anchorCards(items)
      .then((res) => {
        if (cancelled) return
        fetchedRef.current = true
        const map: Record<string, KbAnchorCard | null> = {}
        hits.forEach((h, i) => {
          map[cardKey(h.doc_id, h.chunk_index)] = res[i] ?? null
        })
        setCards(map)
      })
      .catch(() => {
        /* 锚定是增强：取失败 → 全体纯文本行，不影响观测；fetchedRef 未置位，
           deps 变化（重发回填）时自然重试 */
      })
    return () => {
      cancelled = true
      // 清洗即解除完成标记：StrictMode 开发期"挂载→清洗→重挂"序列中，
      // 首轮请求被清洗丢弃后第二轮 effect 必须能重取——否则 cards 永远
      // 为空，定位按钮/缩略图/heading 永不渲染（曾致锚定预览整体失效）
      fetchedRef.current = false
    }
    // entry.result 定格后 hits 稳定（键组不变），一次即可
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entry.hasResult, entry.result])

  // 证据 viewer：点击定位时聚合同文档命中锚点
  const [viewer, setViewer] = useState<{
    docUrl: string
    docName: string
    anchors: EvidenceAnchor[]
    initialId?: string
  } | null>(null)

  const locate = (docId: UUID, chunkIndex: number | null) => {
    const mine = cards[cardKey(docId, chunkIndex)]
    if (!mine?.doc_url) return
    const same = hits
      .map((h) => cards[cardKey(h.doc_id, h.chunk_index)])
      .filter((c): c is KbAnchorCard => !!c && !!c.doc_uri && c.doc_uri === mine.doc_uri)
    const anchors: EvidenceAnchor[] = []
    let initialId: string | undefined
    for (const [ci, c] of same.entries()) {
      if (c.page && c.bbox) {
        const id = `${c.doc_uri}:${c.page}:${ci}:main`
        anchors.push({
          id,
          page: c.page,
          bbox: c.bbox,
          title: sanitizeKbText(c.content).slice(0, 60) || '命中片段',
          meta: c.kind === 'table' ? '表格单元' : undefined,
        })
        if (c === mine) initialId = id
      }
      for (const [ai, a] of (c.assets ?? []).entries()) {
        if (a.page && a.bbox) {
          // id 含 ci：不同命中的资产即使同页同序也唯一（曾裸用 ai 跨卡片撞 id）
          const id = `${c.doc_uri}:${a.page}:${ci}:a${ai}`
          anchors.push({
            id,
            page: a.page,
            bbox: a.bbox,
            title: a.caption || '图片资产',
            meta: '图',
          })
          // 本体无 bbox/页码（主锚点缺失）时回落到该条自己的首个资产锚点——
          // 点了哪条就跳哪条，绝不回退到第一条命中的位置
          if (c === mine && initialId === undefined) initialId = id
        }
      }
    }
    if (!anchors.length) return
    if (!initialId) {
      // 被点命中自身零锚框（有 bbox 无页码等残缺）：打开只会落在别条位置，明确拒绝
      toastError('该命中自身无可定位锚框（缺页码或坐标）')
      return
    }
    setViewer({
      docUrl: mine.doc_url,
      docName: mine.filename || entry.name,
      anchors,
      initialId,
    })
  }

  return (
    <Flex justify="flex-start" align="flex-start" gap={10} className="msg-in">
      <div
        className="chat-avatar"
        style={{ background: 'var(--color-role-tool)', width: 28, height: 28 }}
      >
        {entry.hasResult ? (
          <Check size={14} />
        ) : (
          <Wrench size={14} className={entry.abandoned ? undefined : 'executing'} />
        )}
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <Collapse
          size="small"
          defaultActiveKey={[]}
          items={[
            {
              key: entry.id,
              label: (
                <Flex align="center" gap={6}>
                  <BookOpen size={13} style={{ color: 'var(--color-text-tertiary)' }} />
                  <code
                    className={entry.hasResult ? undefined : 'shimmer'}
                    style={{ fontSize: 12 }}
                  >
                    {entry.name}
                  </code>
                  {payload?.query ? (
                    <Typography.Text type="secondary" ellipsis style={{ maxWidth: 220, fontSize: 12 }}>
                      「{payload.query}」
                    </Typography.Text>
                  ) : null}
                  {entry.hasResult ? (
                    okPayload ? (
                      <>
                        <Tag color="green" style={{ marginInlineStart: 4 }}>
                          {payload?.total ?? hits.length} 命中
                        </Tag>
                        {entry.jevKb === 'unavailable' ? (
                          <Tooltip title="相关性判决服务不可用，本次结果未过滤（原样透传），检索本身不受影响">
                            <Tag style={{ marginInlineStart: 0 }}>jev 未介入</Tag>
                          </Tooltip>
                        ) : null}
                      </>
                    ) : payload ? (
                      <Tag color="orange" style={{ marginInlineStart: 4 }}>
                        检索失败
                      </Tag>
                    ) : (
                      <Tag color="red" style={{ marginInlineStart: 4 }}>
                        结果解析失败
                      </Tag>
                    )
                  ) : entry.abandoned ? (
                    <Tag style={{ marginInlineStart: 4 }}>已中断</Tag>
                  ) : (
                    <Tag color="processing" style={{ marginInlineStart: 4 }}>
                      检索中…
                    </Tag>
                  )}
                </Flex>
              ),
              children: (
                <Flex vertical gap={8}>
                  {!entry.hasResult ? (
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      检索执行中…
                    </Typography.Text>
                  ) : payload === null ? (
                    // 三态③：载荷不可解析（工具异常文本/格式漂移）≠ 检索失败
                    <Typography.Text type="danger" style={{ fontSize: 12 }}>
                      结果解析失败：工具返回内容不是约定的检索载荷（可能为工具
                      异常文本或格式漂移），详见下方「原始 JSON」
                    </Typography.Text>
                  ) : !okPayload ? (
                    <Typography.Text type="warning" style={{ fontSize: 12 }}>
                      {payload?.message ?? '检索失败'}
                    </Typography.Text>
                  ) : hits.length === 0 ? (
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      未检索到相关内容
                    </Typography.Text>
                  ) : (
                    hits.map((h, i) => {
                      const card = cards[cardKey(h.doc_id, h.chunk_index)]
                      const thumbs = (card?.assets ?? []).filter((a) => a.thumb_b64).slice(0, 3)
                      const locatable =
                        !!card?.doc_url && (!!card?.bbox || !!card?.assets?.some((a) => a.bbox && a.page))
                      return (
                        <div
                          key={`${h.doc_id}-${h.chunk_index ?? i}`}
                          onClick={locatable ? () => locate(h.doc_id!, h.chunk_index ?? null) : undefined}
                          style={{
                            border: '1px solid var(--color-border)',
                            borderRadius: 6,
                            padding: '6px 8px',
                            background: 'var(--color-bg-hover, rgba(0,0,0,0.02))',
                            cursor: locatable ? 'pointer' : undefined,
                          }}
                        >
                          <Flex justify="space-between" align="center" gap={6}>
                            <Flex gap={6} align="center" wrap>
                              {h.page ? <Tag style={{ marginInlineStart: 0 }}>P{h.page}</Tag> : null}
                              <FileText size={12} style={{ color: 'var(--color-text-tertiary)' }} />
                              <Typography.Text ellipsis style={{ maxWidth: 200, fontSize: 12, fontWeight: 500 }}>
                                {h.filename}
                              </Typography.Text>
                              {card?.kind === 'table' ? <Tag color="purple">表格</Tag> : null}
                            </Flex>
                            <Flex gap={4} align="center">
                              {locatable ? (
                                <Tooltip title="原始 PDF 定位（叠框高亮）">
                                  <Button
                                    size="small"
                                    icon={<Crosshair size={12} />}
                                    onClick={(e) => {
                                      // 行级 onClick 已定位；阻止冒泡避免连续触发两次
                                      e.stopPropagation()
                                      locate(h.doc_id!, h.chunk_index ?? null)
                                    }}
                                  />
                                </Tooltip>
                              ) : null}
                              <Tag color="blue">{scorePct(h.score)}</Tag>
                            </Flex>
                          </Flex>
                          {card?.heading_path?.length ? (
                            <Typography.Text
                              type="secondary"
                              ellipsis
                              style={{ display: 'block', marginTop: 2, fontSize: 11 }}
                              title={card.heading_path.join(' / ')}
                            >
                              {card.heading_path.join(' / ')}
                            </Typography.Text>
                          ) : null}
                          <Typography.Paragraph
                            ellipsis={{ rows: 2 }}
                            style={{ margin: '4px 0 0', fontSize: 12, color: 'var(--color-text-secondary)' }}
                          >
                            {sanitizeKbText(card?.content ?? h.content)}
                          </Typography.Paragraph>
                          {thumbs.length ? (
                            <Flex gap={6} style={{ marginTop: 6 }}>
                              {thumbs.map((a, ai) => (
                                <Tooltip key={ai} title={a.caption || '图片资产'}>
                                  <img
                                    src={`data:image/jpeg;base64,${a.thumb_b64}`}
                                    alt={a.caption || '资产缩略图'}
                                    style={{
                                      height: 48,
                                      borderRadius: 4,
                                      border: '1px solid var(--color-border)',
                                    }}
                                  />
                                </Tooltip>
                              ))}
                            </Flex>
                          ) : null}
                        </div>
                      )
                    })
                  )}
                  {/* 原始 JSON 次级折叠：保留调试观测性，不喧宾 */}
                  {entry.hasResult ? (
                    <Collapse
                      size="small"
                      ghost
                      items={[
                        {
                          key: 'raw',
                          label: (
                            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                              原始 JSON
                            </Typography.Text>
                          ),
                          children: (
                            <pre
                              className="tc-payload__pre"
                              style={{ margin: 0, maxHeight: 200, overflow: 'auto', fontSize: 11 }}
                            >
                              <code className="tc-json">{entry.result}</code>
                            </pre>
                          ),
                        },
                      ]}
                    />
                  ) : null}
                </Flex>
              ),
            },
          ]}
        />
      </div>
      {/* 证据锚定 viewer：keyed 挂载（每次定位新实例，卸载销毁文档句柄） */}
      {viewer ? (
        <KbEvidenceViewer
          key={viewer.docUrl}
          onClose={() => setViewer(null)}
          docUrl={viewer.docUrl}
          docName={viewer.docName}
          anchors={viewer.anchors}
          initialAnchorId={viewer.initialId}
        />
      ) : null}
    </Flex>
  )
})
