/** Feed item from /api/feed */
export interface FeedItem {
  id: string
  title: string
  url?: string
  platform: string
  source?: string
  fetched_at: string
  created_at?: string
  published_at?: string
  lang?: string

  // Author
  author_name?: string
  author_id?: string
  author_avatar?: string

  // Content (list API has cover_url + ai_summary; detail API adds content/detail_json)
  cover_url?: string
  ai_summary?: string
  ai_key_points?: (string | { title: string; points: string[] })[]
  ai_category?: string
  ai_keywords?: string[]
  relevance_score?: number

  // v4.0 multi-tag classification (legacy ai_category remains as primary L1 fallback)
  ai_categories?: string[] | null
  ai_subcategories?: string[] | null
  multi_l1_reason?: string | null
  ai_extracted?: {
    skills?: string[]
    models?: string[]
    event_card?: Record<string, unknown> | null
    [key: string]: unknown
  } | null
  visible?: number

  // Detail-only fields (fetched on click via /api/feed/<id>)
  content?: string
  description?: string
  detail_json?: Record<string, unknown>
  comments_json?: Array<Record<string, unknown>>

  // Metrics (from metrics_json, parsed by backend)
  metrics_json?: Record<string, number>

  // Legacy flat metrics (some backends flatten these)
  likes?: number
  comments?: number
  shares?: number
  views?: number
  plays?: number
  danmaku?: number
  upvotes?: number
  stars?: number
  forks?: number
  bookmarks?: number

  // Tags
  tags_json?: string[]

  thumbnail?: string

  // Media
  media_json?: (string | { url?: string })[]

  // Status flags
  starred_at?: string
  clicked_at?: string
  read_at?: string
  hidden_at?: string

  // Ranking
  ranking_score?: number

  // v12.2 Twitter 视频 ASR
  asr_text?: string
  asr_status?: AsrStatus
  asr_duration_sec?: number
  asr_cost_yuan?: number
  asr_attempted_at?: string
  asr_failed_reason?: string
  asr_provider?: string

  // v12.3 视频 ASR 体验增强
  asr_segments?: AsrSegment[] | null
  asr_text_cn?: string | null
  asr_segments_cn?: (string | null)[] | null

  // Computed
  _isNew?: boolean

  // v15.0 cluster banner: doc 详情页顶部展示所属 cluster
  cluster_id?: number | null
  cluster_title?: string | null
}

/** v12.3: 豆包 ASR 返回的单段时间戳 */
export interface AsrSegment {
  start_ms: number
  end_ms: number
  text: string
}

/** v12.2: Twitter 视频帖 ASR 状态机;v13.0 新增 skipped_quota */
export type AsrStatus =
  | 'running'
  | 'success'
  | 'failed_download'
  | 'failed_extract'
  | 'failed_upload'
  | 'failed_asr'
  | 'failed_empty'
  | 'failed_summary'
  | 'skipped_quota'

/** v12.2: 前端 UI 状态 (映射自 asr_status + null) */
export type TranscriptPanelState = 'idle' | 'running' | 'ready' | 'failed' | 'empty'

/** v12.2: SSE 进度事件 phase 枚举;v12.3 补 translate */
export type AsrPhase =
  | 'download' | 'extract' | 'upload'
  | 'asr_submit' | 'asr_poll' | 'summary' | 'translate' | 'done'

export type ActionStatus =
  | 'pending'
  | 'confirmed'
  | 'executing'
  | 'dispatched'
  | 'done'
  | 'failed'
  | 'dismissed'
  | 'ignored'

export type ActionPriority = 'P0' | 'P1' | 'P2' | 'BUG'

/** Action item from /api/actions */
export interface ActionSourceItem {
  id: string
  platform?: string
  title?: string
  ai_summary?: string | null
  url?: string | null
  referenced_urls?: string[]
}

export interface ActionItem {
  id: string
  title: string
  type: 'research' | 'implementation' | 'investigate' | 'implement' | 'content' | 'track'
  action_type?: 'research' | 'implementation' | 'investigate' | 'implement' | 'content' | 'track'
  status: ActionStatus
  priority?: ActionPriority
  steps?: string[]
  prompt?: string
  expectation?: string
  work_dir?: string
  source_item_ids?: string[]
  source_items?: ActionSourceItem[]
  source_item_count?: number
  ai_reasoning?: string
  decision_brief?: string
  execution_status?: Record<string, unknown> | string | null
  direction?: string
  direction_label?: string
  reason?: string
  score?: number
  source_type?: string
  source_id?: string | number
  created_at: string
  updated_at?: string
  completed_at?: string
}

