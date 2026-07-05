#!/usr/bin/env python3
"""Convert all JSON feed data to readable Markdown files with links.
Includes self-validation checklist."""
import json, os, sys
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ERRORS = []

# ============================================================
# 从 config.json 加载配置
# ============================================================
with open(f"{BASE}/config/config.json") as _cf:
    CONFIG = json.load(_cf)

XHS_FEED_FILTER = CONFIG["xiaohongshu"]["filter"]["feed"]
XHS_SEARCH_FILTER = CONFIG["xiaohongshu"]["filter"]["search"]

def xhs_feed_passes_filter(title, desc):
    """Check if XHS feed item passes the content filter."""
    cfg = XHS_FEED_FILTER
    if not cfg["enabled"]:
        return True
    text = (title + " " + desc).lower()
    # Exclude check
    for kw in cfg["keywords_exclude"]:
        if kw.lower() in text:
            return False
    # Include check: at least one keyword must match
    for kw in cfg["keywords_include"]:
        if kw.lower() in text:
            return True
    return False

def xhs_search_passes_filter(interact):
    """Check if XHS search item passes engagement filter."""
    cfg = XHS_SEARCH_FILTER
    if not cfg["enabled"]:
        return True
    likes = int(interact.get('liked_count', '0') or '0')
    collects = int(interact.get('collected_count', '0') or '0')
    comments_str = interact.get('comment_count', '0') or '0'
    comments_val = int(comments_str) if comments_str.isdigit() else 0
    total = likes + collects + comments_val
    return likes >= cfg["min_likes"] and total >= cfg["min_total_engagement"]

def check(condition, msg):
    """Self-validation: record error if condition fails."""
    if not condition:
        ERRORS.append(msg)

def tweet_url(t):
    return f"https://x.com/{t['author']['screenName']}/status/{t['id']}"

def xhs_note_url(note_id):
    return f"https://www.xiaohongshu.com/explore/{note_id}"

# ============================================================
# TWITTER
# ============================================================
def convert_twitter(filename, title):
    path = f"{BASE}/data/sources/twitter/{filename}.json"
    if not os.path.exists(path):
        ERRORS.append(f"[Twitter] 文件不存在: {filename}.json")
        return
    with open(path) as f:
        data = json.load(f)
    tweets = data if isinstance(data, list) else data.get('data', [])
    check(len(tweets) > 0, f"[Twitter] {filename} 推文数为0")

    from datetime import datetime
    date_str = datetime.now().strftime('%Y-%m-%d')
    lines = [f"# {title}", f"> 共 {len(tweets)} 条 | {date_str}\n"]
    for i, t in enumerate(tweets, 1):
        author = t['author']['name']
        screen = t['author']['screenName']
        raw_text = t['text']
        text = raw_text.replace('\n', '\n> ')
        likes = t['metrics']['likes']
        views = t['metrics']['views']
        rts = t['metrics']['retweets']
        bm = t['metrics']['bookmarks']
        time_str = t.get('createdAtLocal', '')
        url = tweet_url(t)
        lang = t.get('lang', '')
        rt_tag = f" 🔁 转推自 @{t['retweetedBy']}" if t.get('isRetweet') else ''
        score = f" | 📊 score:{t['score']:.0f}" if t.get('score') else ''

        # Detect if English content needs translation marker
        is_english = lang == 'en' or (lang not in ('zh', 'ja', 'ko', 'qam', 'zxx', 'und') and
                                       all(ord(c) < 0x4E00 or ord(c) > 0x9FFF for c in raw_text[:100].replace(' ','')))

        # Validation
        check(url.startswith('https://x.com/'), f"[Twitter] 第{i}条缺少有效链接")

        lang_tag = " 🌐EN" if is_english else ""
        lines.append(f"## {i}. @{screen} ({author}){rt_tag}{lang_tag}")
        lines.append(f"🔗 {url}")
        lines.append(f"**{time_str}** | ❤️ {likes} | 🔁 {rts} | 👁️ {views:,} | 🔖 {bm}{score}\n")
        lines.append(f"> {text}\n")

        # English translation placeholder
        if is_english and len(raw_text) > 30:
            lines.append(f"<details><summary>🇨🇳 中文翻译</summary>\n\n待翻译\n\n</details>\n")

        if t.get('quotedTweet'):
            qt = t['quotedTweet']
            qt_screen = qt['author']['screenName']
            qt_text = qt['text'][:200].replace('\n', ' ')
            qt_url = f"https://x.com/{qt_screen}/status/{qt['id']}"
            qt_lang = qt.get('lang', '')
            lines.append(f"📎 引用 @{qt_screen} ({qt_url}): {qt_text}\n")

        if t.get('media'):
            for mi, m in enumerate(t['media'][:4], 1):
                murl = m.get('url', '')
                if m['type'] == 'photo' and murl:
                    lines.append(f"![图{mi}]({murl})")
                elif murl:
                    lines.append(f"🎬 [{m['type']}]({murl})")
            lines.append('')

        if t.get('urls'):
            for u in t['urls']:
                lines.append(f"🔗 {u}")
            lines.append('')

        lines.append("---\n")

    out = f"{BASE}/data/sources/twitter/{filename}.md"
    with open(out, 'w') as f:
        f.write('\n'.join(lines))
    print(f"✅ {out} ({len(tweets)} tweets)")

