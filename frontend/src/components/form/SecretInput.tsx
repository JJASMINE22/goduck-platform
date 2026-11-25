/* SecretInput —— 密文输入（API Key / 密码）。
   浏览器 type=password 的掩码是间距过大且不可控的圆点，此处自绘掩码层：
   真实值放在文字透明的 antd Input 中（光标 / 选区 / 粘贴 / 溢出滚动全原生），
   其上叠一绝对定位等宽字体层逐字符画 *（间距紧凑、与光标逐字对齐）；
   掩码层 pointer-events 穿透、随 input.scrollLeft 同步位移（长 Key 滚动不脱节）。
   眼睛钮切换明文。Form.Item 兼容：受控 value / onChange（直传 string，
   同 InputNumber 惯例）。 */
import { Input, theme } from 'antd'
import type { InputProps, InputRef } from 'antd'
import { Eye, EyeOff } from 'lucide-react'
import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react'

export interface SecretInputProps
  extends Omit<InputProps, 'type' | 'value' | 'onChange' | 'prefix' | 'suffix' | 'allowClear'> {
  value?: string | null
  onChange?: (value: string) => void
}

/** 掩码避让右侧眼睛钮的留白（眼睛 14 + 前后 margin + 内边距） */
const EYE_ZONE = 32

export function SecretInput(props: SecretInputProps): ReactNode {
  const { value, onChange, size, disabled, autoComplete, placeholder, style, className } = props
  const [visible, setVisible] = useState(false)
  const [scrollLeft, setScrollLeft] = useState(0)
  const inputRef = useRef<InputRef>(null)
  const { token } = theme.useToken()

  // 掩码层文字起点 = 边框(1px) + antd 输入横向内边距，恰等于 antd 控件横向 token
  const textInset = size === 'small' ? token.controlPaddingHorizontalSM : token.controlPaddingHorizontal
  const fontSize = size === 'large' ? token.fontSizeLG : token.fontSize

  // input 自身滚动（长值溢出后光标跟随滚动、方向键平移）→ 掩码层同步位移
  useEffect(() => {
    const el = inputRef.current?.input
    if (!el) return
    const sync = () => setScrollLeft(el.scrollLeft)
    el.addEventListener('scroll', sync)
    return () => el.removeEventListener('scroll', sync)
  }, [])
  // 值变更（键入 / 回填 / 重置）后浏览器可能自动滚动到光标：读取一次实际位置
  useEffect(() => {
    const el = inputRef.current?.input
    if (el) setScrollLeft(el.scrollLeft)
  }, [value])

  const val = value ?? ''

  const maskLayer: CSSProperties = {
    position: 'absolute',
    // antd 输入框 hover/聚焦时 affix wrapper 会提到 z-index:1，掩码层必须
    // 恒压其上，否则交互瞬间（正在输入时）* 会整层消失
    zIndex: 2,
    top: 0,
    bottom: 0,
    left: textInset,
    right: EYE_ZONE,
    display: 'flex',
    alignItems: 'center',
    overflow: 'hidden',
    pointerEvents: 'none',
    whiteSpace: 'pre',
    fontFamily: 'var(--font-mono)',
    fontSize,
    color: disabled ? 'var(--color-text-tertiary)' : 'var(--color-text)',
    opacity: disabled ? 0.55 : 1,
  }

  return (
    <span className={className} style={{ ...style, position: 'relative', display: 'block' }}>
      <Input
        ref={inputRef}
        value={val}
        onChange={(e) => onChange?.(e.target.value)}
        size={size}
        disabled={disabled}
        autoComplete={autoComplete}
        placeholder={placeholder}
        spellCheck={false}
        styles={{
          input: {
            fontFamily: 'var(--font-mono)',
            color: visible ? undefined : 'transparent',
            caretColor: 'var(--color-text)',
          },
        }}
        suffix={
          <span
            className="secret-input__eye"
            title={visible ? '隐藏' : '显示'}
            onClick={() => setVisible((v) => !v)}
            onMouseDown={(e) => e.preventDefault()}
          >
            {visible ? <EyeOff size={14} /> : <Eye size={14} />}
          </span>
        }
      />
      {visible ? null : (
        <span aria-hidden="true" style={maskLayer}>
          <span style={{ display: 'inline-block', transform: `translateX(${-scrollLeft}px)` }}>
            {'*'.repeat(val.length)}
          </span>
        </span>
      )}
    </span>
  )
}