export interface ActionDirectionSummary {
  slug: string
  label: string
  count: number
}

export interface ActionBoardDirection extends ActionDirectionSummary {
  items: ActionItem[]
  has_more?: boolean
  next_offset?: number | null
}

/** Feed section (grouped by category) */
export interface FeedSection {
  key: string
  label: string
  items: FeedItem[]
  count: number
}

export interface InfoReadModelCursor {
  version_id?: string | null
  scope_key?: string | null
  rank_after?: number | null
  exclude_ids?: string[] | null
}

export type FeedEventsCursor = number | InfoReadModelCursor | null

/** Navigation state — v18.0 nav-merge: 6 tab → 3 tab。
 *  删 recommend / channels / starred / history（PRD §6 + §Spec-1）：
 *  - recommend + channels 合并为 info（信息 tab，复用 ChannelsView 实现）
 *  - starred / history 降级为头像下拉 + 全屏路由 (/starred /history)
 *  L1View 缩窄到 3 项，编译期错误 = 影响面定位入口。
 */
export type L1View = 'highlights' | 'info' | 'actions'

/** v18.0 老 hash 重定向白名单：从老书签/分享链接进入时映射到新 view */
export type LegacyL1View = 'recommend' | 'channels' | 'starred' | 'history'

/** Theme mode */
export type ThemeMode = 'light' | 'dark'

/** API stats response */
export interface StatsResponse {
  total: number
  by_platform: Record<string, number>
  by_category: Record<string, number>
  last_fetch?: string
}

/** Health status */
export interface HealthStatus {
  status: 'ok' | 'warning' | 'error'
  checks: Array<{
    name: string
    status: string
    message?: string
  }>
}

/** SSE event during action generation */
export interface ActionSSEEvent {
  type: 'thinking' | 'progress' | 'result' | 'error'
  data: string
  step?: string
}

/** Subcategory (L2) entry inside a category (L1) — v4.0 */
export interface SubcategoryEntry {
  id: string
  name: string
  examples?: string[]
}

/** Classification config (from /api/classification) */
export interface ClassificationConfig {
  version?: string
  categories: Array<{
    id: string
    name: string
    visible: boolean
    fallback_keywords?: string[]
    description?: string
    priority?: number
    /** v4.0: L2 list rendered as section pills (replaces fallback_keywords as pill source) */
    subcategories?: SubcategoryEntry[]
  }>
}

/** Platform pill for channel view */
export interface PlatformPill {
  key: string
  label: string
  count: number
  priority: 'high' | 'normal' | 'low'
}

// ───────────────────── v15.0 事件聚合 ─────────────────────

/** 事件聚合时间线的单个 cluster (来自 GET /api/feed/events) */
export interface ClusterEventSourcePreview {
  platform: string
  author?: string | null
  source?: string | null
}

export interface ClusterEvent {
  id: number
  ai_title: string
  ai_summary?: string | null
  doc_count: number
  /** v15.1 PRD §5.17：unique_source_count >= 2 是新可见门槛；
   *  BF-0428-1: 也是 EventCard 来源徽章 + cluster 弹窗 header 的显示来源（必填） */
  unique_source_count: number
  /** 主 L1 分类 id,用于精选事件来源行首个标签 */
  category?: string | null
  /** 精选事件列表的轻量来源预览；完整来源仍在 cluster 弹窗中展示 */
  source_preview?: ClusterEventSourcePreview[]
  first_doc_at: string
  last_doc_at: string | null
  platforms: string[]
  cover_url: string | null
  /** per-user：cluster_status 记录存在 AND live_version > last_seen_version；
   *  v15.1 R7.2：first-time viewer (last_seen_version=null) 永远 false */
  has_update: boolean
  live_version: number
  /** v15.1：null = 当前用户从未"看过"该 cluster（cluster_status 无记录），
   *  前端 mount 比对时 null → 不显示更新角标（R7.2 边界） */
  last_seen_version?: number | null
}

