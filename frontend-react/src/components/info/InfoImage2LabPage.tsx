import { useEffect, useState } from 'react'
import {
  Bookmark,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Clock,
  Eye,
  GitFork,
  Heart,
  MessageCircle,
  Play,
  Search,
  Share2,
  Star,
  UserRound,
} from 'lucide-react'
import { cn, formatNumber } from '../../lib/utils'
import { PlatformIcon } from '../shared/PlatformIcon'

const REFERENCE_WIDTH = 1586
const REFERENCE_HEIGHT = 1050

type LabCard = {
  platform: string
  author: string
  title: string
  summary: string
  time: string
  metrics?: Partial<Record<LabMetricKey, number>>
  cover?: string
}

type LabMetricKey = 'likes' | 'comments' | 'views' | 'shares' | 'plays' | 'bookmarks' | 'stars' | 'forks'

const LAB_METRIC_ICONS: [LabMetricKey, typeof Heart][] = [
  ['likes', Heart],
  ['comments', MessageCircle],
  ['views', Eye],
  ['shares', Share2],
  ['plays', Play],
  ['bookmarks', Bookmark],
  ['stars', Star],
  ['forks', GitFork],
]

const CATEGORY_CHIPS = [
  { label: '产品', count: 36, active: true },
  { label: '工具', count: 24 },
  { label: 'Coding', count: 18 },
  { label: 'Skill', count: 12 },
  { label: '模型', count: 10 },
  { label: '技术', count: 8 },
  { label: '行业', count: 7 },
]

const LAB_CARDS: LabCard[] = [
  {
    platform: 'github',
    author: 'tinyhumansai',
    title: 'tinyhumansai/openhuman',
    summary: 'OpenHuman 是 tinyhumansai 推出的个人 AI 产品，定位为私密、简洁且强大的个人 AI 超级智能体。',
    time: '1 天前',
    metrics: { stars: 17000, forks: 1500 },
  },
  {
    platform: 'hackernews',
    author: 'lukaspetersson',
    title: 'We let AIs run radio stations',
    summary: 'Andon Labs 让 AI Agent 完全自主运营电台，包括直播节目和商业运营全流程，无人类干预。实验覆盖零售、媒体等多个领域。',
    time: '1 天前',
    metrics: { comments: 93 },
  },
  {
    platform: 'lingowhale',
    author: 'Datawhale',
    title: '李沐时隔一年，回归B站了！',
    summary: '李沐回归 B 站，发布 Boson AI 团队新产品 Higgs Avatar v1，从静态图生成实时对话 Avatar，支持语音、表情与口型同步。',
    time: '1 天前',
  },
  {
    platform: 'twitter',
    author: '@CursorAI',
    title: 'Cursor 推出记忆功能，可跨项目保留开发上下文',
    summary: 'Cursor 新增长期记忆能力，能在不同项目间保留用户偏好、代码风格与关键上下文，提升开发连续性。',
    time: '5 小时前',
    metrics: { views: 94, comments: 18 },
    cover: '/image2-lab/cursor-memory-cover.png',
  },
  {
    platform: 'twitter',
    author: '@AnthropicAI',
    title: 'Claude 3.5 Sonnet 新增“工具使用预览”模式，提升复杂任务可靠性',
    summary: 'Anthropic 发布 Claude 3.5 Sonnet 的新预览模式，优化了工具调用的稳定性与透明度，开发者可更精确地控制模型行为。',
    time: '3 小时前',
    metrics: { views: 215, comments: 52 },
    cover: '/image2-lab/claude-cover.png',
  },
  {
    platform: 'twitter',
    author: '@figma',
    title: 'Figma Sites 正式上线：从设计到发布，一体化网页构建工具',
    summary: 'Figma 推出 Figma Sites，支持响应式布局、动画和自定义域名发布，设计师可直接完成从设计到上线的全流程。',
    time: '4 小时前',
    metrics: { views: 186, comments: 29 },
    cover: '/image2-lab/figma-sites-cover.png',
  },
  {
    platform: 'github',
    author: '@github',
    title: 'GitHub Copilot Enterprise 新增策略管理与合规中心',
    summary: 'GitHub Copilot Enterprise 增加了组织级策略管理、审计日志和合规中心，帮助企业更好地治理 AI 辅助开发。',
    time: '6 小时前',
    metrics: { views: 342, comments: 41 },
  },
  {
    platform: 'twitter',
    author: '@perplexity_ai',
    title: 'Perplexity 推出 Deep Research API，支持自定义知识探索流程',
    summary: 'Perplexity 发布 Deep Research API，开发者可构建基于搜索与推理的研究应用，支持长链路跨多源引用。',
    time: '7 小时前',
    metrics: { views: 153, comments: 27 },
    cover: '/image2-lab/perplexity-cover.png',
  },
  {
    platform: 'lingowhale',
    author: '晚点LatePost',
    title: '制造豆包：一个 AI 超级入口的形成与转向',
    summary: '晚点深度报道字节跳动 AI 助手豆包从 2023 年创立到日活破亿的产品历程，呈现入口竞争、产品定位与组织取舍。',
    time: '1 天前',
  },
  {
    platform: 'reddit',
    author: 'r/LocalLLaMA',
    title: '本地大模型部署指南 2024：硬件选择与性能调优',
    summary: '社区整理了 2024 年最新的本地大模型部署经验与显卡选择建议，覆盖量化、推理框架与吞吐优化。',
    time: '8 小时前',
    metrics: { views: 277, comments: 64 },
  },
  {
    platform: 'hackernews',
    author: '@levelsio',
    title: 'Maker 的下一站：从工具到分发',
    summary: '从 Indie Hacker 经验出发，探讨独立开发者如何构建可持续的分发渠道，而不是只停留在产品功能迭代。',
    time: '9 小时前',
    metrics: { views: 198, comments: 31 },
  },
  {
    platform: 'rss',
    author: 'TechCrunch',
    title: 'Stripe 收购 Lemon Squeezy，以扩展全球支付生态',
    summary: 'Stripe 宣布收购 SaaS 支付平台 Lemon Squeezy，加速在独立开发者和小型软件公司的支付布局。',
    time: '10 小时前',
    metrics: { views: 221, comments: 22 },
  },
]