# ============================================================
# XIAOHONGSHU
# ============================================================
def convert_xhs(filename, title):
    path = f"{BASE}/data/sources/xiaohongshu/{filename}.json"
    if not os.path.exists(path):
        ERRORS.append(f"[XHS] 文件不存在: {filename}.json")
        return
    with open(path) as f:
        data = json.load(f)

    # Handle different structures: combined format (with detail) or raw feed/search
    if isinstance(data, dict) and 'items' in data and data.get('count'):
        # Combined format from feed+read
        items = data['items']
    elif isinstance(data, dict):
        d = data.get('data', {})
        if isinstance(d, dict):
            items = d.get('items', [])
        elif isinstance(d, list):
            items = d
        else:
            items = []
    else:
        items = data

    check(len(items) > 0, f"[XHS] {filename} 笔记数为0")

    # Apply filters
    is_feed = 'recommend' in filename or 'feed' in filename
    is_search = 'search' in filename
    filtered_items = []
    for item in items:
        nc = item.get('detail', {}) or item.get('note_card', {}) or item.get('feed_card', {})
        feed_card = item.get('feed_card', {})
        t_text = nc.get('title', '') or nc.get('display_title', '') or feed_card.get('display_title', '')
        d_text = nc.get('desc', '')
        interact = nc.get('interact_info', {})

        if is_feed and not xhs_feed_passes_filter(t_text, d_text):
            continue
        if is_search and not xhs_search_passes_filter(interact):
            continue
        filtered_items.append(item)

    total_before = len(items)
    items = filtered_items

    lines = [f"# {title}", f"> 原始 {total_before} 条，过滤后 {len(items)} 条 | {datetime.now().strftime('%Y-%m-%d')}\n"]
    has_image = False
    has_desc = False

    for i, item in enumerate(items, 1):
        # Prefer detail (from xhs read) over feed_card
        nc = item.get('detail', {}) or item.get('note_card', {}) or item.get('feed_card', {})
        feed_card = item.get('feed_card', {})
        note_id = item.get('id', '')
        title_text = nc.get('title', '') or nc.get('display_title', '') or feed_card.get('display_title', '无标题')
        user = nc.get('user', {})
        nickname = user.get('nickname', '') or user.get('nick_name', '未知')
        interact = nc.get('interact_info', {})
        likes = interact.get('liked_count', '0')
        collects = interact.get('collected_count', '0')
        comments = interact.get('comment_count', '0')
        note_type = nc.get('type', 'normal')
        corner = nc.get('corner_tag_info', [])
        time_text = corner[0].get('text', '') if corner else ''
        url = xhs_note_url(note_id)

        # Description / body text
        desc = nc.get('desc', '')
        if desc:
            has_desc = True

        # Video duration
        video_info = nc.get('video', {})
        duration = ''
        if video_info:
            capa = video_info.get('capa', {})
            dur = capa.get('duration', 0) if isinstance(capa, dict) else 0
            if dur:
                duration = f" | 🎬 {dur}s"

        # Cover image
        cover = nc.get('cover', {})
        cover_url = cover.get('url_default', '') or cover.get('url_pre', '')
        if not cover_url:
            for info in cover.get('info_list', []):
                if info.get('image_scene') in ('FD_WM_WEBP', 'WB_DFT'):
                    cover_url = info['url']
                    break

        # All images
        image_list = nc.get('image_list', [])
        image_urls = []
        for img in image_list:
            img_url = img.get('url_default', '')
            if not img_url:
                for info in img.get('info_list', []):
                    if info.get('image_scene') in ('WB_DFT', 'FD_WM_WEBP'):
                        img_url = info['url']
                        break
            if img_url:
                image_urls.append(img_url)

        if image_urls or cover_url:
            has_image = True

        # Tags
        tag_list = nc.get('tag_list', [])
        tags = ' '.join([f"#{t.get('name','')}" for t in tag_list if t.get('name')])

        # Validation
        check(note_id, f"[XHS] 第{i}条缺少笔记ID")
        check(url.startswith('https://'), f"[XHS] 第{i}条缺少有效链接")

        lines.append(f"## {i}. {title_text}")
        lines.append(f"🔗 {url}")
        lines.append(f"**{nickname}** | {time_text} | ❤️ {likes} | ⭐ {collects} | 💬 {comments} | 类型: {note_type}{duration}\n")

        if desc:
            clean_desc = desc.replace('\n', '\n> ')
            lines.append(f"### 正文\n")
            lines.append(f"> {clean_desc}\n")

        if tags:
            lines.append(f"🏷️ {tags}\n")

        # Images (max 4)
        if cover_url and not image_urls:
            lines.append(f"![封面]({cover_url})\n")
        elif image_urls:
            for j, img_url in enumerate(image_urls[:4], 1):
                lines.append(f"![图{j}]({img_url})")
            lines.append('')

        # Comments (楼层制 + 树形回复，参考抖音搜索洞察格式)
        item_comments = item.get('comments', [])
        if item_comments:
            lines.append(f"### 评论 ({len(item_comments)}条)\n")
            for ci, c in enumerate(item_comments, 1):
                c_author = c.get('author', '匿名')
                c_content = c.get('content', '').replace('\n', ' ')
                c_likes = c.get('likes', 0)
                c_ip = c.get('ip_location', '')
                c_time = c.get('time', '')
                meta_parts = [f"👍{c_likes}"]
                if c_time:
                    meta_parts.append(c_time)
                if c_ip:
                    meta_parts.append(c_ip)
                lines.append(f"【{ci}楼】{c_author} · {' · '.join(meta_parts)}\n")
                lines.append(f"{c_content}\n")
                for sc in c.get('sub_comments', []):
                    sc_author = sc.get('author', '')
                    sc_content = sc.get('content', '').replace('\n', ' ')
                    sc_likes = sc.get('likes', 0)
                    lines.append(f"└ {sc_author} · 👍{sc_likes}: {sc_content}\n")
            lines.append('')

        lines.append("---\n")

    # Validate
    # Note: feed/hot level data often lacks desc and full images - that's expected
    out = f"{BASE}/data/sources/xiaohongshu/{filename}.md"
    with open(out, 'w') as f:
        f.write('\n'.join(lines))
    print(f"✅ {out} ({len(items)} notes, desc:{has_desc}, images:{has_image})")