export interface ClusterViewerStatus {
  clicked_at?: string | null
  starred_at?: string | null
  last_seen_version?: number | null
}

/** 事件聚合 feed 响应 (GET /api/feed/events) */
export interface FeedEventsResponse {
  /** event_aggregation_ready feature flag */
  enabled: boolean
  events: ClusterEvent[]
  /** next page number or read-model cursor, null 表示已到底 */
  next_cursor: FeedEventsCursor
  /** snapshot 以来新增 cluster 计数 */
  new_since_last_fetch: number
  /** 30 天窗口内总 cluster 数（仅做指标，前端不展示） */
  total_available_within_30d: number
  /** Timeline day counts for the full filtered result, keyed as YYYY-MM-DD in browser-local time. */
  date_counts?: Record<string, number>
  read_model?: string | null
  read_model_version_id?: string | null
  scope_key?: string | null
  degraded?: boolean
  degraded_reason?: string
  data_backend?: string
}

/** cluster 详情 (GET /api/clusters/:id) */
export interface ClusterDetail {
  id: number
  ai_title: string
  ai_summary: string | null
  /** BF-0428-5: cluster key_points 与单 doc 同 schema —— 嵌套对象数组 +
   *  string fallback。支持 [{title, points: []}, ...] 结构,渲染时按
   *  小标题分组(加粗) + sub-points 紫点列表,与 DetailPanel 视觉一致。 */
  ai_key_points: (string | { title: string; points: string[] })[]
  doc_count: number
  /** BF-0428-1：cluster 弹窗 header "来源 N" 显示用 unique_source_count，
   *  与 EventCard 卡片徽章统一（按 source_identity 去重，而非 (platform, author_name) 去重） */
  unique_source_count: number
  platforms: string[]
  category?: string | null
  first_doc_at: string
  last_doc_at: string | null
  cover_url: string | null
  media_urls?: string[]
  live_version: number
  user_last_seen_version: number | null
  viewer_status?: ClusterViewerStatus
  is_visible_in_feed: boolean
  /** merged_into 时返回；前端应 history.replaceState 到新 id */
  redirect_to?: number
}

/** cluster 来源 doc */
export interface ClusterSource {
  item_id: string
  title: string
  author: string | null
  platform: string
  published_at: string | null
  url: string | null
  cover_url?: string | null
  media_urls?: string[]
  /** 0 / 1 (后端返回 int) */
  is_primary_source: number
  /** 'official' / 'community' / null */
  authority_badge: 'official' | 'community' | null
  snippet: string
}

/** cluster 来源响应 (GET /api/clusters/:id/sources) */
export interface ClusterSourcesResponse {
  sources: ClusterSource[]
  next_cursor: number | null
}

export interface ClusterBundleResponse {
  cluster: ClusterDetail
  sources: ClusterSource[]
  sources_next_cursor: number | null
}

export interface LibraryItemEntry {
  id: string
  type: 'item'
  occurred_at: string
  item: FeedItem
}

export interface LibraryClusterEntry {
  id: string
  type: 'cluster'
  occurred_at: string
  cluster: ClusterDetail
}

export type LibraryEntry = LibraryItemEntry | LibraryClusterEntry

export interface LibraryResponse {
  entries: LibraryEntry[]
  total: number
  offset: number
  limit: number
  view: 'history' | 'starred'
}

/** cluster 关联 action (GET /api/clusters/:id/actions) */
export interface ClusterAction {
  id: string
  title: string
  action_type: string
  prompt: string
  steps?: string[]
  priority: string
  status: ActionStatus
  cluster_version: number | null
  is_stale: number
  created_at?: string
  reason?: string
  source_type?: string
  source_id?: string | number
  source_item_ids?: string[]
}

/** 推荐页搜索响应 (GET /api/search?context=recommend) */
export interface SearchRecommendResponse {
  events: ClusterEvent[]
  events_total: number
  docs: FeedItem[]
  docs_total: number
  /** 后端搜索超时降级时为 true;此时 events 为空不代表"无结果" */
  degraded?: boolean
  degraded_reason?: string | null
}

/** 非推荐上下文搜索 (channel/collection/history) */
export interface SearchDocsResponse {
  docs: FeedItem[]
  docs_total: number
}

export type SearchContext = 'recommend' | 'channel' | 'collection' | 'history'
