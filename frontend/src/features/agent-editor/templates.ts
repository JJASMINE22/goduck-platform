/* 内置子代理模板 —— 画布左侧「模块面板」拖出即建节点。
   起步模板源自 agent_confs/default.yaml 的子代理 + 通用模板；字段为 Partial<NodeFields>，
   由 newSubagentNode 合并（缺失字段回退默认空值）。后续可扩展「从已有智能体保存为模板」。 */
import type { NodeFields } from './graphModel'

export interface SubagentTemplate {
  key: string
  label: string
  hint: string
  fields: Partial<NodeFields>
}

export const SUBAGENT_TEMPLATES: SubagentTemplate[] = [
  {
    key: 'blank',
    label: '空白子代理',
    hint: '从零开始配置',
    fields: {},
  },
  {
    key: 'researcher',
    label: '研究助手',
    hint: '信息检索与深度研究',
    fields: {
      name: 'researcher',
      description: '专门负责信息检索与深度研究任务的子代理。',
      system_prompt:
        '你是一个专业的研究助手。根据主题进行系统性信息搜索、筛选、整理与归纳，产出结构化摘要。保持客观，标注来源，区分事实与观点。',
    },
  },
  {
    key: 'analyzer',
    label: '分析专家',
    hint: '数据分析与逻辑推理',
    fields: {
      name: 'analyzer',
      description: '专门负责数据分析与逻辑推理任务的子代理。',
      system_prompt:
        '你是一个数据分析专家。对提供的结构或数据进行深入分析，识别关键模式、趋势与异常，给出有数据支撑的结论与建议。推理严谨。',
    },
  },
  {
    key: 'writer',
    label: '写作助手',
    hint: '文案/稿件撰写',
    fields: {
      name: 'writer',
      description: '专门负责文案、稿件与内容创作的子代理。',
      system_prompt: '你是一个资深写作助手。根据要点撰写高质量、结构清晰的内容，贴合受众与语气要求。',
    },
  },
  {
    key: 'translator',
    label: '翻译助手',
    hint: '多语言互译',
    fields: {
      name: 'translator',
      description: '专门负责多语言翻译的子代理。',
      system_prompt: '你是一个专业翻译。在保持原意与语气的原则下，提供准确、地道的译文。',
    },
  },
]

export function findTemplate(key: string): SubagentTemplate | undefined {
  return SUBAGENT_TEMPLATES.find((t) => t.key === key)
}