# ============================================================
# XIAOHONGSHU - 详细笔记 (xhs read result)
# ============================================================
def convert_xhs_detail(filename, title):
    path = f"{BASE}/data/sources/xiaohongshu/{filename}.json"
    if not os.path.exists(path):
        ERRORS.append(f"[XHS] 文件不存在: {filename}.json")
        return
    with open(path) as f:
        data = json.load(f)

    items = data.get('data', {}).get('items', []) if isinstance(data, dict) else data
    if not items:
        ERRORS.append(f"[XHS] {filename} 无内容")
        return

    nc = items[0].get('note_card', {})
    note_title = nc.get('title', '无标题')
    desc = nc.get('desc', '')
    user = nc.get('user', {})
    nickname = user.get('nickname', '') or user.get('nick_name', '未知')
    interact = nc.get('interact_info', {})
    ip = nc.get('ip_location', '')
    time_val = nc.get('time', 0)

    lines = [f"# {note_title}", ""]
    lines.append(f"**{nickname}** | {ip}")
    lines.append(f"❤️ {interact.get('liked_count', '0')} | ⭐ {interact.get('collected_count', '0')}\n")

    if desc:
        lines.append("## 正文\n")
        lines.append(desc + "\n")

    # images
    images = nc.get('image_list', [])
    if images:
        lines.append("## 图片\n")
        for j, img in enumerate(images, 1):
            img_url = img.get('url_default', '')
            if not img_url:
                for info in img.get('info_list', []):
                    if info.get('image_scene') in ('WB_DFT', 'WB_PRV'):
                        img_url = info['url']
                        break
            if img_url:
                lines.append(f"![图{j}]({img_url})\n")

    out = f"{BASE}/data/sources/xiaohongshu/{filename}.md"
    with open(out, 'w') as f:
        f.write('\n'.join(lines))
    print(f"✅ {out}")

