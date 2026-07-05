#!/usr/bin/env python3
"""Enhanced data fetcher: B站封面图 + B站搜索排序 + 小红书扩大推荐流"""
import json, subprocess, time, os, shutil, urllib.request, urllib.parse

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 小红书 CLI：XHS_BIN 环境变量优先，缺省回退 PATH 查找；未安装则跳过 xhs 抓取
XHS = os.environ.get("XHS_BIN") or shutil.which("xhs") or ""
_XHS_DIR = os.path.dirname(XHS)
_XHS_ENV = {**os.environ, 'PATH': (_XHS_DIR + ':' if _XHS_DIR else '') + os.environ.get('PATH', '')}
BILI_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://www.bilibili.com",
}

def bili_api(url):
    """Call B站 API via curl to avoid Python SSL issues."""
    result = subprocess.run(
        ['curl', '-s', url, '-H', 'User-Agent: Mozilla/5.0', '-H', 'Referer: https://www.bilibili.com'],
        capture_output=True, text=True, timeout=15
    )
    return json.loads(result.stdout)

def enrich_bili_covers(filename):
    """Enrich B站 video list with cover images from API."""
    path = f"{BASE}/data/sources/bilibili/{filename}.json"
    if not os.path.exists(path):
        print(f"  ⚠️ {filename}.json 不存在")
        return
    with open(path) as f:
        data = json.load(f)

    # Extract items
    if isinstance(data, list):
        items = data
    elif isinstance(data.get('data'), list):
        items = data['data']
    elif isinstance(data.get('data'), dict):
        items = data['data'].get('items', [])
    else:
        items = []

    enriched = 0
    for v in items:
        bvid = v.get('bvid', '') or v.get('id', '')
        bvid = str(bvid)
        if not bvid or not bvid.startswith('BV'):
            continue
        if v.get('pic'):  # already has cover
            enriched += 1
            continue
        try:
            api_data = bili_api(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}")
            vd = api_data.get('data', {})
            if vd.get('pic'):
                v['pic'] = vd['pic']
                # Also fill in missing stats/duration
                if not v.get('duration') or v.get('duration') == '00:00':
                    dur_s = vd.get('duration', 0)
                    if dur_s:
                        m, s = divmod(dur_s, 60)
                        v['duration'] = f"{m}:{s:02d}"
                        v['duration_seconds'] = dur_s
                if not v.get('stats') or v.get('stats') == {}:
                    v['stats'] = vd.get('stat', {})
                enriched += 1
            time.sleep(0.3)  # rate limit
        except Exception as e:
            print(f"  ⚠️ {bvid}: {e}")

    # Save back
    with open(path, 'w') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  ✅ {filename}: {enriched}/{len(items)} 条已补充封面")

def fetch_bili_search(keyword, order="", n=10, filename=""):
    """Fetch B站 search via API with sort order and covers.
    order: '' (综合推荐), 'click' (最多播放), 'stow' (最多收藏)
    """
    encoded_kw = urllib.parse.quote(keyword)
    url = f"https://api.bilibili.com/x/web-interface/search/type?search_type=video&keyword={encoded_kw}&order={order}&page=1"
    data = bili_api(url)
    results = data.get('data', {}).get('result', [])[:n]

    # Clean up HTML tags in titles and add full pic URLs
    for r in results:
        r['title'] = r.get('title', '').replace('<em class="keyword">', '').replace('</em>', '')
        pic = r.get('pic', '')
        if pic and not pic.startswith('http'):
            r['pic'] = 'https:' + pic

    out = {'data': results}
    path = f"{BASE}/data/sources/bilibili/{filename}.json"
    with open(path, 'w') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"  ✅ {filename}: {len(results)} 条 (order={order or '综合'})")

def fetch_xhs_comments(note_id, max_retries=2):
    """Fetch comments for a single XHS note."""
    if not XHS:
        return []
    for attempt in range(max_retries):
        try:
            r = subprocess.run(
                [XHS, 'comments', note_id, '--json'],
                capture_output=True, text=True, timeout=15,
                env=_XHS_ENV
            )
            if r.returncode == 0 and r.stdout.strip():
                data = json.loads(r.stdout)
                comments = data.get('data', {}).get('comments', [])
                return [{
                    'author': c.get('user_info', {}).get('nickname', ''),
                    'content': c.get('content', ''),
                    'likes': c.get('like_count', 0),
                    'time': c.get('create_time', ''),
                    'sub_comments': [{
                        'author': sc.get('user_info', {}).get('nickname', ''),
                        'content': sc.get('content', ''),
                        'likes': sc.get('like_count', 0),
                    } for sc in c.get('sub_comments', [])[:3]]
                } for c in comments[:5]]  # Top 5 comments
        except Exception:
            pass
        time.sleep(1)
    return []

