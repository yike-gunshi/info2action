/** 平台显示顺序与白名单。
 *  抽离到独立文件以避免组件文件混合导出常量违反 React Fast Refresh 规则
 *  （否则 HMR 会 invalidate 整个模块树，导致 store 重置 + LoadingSkeleton 卡死）。
 */
// 用户决策（2026-05-12）：频道页/推荐页 section 顺序统一
// 前 7 项 = 用户给定优先序：X 公众号 AGI之路 GitHub Reddit RSS HN
// 后 2 项 = 用户未列出的（B 站 / Manual）按当前活跃度拼末尾
export const PLATFORM_ORDER = [
  'twitter', 'lingowhale', 'waytoagi', 'github', 'reddit', 'rss', 'hackernews',
  'bilibili', 'manual',
]