# ============================================================
# BILIBILI
# ============================================================
def convert_bili_feed():
    """B站动态流 - 注意: feed API 不返回 bvid/url，只有动态ID"""
    path = f"{BASE}/data/sources/bilibili/1-feed.json"
    if not os.path.exists(path):
        ERRORS.append("[Bili] 文件不存在: 1-feed.json")
        return
    with open(path) as f:
        data = json.load(f)
    items = data.get('data', {}).get('items', []) if isinstance(data, dict) else data
    check(len(items) > 0, "[Bili] feed 动态数为0")

    lines = ["# B站动态流", f"> 共 {len(items)} 条 | {datetime.now().strftime('%Y-%m-%d')}"]
    lines.append("> ⚠️ 动态流 API 不返回视频直链，需要点击动态查看\n")

    for i, item in enumerate(items, 1):
        author = item.get('author', {}).get('name', '未知')
        title = item.get('title', '')
        text = item.get('text', '')[:300]
        time_label = item.get('published_label', '')
        stats = item.get('stats', {})
        like = stats.get('like', 0)
        comment = stats.get('comment', 0)
        dyn_id = item.get('id', '')

        # 动态链接（非视频直链，但可查看）
        dyn_url = f"https://t.bilibili.com/{dyn_id}" if dyn_id else ''

        lines.append(f"## {i}. {author}")
        if dyn_url:
            lines.append(f"🔗 {dyn_url}")
        lines.append(f"**{time_label}** | ❤️ {like} | 💬 {comment}")
        if title:
            lines.append(f"\n**{title}**")
        if text:
            lines.append(f"\n{text}")
        lines.append("\n---\n")

    out = f"{BASE}/data/sources/bilibili/1-feed.md"
    with open(out, 'w') as f:
        f.write('\n'.join(lines))
    print(f"✅ {out} ({len(items)} items)")