def fetch_xhs_feed_expanded(target=25, batch_delay=5):
    """Fetch expanded XHS feed with pagination, respecting rate limits."""
    if not XHS:
        print("  ⚠️ 未找到 xhs CLI（设置 XHS_BIN 或加入 PATH），跳过小红书抓取")
        return
    all_items = []
    cursor = ''
    batch = 0

    while len(all_items) < target and batch < 5:
        batch += 1
        print(f"  抓取第 {batch} 批...")
        try:
            cmd = [XHS, 'feed', '--json']
            env = _XHS_ENV
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=20, env=env)
            if result.returncode != 0:
                print(f"  ⚠️ feed 返回错误: {result.stderr[:80]}")
                break
            data = json.loads(result.stdout)
            items = data.get('data', {}).get('items', [])
            if not items:
                print(f"  ⚠️ 第 {batch} 批无数据")
                break

            # Deduplicate by ID
            existing_ids = {item.get('id') for item in all_items}
            new_items = [item for item in items if item.get('id') not in existing_ids]
            all_items.extend(new_items)
            print(f"    获取 {len(items)} 条，新增 {len(new_items)} 条，累计 {len(all_items)} 条")

            if len(new_items) == 0:
                print(f"  ℹ️ 无新内容，停止")
                break
        except Exception as e:
            print(f"  ❌ {e}")
            break

        if len(all_items) >= target:
            break
        print(f"    等待 {batch_delay}s 避免风控...")
        time.sleep(batch_delay)

    # Now read details for each (with delays)
    print(f"\n  开始获取 {len(all_items)} 条详情...")
    detailed = []
    for i, item in enumerate(all_items[:target]):
        note_id = item.get('id', '')
        title = item.get('note_card', {}).get('display_title', '')[:30]
        print(f"    [{i+1}/{min(len(all_items), target)}] {note_id}: {title}...")
        try:
            r = subprocess.run(
                [XHS, 'read', note_id, '--json'],
                capture_output=True, text=True, timeout=15,
                env=_XHS_ENV
            )
            if r.returncode == 0 and r.stdout.strip():
                detail = json.loads(r.stdout)
                detail_items = detail.get('data', {}).get('items', [])
                if detail_items:
                    nc = detail_items[0].get('note_card', {})
                    # Also fetch comments
                    time.sleep(1.5)
                    comments = fetch_xhs_comments(note_id)
                    detailed.append({
                        'id': note_id,
                        'feed_card': item.get('note_card', {}),
                        'detail': nc,
                        'comments': comments
                    })
                    c_count = len(comments)
                    print(f"      ✅ +{c_count}条评论")
                else:
                    detailed.append({'id': note_id, 'feed_card': item.get('note_card', {}), 'detail': {}, 'comments': []})
            else:
                detailed.append({'id': note_id, 'feed_card': item.get('note_card', {}), 'detail': {}, 'comments': []})
        except Exception as e:
            detailed.append({'id': note_id, 'feed_card': item.get('note_card', {}), 'detail': {}, 'comments': []})
            print(f"      ❌ {e}")
        time.sleep(2)  # conservative rate limit

    output = {'count': len(detailed), 'items': detailed}
    path = f"{BASE}/data/sources/xiaohongshu/1-recommend-feed.json"
    with open(path, 'w') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    desc_count = sum(1 for d in detailed if d.get('detail', {}).get('desc'))
    print(f"\n  ✅ 推荐流: {len(detailed)} 条 (有正文: {desc_count})")

def enrich_all_bili_covers():
    """Enrich all Bili JSON files that have video lists."""
    print("\n📸 B站封面图补充:")
    bili_dir = f"{BASE}/data/sources/bilibili"
    for fn in sorted(os.listdir(bili_dir)):
        if fn.endswith('.json') and not fn.startswith('video-') and fn != '1-feed.json':
            enrich_bili_covers(fn.replace('.json', ''))

def main():
    covers_only = '--covers-only' in sys.argv

    if covers_only:
        enrich_all_bili_covers()
        return

    print("=" * 60)
    print("增强数据抓取 (完整模式)")
    print("=" * 60)

    enrich_all_bili_covers()

    print("\n✅ 增强抓取完成!")

if __name__ == '__main__':
    import sys
    main()
