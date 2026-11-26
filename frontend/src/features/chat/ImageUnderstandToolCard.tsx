/* image_understand 工具专属卡（聊天时间线）—— 原图 + 描述的结构化展示。
    对比通用 ToolCallCard 的平铺 JSON：图像是内容载体而非调试观测，直接
    渲染原图（等比缩放封顶、点击 ZoomStage 放大查看）+ 视觉模型描述
    （markdown 渲染，含表格/列表）；来源 ref（公网 URL / 工作区路径）以
    次级文本行标注，原始 JSON 收进次级折叠保留观测性。
    默认展开（图像即交付物），可整体收起。

    结果三态（判定序 hasResult → parse → result 字段，同 KbSearchToolCard）：
    ① result=ok → 绿「图像已解析」；
    ② parse 成功 ∧ result=error → 橙「解析失败」+ message（URL 拒绝/
       无视觉模型等真实失败）；
    ③ parse 失败（payload=null）→ 红「结果解析失败」，与解析失败严格区分。
    image_url 缺失（对象未落档/签名失败）→ 降级仅描述 + 说明行。 */
import { Check, Image as ImageIcon, Wrench } from 'lucide-react'
import { Collapse, Flex, Tag, Typography } from 'antd'
import { memo, useMemo, useState, type ReactNode } from 'react'
import { Markdown } from '@/components/markdown/Markdown'
import { ZoomStage } from '@/components/markdown/ZoomStage'
import { parseImageUnderstandPayload } from './imageUnderstandTool'
import type { ToolCallEntry } from '@/store/chatStore'
import type { ImageUnderstandToolPayload } from '@/types'

export const ImageUnderstandToolCard = memo(function ImageUnderstandToolCard({
  entry,
}: {
  entry: ToolCallEntry
}): ReactNode {
  const payload = useMemo(
    () => (entry.hasResult ? (parseImageUnderstandPayload(entry.result ?? '') as ImageUnderstandToolPayload | null) : null),
    [entry.hasResult, entry.result],
  )
  const okPayload = payload?.result === 'ok'
  const prompt = typeof entry.args?.prompt === 'string' ? entry.args.prompt : ''
  const imageRef = typeof entry.args?.image_ref === 'string' ? entry.args.image_ref : ''
  const [zoom, setZoom] = useState(false)

  return (
    <Flex justify="flex-start" align="flex-start" gap={10} className="msg-in">
      <div className="chat-avatar" style={{ background: 'var(--color-role-tool)' }}>
        {entry.hasResult ? (
          <Check size={14} />
        ) : (
          <Wrench size={14} className={entry.abandoned ? undefined : 'executing'} />
        )}
      </div>
      <div className="msg-card msg-card--tool">
        <Collapse
          size="small"
          defaultActiveKey={[entry.id]}
          items={[
            {
              key: entry.id,
              label: (
                <Flex align="center" gap={6}>
                  <ImageIcon size={13} style={{ color: 'var(--color-card-tool)' }} />
                  <code
                    className={entry.hasResult ? undefined : 'shimmer'}
                    style={{ fontSize: 12 }}
                  >
                    {entry.name}
                  </code>
                  {prompt ? (
                    <Typography.Text type="secondary" ellipsis style={{ maxWidth: 220, fontSize: 12 }}>
                      「{prompt}」
                    </Typography.Text>
                  ) : null}
                  {entry.hasResult ? (
                    okPayload ? (
                      <Tag color="green" style={{ marginInlineStart: 4 }}>
                        图像已解析
                      </Tag>
                    ) : payload ? (
                      <Tag color="orange" style={{ marginInlineStart: 4 }}>
                        解析失败
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
                      解析中…
                    </Tag>
                  )}
                </Flex>
              ),
              children: (
                <Flex vertical gap={8}>
                  {!entry.hasResult ? (
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      图像解析执行中…
                    </Typography.Text>
                  ) : payload === null ? (
                    <Typography.Text type="danger" style={{ fontSize: 12 }}>
                      结果解析失败：工具返回内容不是约定的图像载荷（可能为工具
                      异常文本或格式漂移），详见下方「原始 JSON」
                    </Typography.Text>
                  ) : !okPayload ? (
                    <Typography.Text type="warning" style={{ fontSize: 12 }}>
                      {payload?.message ?? '图像解析失败'}
                    </Typography.Text>
                  ) : (
                    <>
                      {imageRef ? (
                        <Typography.Text
                          type="secondary"
                          ellipsis
                          style={{ fontSize: 11 }}
                          title={imageRef}
                        >
                          来源：{imageRef}
                        </Typography.Text>
                      ) : null}
                      {payload?.image_url ? (
                        <div className="tc-figure">
                          <img
                            src={payload.image_url}
                            alt={prompt || '分析图像'}
                            onClick={() => setZoom(true)}
                          />
                        </div>
                      ) : (
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                          原图渲染链接不可用（存储失败或签名失败），仅展示文字描述
                        </Typography.Text>
                      )}
                      {payload?.description ? (
                        <div className="tc-figure__desc">
                          <Markdown>{payload.description}</Markdown>
                        </div>
                      ) : null}
                    </>
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
      <ZoomStage open={zoom} onClose={() => setZoom(false)}>
        <img src={payload?.image_url} alt={prompt || '分析图像'} style={{ maxHeight: '78vh' }} />
      </ZoomStage>
    </Flex>
  )
})