def convert_bili_videos(filename, title):
    """B站视频列表（user-videos, hot, rank, search）"""
    path = f"{BASE}/data/sources/bilibili/{filename}.json"
    if not os.path.exists(path):
        ERRORS.append(f"[Bili] 文件不存在: {filename}.json")
        return
    with open(path) as f:
        data = json.load(f)

    if isinstance(data, list):
        items = data
    elif isinstance(data.get('data'), list):
        items = data['data']
    elif isinstance(data.get('data'), dict):
        items = data['data'].get('items', [])
    else:
        items = []

    check(len(items) > 0, f"[Bili] {filename} 视频数为0")

    # Sort search results by play count (descending) for better discoverability
    if 'search' in filename:
        items.sort(key=lambda x: x.get('play', 0) or x.get('stats', {}).get('view', 0) if isinstance(x.get('stats'), dict) else 0, reverse=True)

    lines = [f"# {title}", f"> 共 {len(items)} 条 | {datetime.now().strftime('%Y-%m-%d')}\n"]
    has_url = False
    has_duration = False

    for i, v in enumerate(items, 1):
        vid_title = v.get('title', '无标题')
        bvid = v.get('bvid', '') or str(v.get('id', ''))
        url = v.get('url', '') or v.get('arcurl', '')
        if not url and bvid and str(bvid).startswith('BV'):
            url = f"https://www.bilibili.com/video/{bvid}"
        if url:
            has_url = True

        # Owner
        owner = v.get('owner', {})
        if isinstance(owner, dict):
            owner_name = owner.get('name', '')
        else:
            owner_name = str(owner) if owner else ''
        author_val = v.get('author', '')
        if isinstance(author_val, dict):
            owner_name = owner_name or author_val.get('name', '')
        elif author_val:
            owner_name = owner_name or str(author_val)

        desc = (v.get('description', '') or '')[:200]

        # Stats - prefer top-level 'play' over nested stats.view
        stats = v.get('stats', {}) or {}
        view = v.get('play', 0) or (stats.get('view', 0) if isinstance(stats, dict) else 0)
        if isinstance(stats, dict):
            like = stats.get('like', 0)
            coin = stats.get('coin', 0)
            fav = stats.get('favorite', 0)
            danmaku = stats.get('danmaku', 0)
        else:
            like = coin = fav = danmaku = 0

        # Duration: user-videos returns 0, note this
        duration = v.get('duration', '')
        duration_s = v.get('duration_seconds', 0)
        if duration and duration != '00:00':
            has_duration = True
        if duration == '00:00' or duration_s == 0:
            duration = '(时长未返回)'

        # Validation
        check(vid_title, f"[Bili] {filename} 第{i}条缺少标题")

        lines.append(f"## {i}. {vid_title}")
        if url:
            lines.append(f"🔗 {url}")
        info_parts = []
        if owner_name:
            info_parts.append(f"UP: {owner_name}")
        if duration and duration != '(时长未返回)':
            info_parts.append(f"⏱ {duration}")
        elif duration == '(时长未返回)':
            info_parts.append(f"⏱ -")
        if view:
            info_parts.append(f"👁️ {view:,}")
        if like:
            info_parts.append(f"❤️ {like:,}")
        if coin:
            info_parts.append(f"🪙 {coin:,}")
        if fav:
            info_parts.append(f"⭐ {fav:,}")
        if danmaku:
            info_parts.append(f"💬 弹幕{danmaku:,}")
        if info_parts:
            lines.append(' | '.join(info_parts))

        # Cover image
        pic = v.get('pic', '')
        if pic:
            if not pic.startswith('http'):
                pic = 'https:' + pic
            lines.append(f"\n![封面]({pic})")

        if desc:
            lines.append(f"\n{desc}")
        lines.append("\n---\n")

    if not has_url:
        check(False, f"[Bili] {filename} 所有视频都缺少URL")

    out = f"{BASE}/data/sources/bilibili/{filename}.md"
    with open(out, 'w') as f:
        f.write('\n'.join(lines))
    print(f"✅ {out} ({len(items)} items, urls:{has_url}, duration:{has_duration})")