const LAB_COLUMNS: LabCard[][] = [
  [LAB_CARDS[0], LAB_CARDS[3], LAB_CARDS[9]],
  [LAB_CARDS[1], LAB_CARDS[4], LAB_CARDS[6], LAB_CARDS[10]],
  [LAB_CARDS[2], LAB_CARDS[5], LAB_CARDS[7], LAB_CARDS[8], LAB_CARDS[11]],
]

function getCanvasScale() {
  if (typeof window === 'undefined') return 1
  return Math.min(1, window.innerWidth / REFERENCE_WIDTH)
}

function useReferenceCanvasScale() {
  const [scale, setScale] = useState(getCanvasScale)
  useEffect(() => {
    function updateScale() {
      setScale(getCanvasScale())
    }
    updateScale()
    window.addEventListener('resize', updateScale)
    return () => window.removeEventListener('resize', updateScale)
  }, [])
  return scale
}

function LabTopBar() {
  return (
    <header className="h-[66px] border-b border-[#ded8cf] bg-[#fbfaf7]" data-testid="image2-lab-topbar">
      <div className="relative flex h-full w-full items-center px-[46px]">
        <a
          href="#v=highlights"
          className="font-brand text-[34px] font-semibold italic leading-none tracking-normal text-[#171410]"
          data-testid="image2-lab-logo"
        >
          info2act
        </a>

        <nav className="absolute left-1/2 top-0 flex h-full -translate-x-1/2 items-center gap-14" aria-label="Image2 预览主导航">
          {[
            { label: '精选', href: '#v=highlights' },
            { label: '信息', href: '#v=info-image2-lab', active: true },
            { label: '行动', href: '#v=actions' },
          ].map((item) => (
            <a
              key={item.label}
              href={item.href}
              className={cn(
                'relative flex h-full items-center font-body-cjk text-[19px] font-medium tracking-normal',
                item.active ? 'text-[#191612]' : 'text-[#6e6962]',
              )}
              aria-current={item.active ? 'page' : undefined}
            >
              {item.label}
              {item.active && <span className="absolute bottom-0 left-1/2 h-[2px] w-14 -translate-x-1/2 bg-[#c65a1e]" />}
            </a>
          ))}
        </nav>

        <div className="ml-auto flex items-center gap-7 text-[#171410]">
          <button
            type="button"
            className="flex h-10 w-10 items-center justify-center rounded-[8px] text-[#171410] transition-colors hover:bg-[#f1ece4]"
            aria-label="搜索"
          >
            <Search className="h-[22px] w-[22px]" strokeWidth={1.8} />
          </button>
          <button
            type="button"
            className="flex items-center gap-3 font-body-cjk text-[16px] font-medium tracking-normal text-[#171410]"
            aria-label="用户菜单"
          >
            <span className="flex h-9 w-9 items-center justify-center rounded-full border border-[#ded8cf] bg-[#fffdf9]">
              <UserRound className="h-[21px] w-[21px]" strokeWidth={1.7} />
            </span>
            <span>向阳乔木</span>
            <ChevronDown className="h-4 w-4" strokeWidth={1.7} />
          </button>
        </div>
      </div>
    </header>
  )
}

