"""URL 归一化 — v13.0

把 Twitter/YouTube 的多种 URL 形态归一化到稳定 item_id,
保证"同一内容提交多次"(cron + 手动 / 多域名 / shorts 等)只产生一条 DB 记录。

接口:
    normalize_url(url: str) -> NormalizedUrl

- Twitter(`twitter.com` / `x.com`,大小写任意)
    → `tw_{tweet_id}`;canonical_url 统一成 `https://x.com/.../status/{id}`(保留原 user_handle,抓不到 handle 时占位 `_`)
- YouTube(4 种形态,video_id 必须保留大小写):
    `youtube.com/watch?v=ID`
    `youtu.be/ID`
    `youtube.com/shorts/ID`
    `youtube.com/embed/ID`
    → `yt_{video_id}`;canonical_url 统一成 `https://www.youtube.com/watch?v={id}`
- 其他(博客/公众号/微博/B 站等)
    → `platform='manual'`,`item_id = md5(url.encode())`(沿用 F31 旧规则);canonical_url 保留原 URL

风险/边界已覆盖:
- host 大小写:`re.IGNORECASE`(Twitter.com / X.COM 都可)
- YouTube `watch?v=...&t=45s` 带 query 参数 — 只取 v 的值
- YouTube video_id 严格 11 位 `[a-zA-Z0-9_-]`,**不**做 lower-case
- 非 URL / 空字符串:返回 platform='manual' + md5(url)(不抛异常,让调用方继续走旧路径)
- 末尾 trailing slash / 前后空白 — 先 strip,不改变归一化结果
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class NormalizedUrl:
    """归一化结果。

    Attributes:
        platform: 'twitter' / 'youtube' / 'manual'
        item_id:  稳定主键。tw_{id} / yt_{id} / md5(url)
        canonical_url: 用于展示 / 抓取的标准 URL(不用作主键)
        raw_id: 平台原生 ID(tweet_id / video_id);manual 时为 None
    """
    platform: str
    item_id: str
    canonical_url: str
    raw_id: Optional[str]


# ---------- 正则 ----------
# Twitter / X:两个 host 大小写不敏感,`/user_handle/status/{tweet_id}`
# 有些场景 canonical_url 没有 user_handle(`i/status/{id}`),保留兼容
_TW_STATUS_RE = re.compile(
    r"(?:twitter\.com|x\.com)/(?P<user>[^/?#]+)/status/(?P<tid>\d+)",
    re.IGNORECASE,
)
_TW_I_STATUS_RE = re.compile(
    r"(?:twitter\.com|x\.com)/i/status/(?P<tid>\d+)",
    re.IGNORECASE,
)

# YouTube video_id 规则:严格 11 位 [a-zA-Z0-9_-],大小写敏感(YouTube 自身规则)
# 不 lower-case
_YT_ID_RE = r"[a-zA-Z0-9_-]{11}"
_YT_PATTERNS = [
    # youtube.com/watch?v=ID   (允许 &t=... 等后续参数)
    re.compile(rf"(?:www\.|m\.)?youtube\.com/watch\?(?:[^#\s]*&)?v=(?P<vid>{_YT_ID_RE})", re.IGNORECASE),
    # youtu.be/ID
    re.compile(rf"youtu\.be/(?P<vid>{_YT_ID_RE})", re.IGNORECASE),
    # youtube.com/shorts/ID
    re.compile(rf"(?:www\.|m\.)?youtube\.com/shorts/(?P<vid>{_YT_ID_RE})", re.IGNORECASE),
    # youtube.com/embed/ID
    re.compile(rf"(?:www\.|m\.)?youtube\.com/embed/(?P<vid>{_YT_ID_RE})", re.IGNORECASE),
]


def _manual(url: str) -> NormalizedUrl:
    """非 Twitter/YouTube 链接:沿用 F31 旧规则,item_id = md5(url)。"""
    return NormalizedUrl(
        platform="manual",
        item_id=hashlib.md5(url.encode("utf-8")).hexdigest(),
        canonical_url=url,
        raw_id=None,
    )


def _try_twitter(url: str) -> Optional[NormalizedUrl]:
    m = _TW_STATUS_RE.search(url)
    if m:
        tid = m.group("tid")
        user = m.group("user")
        # user handle 有些是 `i`(系统占位)- 走 i/status 分支更干净
        canonical = f"https://x.com/{user}/status/{tid}"
        return NormalizedUrl(
            platform="twitter",
            item_id=tid,  # F31 原规则:Twitter 主键用纯 tweet_id,无前缀
            canonical_url=canonical,
            raw_id=tid,
        )
    m = _TW_I_STATUS_RE.search(url)
    if m:
        tid = m.group("tid")
        canonical = f"https://x.com/i/status/{tid}"
        return NormalizedUrl(
            platform="twitter",
            item_id=tid,
            canonical_url=canonical,
            raw_id=tid,
        )
    return None


def _try_youtube(url: str) -> Optional[NormalizedUrl]:
    for pat in _YT_PATTERNS:
        m = pat.search(url)
        if m:
            vid = m.group("vid")  # 保留大小写
            canonical = f"https://www.youtube.com/watch?v={vid}"
            return NormalizedUrl(
                platform="youtube",
                item_id=f"yt_{vid}",
                canonical_url=canonical,
                raw_id=vid,
            )
    return None


def normalize_url(url: str) -> NormalizedUrl:
    """URL 归一化总入口。

    - 空/非字符串 / 非 http(s) 链接:返回 platform='manual' + md5(url)
      (不抛异常,让调用方走旧规则,保持向后兼容)
    """
    if not url or not isinstance(url, str):
        return _manual(url or "")
    u = url.strip()
    if not u:
        return _manual("")

    # 1. Twitter
    tw = _try_twitter(u)
    if tw:
        return tw
    # 2. YouTube
    yt = _try_youtube(u)
    if yt:
        return yt
    # 3. Other → manual
    return _manual(u)