def convert_bili_video_detail(filename="video-BV1YxckzbEaT"):
    """B站单个视频详情（含字幕+评论+AI总结）"""
    path = f"{BASE}/data/sources/bilibili/{filename}.json"
    if not os.path.exists(path):
        ERRORS.append("[Bili] 视频详情文件不存在")
        return
    with open(path) as f:
        data = json.load(f)

    v = data.get('data', {}).get('video', {})
    sub = data.get('data', {}).get('subtitle', {})
    ai_sum = data.get('data', {}).get('ai_summary', '')
    comments = data.get('data', {}).get('comments', [])

    check(v.get('title'), "[Bili] 视频详情缺少标题")
    check(v.get('url'), "[Bili] 视频详情缺少URL")

    lines = [f"# {v.get('title', '')}", ""]
    url = v.get('url', '')
    lines.append(f"🔗 {url}")
    owner = v.get('owner', {})
    stats = v.get('stats', {})
    lines.append(f"**UP: {owner.get('name','')}** | ⏱ {v.get('duration','')} | "
                 f"👁️ {stats.get('view',0):,} | ❤️ {stats.get('like',0):,} | "
                 f"🪙 {stats.get('coin',0):,} | ⭐ {stats.get('favorite',0):,} | "
                 f"💬 弹幕{stats.get('danmaku',0):,}")
    lines.append(f"\n> {v.get('description','')}\n")

    # B站 AI 摘要
    if ai_sum:
        lines.append("## B站 AI 摘要\n")
        lines.append(ai_sum + "\n")
    else:
        lines.append("## B站 AI 摘要\n")
        lines.append("(该视频无B站官方AI摘要)\n")

    # AI 总结占位（由外部 LLM 生成后回填）
    subtitle_text = sub.get('text', '') if sub.get('available') else ''
    lines.append("## AI 内容总结\n")
    lines.append("{{AI_SUMMARY_PLACEHOLDER}}\n")

    # 字幕
    if sub.get('available') and subtitle_text:
        lines.append("## 字幕全文\n")
        lines.append(subtitle_text)
        lines.append("")
        check(len(subtitle_text) > 50, "[Bili] 字幕内容过短")
    else:
        lines.append("## 字幕\n")
        lines.append("(该视频无字幕，可使用 `bili audio` + `mlx_whisper` 进行 ASR 转录)\n")

    # 热门评论
    if comments:
        lines.append(f"\n## 热门评论 (共{len(comments)}条)\n")
        for c in comments:
            author_info = c.get('author', {})
            if isinstance(author_info, dict):
                cname = author_info.get('name', '匿名')
            else:
                cname = str(author_info)
            msg = c.get('message', '') or c.get('content', '')
            clike = c.get('like', 0)
            reply_count = c.get('reply_count', 0)
            lines.append(f"### 💬 {cname} (❤️ {clike} | {reply_count}条回复)")
            lines.append(f"> {msg}\n")
        check(True, "")  # comments exist
    else:
        lines.append("\n## 热门评论\n")
        lines.append("(无评论数据)\n")
        check(False, "[Bili] 视频详情缺少评论数据")

    out = f"{BASE}/data/sources/bilibili/{filename}.md"
    with open(out, 'w') as f:
        f.write('\n'.join(lines))
    print(f"✅ {out} (subtitle:{sub.get('available',False)}, comments:{len(comments)}, ai_summary:{bool(ai_sum)})")

