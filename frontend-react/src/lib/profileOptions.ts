/** Shared profile options used by OnboardingPage and SettingsPage */

export const ROLES = [
  { id: 'developer', label: '开发者', desc: '写代码、做产品' },
  { id: 'pm', label: '产品经理', desc: '规划产品、管需求' },
  { id: 'founder', label: '创业者', desc: '创业或独立开发' },
  { id: 'researcher', label: '研究员', desc: '研究 AI/ML 技术' },
  { id: 'investor', label: '投资人', desc: '关注 AI 投资机会' },
  { id: 'creator', label: '内容创作者', desc: '写文章、做视频' },
  { id: 'student', label: '学生', desc: '学习 AI 相关知识' },
  { id: 'other', label: '其他', desc: '' },
]

export const INTERESTS = [
  { id: 'ai-tools', label: 'AI 工具' },
  { id: 'ai-coding', label: 'AI 编程' },
  { id: 'ai-agents', label: 'AI Agent' },
  { id: 'llm-models', label: '大模型动态' },
  { id: 'open-source', label: '开源项目' },
  { id: 'ai-products', label: 'AI 产品' },
  { id: 'ai-research', label: 'AI 研究' },
  { id: 'ai-industry', label: '行业动态' },
  { id: 'ai-investment', label: 'AI 投资' },
  { id: 'prompt-eng', label: 'Prompt 工程' },
  { id: 'ai-creative', label: 'AI 创作' },
  { id: 'ai-infra', label: 'AI 基础设施' },
]

export const TOOLS = [
  { id: 'claude-code', label: 'Claude Code' },
  { id: 'cursor', label: 'Cursor' },
  { id: 'chatgpt', label: 'ChatGPT' },
  { id: 'claude', label: 'Claude' },
  { id: 'copilot', label: 'GitHub Copilot' },
  { id: 'midjourney', label: 'Midjourney' },
  { id: 'stable-diffusion', label: 'Stable Diffusion' },
  { id: 'devin', label: 'Devin' },
  { id: 'windsurf', label: 'Windsurf' },
  { id: 'v0', label: 'v0' },
  { id: 'perplexity', label: 'Perplexity' },
  { id: 'gemini', label: 'Gemini' },
  { id: 'coze', label: 'Coze' },
  { id: 'dify', label: 'Dify' },
  { id: 'comfyui', label: 'ComfyUI' },
  { id: 'notion-ai', label: 'Notion AI' },
]

export function getRoleLabel(id: string | null): string {
  return ROLES.find((r) => r.id === id)?.label || '未设置'
}

export function getInterestLabels(ids: string[]): string[] {
  return ids.map((id) => INTERESTS.find((i) => i.id === id)?.label || id)
}

export function getToolLabels(ids: string[]): string[] {
  return ids.map((id) => TOOLS.find((t) => t.id === id)?.label || id)
}