function SegmentedControl() {
  return (
    <div className="flex justify-center" data-testid="image2-lab-segmented">
      <div className="inline-flex h-[38px] items-center rounded-full border border-[#ded8cf] bg-[#fffdf9] px-4 shadow-[0_1px_0_rgba(26,25,23,0.03)]">
        <button className="relative h-full px-6 font-body-cjk text-[15px] font-medium text-[#171410]" type="button">
          按分类
          <span className="absolute bottom-[5px] left-6 right-6 h-[2px] bg-[#c65a1e]" />
        </button>
        <span className="mx-3 h-4 w-px bg-[#ded8cf]" />
        <button className="h-full px-6 font-body-cjk text-[15px] font-medium text-[#5f5a53]" type="button">
          按频道
        </button>
      </div>
    </div>
  )
}

function CategoryRail() {
  return (
    <div className="mx-auto mt-[19px] flex w-[1130px] items-center gap-6" data-testid="image2-lab-category-rail">
      <button
        className="flex h-10 w-10 shrink-0 items-center justify-center rounded-[8px] border border-[#ded8cf] bg-[#fbfaf7] text-[#b9b2a9]"
        type="button"
        aria-label="向左滚动分类"
      >
        <ChevronLeft className="h-4 w-4" strokeWidth={1.7} />
      </button>

      <div className="flex min-w-0 flex-1 items-center justify-between gap-4 overflow-hidden">
        {CATEGORY_CHIPS.map((chip) => (
          <button
            key={chip.label}
            className={cn(
              'flex h-[42px] min-w-[122px] items-center justify-center gap-2 rounded-full border px-5 font-body-cjk text-[16px] font-medium tracking-normal',
              chip.active
                ? 'border-[#c65a1e] bg-[#fffdf9] text-[#c65a1e]'
                : 'border-[#ded8cf] bg-[#fbfaf7] text-[#211e19]',
            )}
            type="button"
          >
            <span>{chip.label}</span>
            <span className="font-mono text-[13px] text-current">{chip.count}</span>
          </button>
        ))}
      </div>

      <button
        className="flex h-10 w-10 shrink-0 items-center justify-center rounded-[8px] border border-[#ded8cf] bg-[#fbfaf7] text-[#171410]"
        type="button"
        aria-label="向右滚动分类"
      >
        <ChevronRight className="h-4 w-4" strokeWidth={1.7} />
      </button>
    </div>
  )
}