# ============================================================
# MAIN
# ============================================================
def main():
    print("=" * 60)
    print("开始转换所有数据...")
    print("=" * 60)

    # Twitter
    convert_twitter("1-following-feed", "Twitter 关注流 (Following)")
    convert_twitter("2-for-you-feed", "Twitter 推荐流 (For You)")
    convert_twitter("3-ai-search", "Twitter AI 搜索")
    if os.path.exists(f"{BASE}/data/sources/twitter/4-bookmarks.json"):
        convert_twitter("4-bookmarks", "Twitter 书签 (Bookmarks)")

    # XHS
    convert_xhs("1-recommend-feed", "小红书推荐流")
    # Search - global + platform-specific keywords
    xhs_kws = CONFIG["global"]["search_keywords"] + CONFIG["xiaohongshu"]["search"].get("extra_keywords", [])
    seen_kws = set()
    for kw in xhs_kws:
        if kw in seen_kws: continue
        seen_kws.add(kw)
        safe_kw = kw.replace(" ", "_")
        fn = f"search-{safe_kw}"
        if os.path.exists(f"{BASE}/data/sources/xiaohongshu/{fn}.json"):
            convert_xhs(fn, f"小红书搜索: {kw}")
    convert_xhs("3-hot", "小红书热门")
    if os.path.exists(f"{BASE}/data/sources/xiaohongshu/favorites.json"):
        convert_xhs("favorites", "小红书收藏")

    # Bili
    convert_bili_feed()
    # UP主视频 - 从 config 动态生成
    for up in CONFIG["bilibili"]["up_list"]:
        safe_name = up["name"].replace(" ", "_")
        fn = f"up-{safe_name}"
        if os.path.exists(f"{BASE}/data/sources/bilibili/{fn}.json"):
            convert_bili_videos(fn, f"B站 UP主 - {up['name']}")
    convert_bili_videos("3-hot", "B站热门视频")
    convert_bili_videos("4-rank", "B站全站排行榜")
    if os.path.exists(f"{BASE}/data/sources/bilibili/watch-later.json"):
        convert_bili_videos("watch-later", "B站稍后再看")
    # 搜索 - global + platform-specific keywords
    bili_kws = CONFIG["global"]["search_keywords"] + CONFIG["bilibili"]["search"].get("extra_keywords", [])
    seen_bili_kws = set()
    for kw in bili_kws:
        if kw in seen_bili_kws: continue
        seen_bili_kws.add(kw)
        safe_kw = kw.replace(" ", "_")
        for suffix, label in [("recommended", "综合推荐"), ("most-played", "最多播放")]:
            fn = f"search-{safe_kw}-{suffix}"
            if os.path.exists(f"{BASE}/data/sources/bilibili/{fn}.json"):
                convert_bili_videos(fn, f"B站搜索: {kw} ({label})")
    # 视频详情 - 遍历所有 video-*.json
    for fn in sorted(os.listdir(f"{BASE}/data/sources/bilibili")):
        if fn.startswith("video-") and fn.endswith(".json"):
            convert_bili_video_detail(fn.replace(".json", ""))

    # Self-validation report
    print("\n" + "=" * 60)
    print("自检报告")
    print("=" * 60)
    real_errors = [e for e in ERRORS if e]
    if real_errors:
        print(f"⚠️ 发现 {len(real_errors)} 个问题:")
        for e in real_errors:
            print(f"  ❌ {e}")
    else:
        print("✅ 全部检查通过")

    # Check file completeness - scan actual .md files
    print("\n📋 文件完整性检查:")
    for platform in ["twitter", "xiaohongshu", "bilibili"]:
        pdir = f"{BASE}/data/sources/{platform}"
        if not os.path.exists(pdir): continue
        for fn in sorted(os.listdir(pdir)):
            if fn.endswith('.md'):
                fp = f"{pdir}/{fn}"
                size = os.path.getsize(fp)
                status = "✅" if size > 100 else "⚠️ 内容过少"
                print(f"  {status} {platform}/{fn} ({size:,} bytes)")

    # Content quality checks
    print("\n📋 内容质量检查:")
    checks = [
        ("Twitter 链接", "data/sources/twitter/1-following-feed.md", "https://x.com/"),
        ("Twitter For-You", "data/sources/twitter/2-for-you-feed.md", "https://x.com/"),
        ("小红书链接", "data/sources/xiaohongshu/1-recommend-feed.md", "https://www.xiaohongshu.com/explore/"),
        ("B站动态链接", "data/sources/bilibili/1-feed.md", "https://t.bilibili.com/"),
        ("B站视频链接", "data/sources/bilibili/3-hot.md", "https://www.bilibili.com/video/"),
        ("B站评论", "data/sources/bilibili/video-BV1YxckzbEaT.md", "## 热门评论"),
        ("B站字幕", "data/sources/bilibili/video-BV1YxckzbEaT.md", "## 字幕全文"),
    ]
    for name, fn, keyword in checks:
        fp = f"{BASE}/{fn}"
        if os.path.exists(fp):
            with open(fp) as f:
                content = f.read()
            found = keyword in content
            print(f"  {'✅' if found else '❌'} {name}: {'包含' if found else '缺少'} '{keyword}'")
        else:
            print(f"  ❌ {name}: 文件不存在")

if __name__ == '__main__':
    main()
