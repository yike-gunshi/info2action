export type ChangelogRelease = {
  id: string
  publishedAt: string
  title: string
  changes: readonly {
    kind: 'feature' | 'improvement' | 'fix'
    title: string
    description: string
    action?: { label: string; href: string }
    media?: readonly { src: string; alt: string; width: number; height: number }[]
  }[]
}

// Only include public, shipped changes. Keep each release ID stable after publication.
// GIFs reference the reviewed, immutable public GitHub archive.
export const CHANGELOG_RELEASES: readonly ChangelogRelease[] = [
  {
    "id": "2026-10-10-reading",
    "publishedAt": "2026-10-10T00:00:00Z",
    "title": "阅读弹窗更清爽",
    "changes": [
      {
        "kind": "improvement",
        "title": "滚动条只在滚动时出现",
        "description": "信息、精选和行动弹窗平时不显示滚动条，滚动时出现细滑块，停下后自动隐藏，正文宽度保持不变。"
      },
      {
        "kind": "improvement",
        "title": "正文两端对齐",
        "description": "在电脑和平板上，弹窗里的正文与要点两端对齐，右边缘更整齐。"
      },
      {
        "kind": "improvement",
        "title": "三个弹窗体验一致",
        "description": "关闭按钮、加载、出错重试和看大图的样式统一，日期显示到年份。"
      },
      {
        "kind": "fix",
        "title": "修复部分信息打不开",
        "description": "修复通过分享链接打开某些信息时，弹窗不显示、也无法重试的问题。"
      }
    ]
  },
  {
    "id": "2026-09-21-product-changelog",
    "publishedAt": "2026-09-21T00:00:00Z",
    "title": "更新日志上线",
    "changes": [
      {
        "kind": "feature",
        "title": "无需登录，即可查看",
        "description": "点击登录或头像左侧的日志图标，即可查看近期更新。有新内容时显示小圆点，查看后消失。"
      }
    ]
  },
  {
    "id": "2026-09-16",
    "publishedAt": "2026-09-16T00:00:00Z",
    "title": "每日要点跟随分类",
    "changes": [
      {
        "kind": "improvement",
        "title": "切换分类，要点同步更新",
        "description": "在精选页选择「模型」「产品」等分类，每日要点会同步展示对应内容。"
      },
      {
        "kind": "improvement",
        "title": "历史加载有提示",
        "description": "查看历史日期时，继续向上滚动可以加载相邻较新日期的内容。加载过程中显示提示，失败时可直接重试。"
      }
    ]
  },
  {
    "id": "2026-09-12",
    "publishedAt": "2026-09-12T00:00:00Z",
    "title": "精选支持按日期回看",
    "changes": [
      {
        "kind": "feature",
        "title": "按日期找回历史内容",
        "description": "通过桌面的日期目录或手机上的日期菜单，直接跳转到某一天，也可以一键回到最新。"
      },
      {
        "kind": "fix",
        "title": "修正异常日期",
        "description": "修复未来日期混入精选的问题，日期目录与内容统计保持一致。"
      }
    ]
  },
  {
    "id": "2026-08-26",
    "publishedAt": "2026-08-26T00:00:00Z",
    "title": "视频与配图浏览改进",
    "changes": [
      {
        "kind": "improvement",
        "title": "视频与配图分开浏览",
        "description": "视频按原始比例显示，补充图片在下方单独浏览，支持滑动和点击放大。"
      },
      {
        "kind": "fix",
        "title": "修复视频播放与封面重复",
        "description": "修复部分 X 视频无法播放、视频封面在图片区重复显示的问题。"
      }
    ]
  },
  {
    "id": "2026-08-02",
    "publishedAt": "2026-08-02T00:00:00Z",
    "title": "每日要点上线",
    "changes": [
      {
        "kind": "feature",
        "title": "先看当天要点",
        "description": "精选页新增每日要点，按天汇总重要更新。点击一条要点，即可打开详情。"
      }
    ]
  },
  {
    "id": "2026-07-26",
    "publishedAt": "2026-07-26T00:00:00Z",
    "title": "手机阅读与已读同步",
    "changes": [
      {
        "kind": "improvement",
        "title": "手机卡片增加缩略图",
        "description": "手机端精选卡片增加配图，修复部分标题底部被裁切的问题。"
      },
      {
        "kind": "feature",
        "title": "不同设备同步已读",
        "description": "同一账号在手机上打开过的内容，电脑端返回页面后也会同步标记为已读。"
      },
      {
        "kind": "improvement",
        "title": "改善 Reddit 视频展示",
        "description": "详情页支持展示可嵌入的 Reddit 视频，并保留原帖入口。"
      }
    ]
  },
  {
    "id": "2026-07-17",
    "publishedAt": "2026-07-17T00:00:00Z",
    "title": "精选摘要更具体",
    "changes": [
      {
        "kind": "improvement",
        "title": "先看清发生了什么",
        "description": "精选摘要更直接地呈现关键事实与影响。打开事件后，可以继续查看分点梳理和相关来源。",
        "media": [
          {
            "src": "https://raw.githubusercontent.com/yike-gunshi/info2action/0b8ce7ffc420fa26d3a9ade8b61a4d5850b25bb8/docs/assets/readme/highlights-demo.gif",
            "alt": "精选事件与详情",
            "width": 1024,
            "height": 650
          }
        ]
      }
    ]
  },
  {
    "id": "2026-07-06",
    "publishedAt": "2026-07-06T00:00:00Z",
    "title": "把信息变成可执行行动",
    "changes": [
      {
        "kind": "feature",
        "title": "从内容生成行动点",
        "description": "在信息或事件详情中生成行动点，结合个人画像，把值得做的事情拆成具体步骤。",
        "media": [
          {
            "src": "https://raw.githubusercontent.com/yike-gunshi/info2action/0b8ce7ffc420fa26d3a9ade8b61a4d5850b25bb8/docs/assets/readme/action-generate-demo.gif",
            "alt": "生成行动点",
            "width": 2048,
            "height": 1298
          }
        ]
      },
      {
        "kind": "feature",
        "title": "复制 Prompt，交给 AI 执行",
        "description": "行动卡提供包含任务背景与执行要求的 Prompt，可以直接复制给 Claude Code 或 Codex 使用。",
        "media": [
          {
            "src": "https://raw.githubusercontent.com/yike-gunshi/info2action/0b8ce7ffc420fa26d3a9ade8b61a4d5850b25bb8/docs/assets/readme/action-copy-prompt.gif",
            "alt": "复制行动 Prompt",
            "width": 1440,
            "height": 912
          }
        ]
      }
    ]
  },
  {
    "id": "2026-05-27",
    "publishedAt": "2026-05-27T00:00:00Z",
    "title": "暗色模式覆盖详情阅读",
    "changes": [
      {
        "kind": "improvement",
        "title": "列表与详情统一暗色外观",
        "description": "切换暗色模式后，精选详情弹窗的背景、文字和控件会同步调整。",
        "media": [
          {
            "src": "https://raw.githubusercontent.com/yike-gunshi/info2action/0b8ce7ffc420fa26d3a9ade8b61a4d5850b25bb8/docs/assets/readme/dark-mode-demo.gif",
            "alt": "暗色阅读",
            "width": 1152,
            "height": 730
          }
        ]
      }
    ]
  },
  {
    "id": "2026-05-25",
    "publishedAt": "2026-05-25T00:00:00Z",
    "title": "收藏、回看与内容阅读",
    "changes": [
      {
        "kind": "feature",
        "title": "事件也能收藏与回看",
        "description": "在事件详情中收藏内容。收藏和浏览历史统一收录信息与事件，方便找回之前看过的内容。",
        "media": [
          {
            "src": "https://raw.githubusercontent.com/yike-gunshi/info2action/0b8ce7ffc420fa26d3a9ade8b61a4d5850b25bb8/docs/assets/readme/starred-demo.gif",
            "alt": "我的收藏",
            "width": 1440,
            "height": 912
          },
          {
            "src": "https://raw.githubusercontent.com/yike-gunshi/info2action/0b8ce7ffc420fa26d3a9ade8b61a4d5850b25bb8/docs/assets/readme/history-demo.gif",
            "alt": "浏览历史",
            "width": 1440,
            "height": 912
          }
        ]
      },
      {
        "kind": "improvement",
        "title": "边看视频，边读转写",
        "description": "视频下方集中显示转写内容与双语字幕，支持跟随播放、点击字幕跳转，以及导出字幕。",
        "media": [
          {
            "src": "https://raw.githubusercontent.com/yike-gunshi/info2action/0b8ce7ffc420fa26d3a9ade8b61a4d5850b25bb8/docs/assets/readme/video-translate-demo.gif",
            "alt": "视频与双语转写",
            "width": 1280,
            "height": 812
          }
        ]
      },
      {
        "kind": "improvement",
        "title": "提交链接后跟踪处理结果",
        "description": "粘贴链接即可提交分析。面板会显示分析中、已完成或已存在等状态，方便查看处理结果。",
        "media": [
          {
            "src": "https://raw.githubusercontent.com/yike-gunshi/info2action/0b8ce7ffc420fa26d3a9ade8b61a4d5850b25bb8/docs/assets/readme/submit-link-demo.gif",
            "alt": "提交链接",
            "width": 2048,
            "height": 1298
          }
        ]
      }
    ]
  },
  {
    "id": "2026-05-15",
    "publishedAt": "2026-05-15T00:00:00Z",
    "title": "按分类浏览信息",
    "changes": [
      {
        "kind": "feature",
        "title": "按主题找内容，也能按来源浏览",
        "description": "信息页支持按内容分类或信源频道浏览。切换到「产品」「工具」「模型」等分类，可以集中查看相关内容。",
        "media": [
          {
            "src": "https://raw.githubusercontent.com/yike-gunshi/info2action/0b8ce7ffc420fa26d3a9ade8b61a4d5850b25bb8/docs/assets/readme/info-feed-demo.gif",
            "alt": "按频道与分类浏览",
            "width": 1280,
            "height": 812
          }
        ]
      }
    ]
  }
]