function EventMeta({ card }: { card: LabCard }) {
  const entries: { Icon: typeof Heart; value: string; key: LabMetricKey }[] = []
  for (const [key, Icon] of LAB_METRIC_ICONS) {
    const value = card.metrics?.[key]
    if (value && entries.length < 2) {
      entries.push({ Icon, value: formatNumber(value), key })
    }
  }

  return (
    <div
      className="ml-auto inline-flex min-w-0 items-center justify-end gap-2.5 font-mono text-[12px] leading-none text-[#827a70]"
      data-testid="image2-lab-card-events"
    >
      {entries.map(({ Icon, value, key }) => (
        <span key={key} className="inline-flex shrink-0 items-center gap-1">
          <Icon className="h-3 w-3" strokeWidth={1.8} />
          <span>{value}</span>
        </span>
      ))}
      <span className="inline-flex shrink-0 items-center gap-1">
        <Clock className="h-3 w-3" strokeWidth={1.8} />
        <span>{card.time}</span>
      </span>
    </div>
  )
}

function NewsCard({ card }: { card: LabCard }) {
  return (
    <article
      className="flex flex-col overflow-hidden rounded-[8px] border border-[#ddd6cc] bg-[#fffdf9] shadow-[0_1px_1px_rgba(26,25,23,0.02)]"
      data-testid="image2-lab-card"
      data-has-cover={card.cover ? 'true' : 'false'}
    >
      {card.cover && (
        <img
          src={card.cover}
          alt=""
          className="aspect-[21/9] w-full border-b border-[#e1dbd2] object-cover"
          data-testid="image2-lab-card-cover"
          loading="eager"
        />
      )}
      <div className={cn(
        'flex flex-1 flex-col px-[18px] pb-[15px]',
        card.cover ? 'pt-[13px]' : 'pt-[17px]',
      )}>
        <h2 className="font-display text-[20px] font-semibold leading-[1.15] tracking-normal text-[#171410]">
          {card.title}
        </h2>
        <p className="mt-[12px] font-body-cjk text-[13px] leading-[1.55] text-[#5d5851] line-clamp-4" data-testid="image2-lab-card-summary">
          {card.summary}
        </p>
        <div
          className="mt-auto flex min-w-0 items-end justify-between gap-3 border-t border-[#e7e0d6] pt-[18px] font-body-cjk"
          data-testid="image2-lab-card-footer"
        >
          <div className="flex min-w-0 items-center gap-2" data-testid="image2-lab-card-source">
            <PlatformIcon platform={card.platform} size="sm" />
            <span className="truncate text-[13px] font-medium leading-none text-[#736d65]">{card.author}</span>
          </div>
          <EventMeta card={card} />
        </div>
      </div>
    </article>
  )
}

export function InfoImage2LabPage() {
  const scale = useReferenceCanvasScale()
  return (
    <div
      className="min-h-screen overflow-hidden bg-[#f5f2ed] text-[#171410]"
      data-testid="image2-lab-page"
      style={{ minHeight: Math.ceil(REFERENCE_HEIGHT * scale) }}
    >
      <div
        className="min-h-[1050px] bg-[#f5f2ed]"
        style={{
          width: REFERENCE_WIDTH,
          transform: `scale(${scale})`,
          transformOrigin: 'top left',
        }}
        data-testid="image2-lab-canvas"
      >
        <LabTopBar />
        <main className="mx-auto w-[1326px] pb-10 pt-[18px]">
          <SegmentedControl />
          <CategoryRail />
          <section className="mt-[30px] grid grid-cols-3 items-start gap-x-7" aria-label="Image2 信息卡片预览">
            {LAB_COLUMNS.map((column, columnIndex) => (
              <div
                key={columnIndex}
                className="flex flex-col gap-4"
                data-testid="image2-lab-column"
              >
                {column.map((card) => (
                  <NewsCard key={card.title} card={card} />
                ))}
              </div>
            ))}
          </section>
        </main>
      </div>
    </div>
  )
}
