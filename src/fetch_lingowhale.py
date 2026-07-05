#!/usr/bin/env python3
"""Fetch subscription feed from Lingowhale API.
Usage: python3 fetch_lingowhale.py
Outputs: data/lingowhale/feed.json, data/lingowhale/groups.json
"""
import json, os, re, ssl, sys, time, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

_SSL_CTX = ssl.create_default_context()

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get('INFO2ACTION_DATA_DIR') or os.path.join(BASE, 'data')
OUT_DIR = os.path.join(DATA_DIR, 'lingowhale')

# Load config
with open(os.path.join(BASE, 'config', 'config.json')) as f:
    CONFIG = json.load(f)

LW = CONFIG.get('lingowhale', {})
if not LW.get('enabled', False):
    print("  ⚠️  公众号未启用 (lingowhale.enabled=false)")
    sys.exit(0)

def _cred(env_key, config_key, default=''):
    """BF-0419-10: 凭证优先读 env(.env 已 git-ignored),fallback 旧 config.json 字段。"""
    return os.environ.get(env_key, '') or LW.get(config_key, default)

API_BASE = 'https://api-public.lingowhale.com'
API_INTERNAL = 'https://api.lingowhale.com'
GROUP_ENDPOINTS = (
    '/api/lingowhale/v1/user_subscribe/list',
    '/api/feed/v1/user_subscribe/list',
)
FEED_ENDPOINTS = (
    '/api/lingowhale/v1/feed/subscription',
    '/api/feed/v2/feed/subscription',
)
DETAIL_ENDPOINTS = (
    '/api/lingowhale/v1/resource/get',
    '/api/feed/v1/resource/get',
)
HEADERS = {
    'Content-Type': 'application/json',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN,zh;q=0.9',
    'Auth-Token': _cred('LINGOWHALE_AUTH_TOKEN', 'auth_token'),
    'Access-Token': _cred('LINGOWHALE_ACCESS_TOKEN', 'access_token'),
    'U-Id': _cred('LINGOWHALE_UID', 'uid'),
    'B-Id': _cred('LINGOWHALE_BID', 'bid'),
    'Guest-Id': _cred('LINGOWHALE_GUEST_ID', 'guest_id'),
    'Imei': 'fingerPrint-web',
    'Origin': 'https://lingowhale.com',
    'Referer': 'https://lingowhale.com/',
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36',
}

MAX_ITEMS = LW.get('max_items', 3000)
ENRICH_DAYS = LW.get('enrich_days', 3)  # Only enrich entries within this many days
FETCH_LOOKBACK_HOURS = LW.get('fetch_lookback_hours', 48)  # Stop paging once oldest entry is older than this
MAX_PAGES = LW.get('max_pages', 20)  # Hard cap to prevent runaway pagination
DEFAULT_PRIORITY_GROUPS = ['每日查看']
CHANNEL_TIMEOUT_SEC = int(os.environ.get('INFO2ACTION_LINGOWHALE_CHANNEL_TIMEOUT_SEC') or LW.get('channel_timeout_sec', 8))


def _post_json(path, payload, timeout=30):
    url = f'{API_BASE}{path}'
    body = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=body, headers=HEADERS, method='POST')
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _api_data(data):
    return (data.get('data') or {}) if isinstance(data, dict) else {}


def _entry_channel(entry):
    channel = entry.get('channel') or {}
    return channel if isinstance(channel, dict) else {}


def _normalize_entry(entry):
    """Normalize current Lingowhale v1 feed fields to the ingest contract."""
    info_source = entry.get('info_source')
    if not isinstance(info_source, dict):
        info_source = {}
        entry['info_source'] = info_source
    channel = _entry_channel(entry)
    channel_name = channel.get('name') or ''
    if channel_name and not info_source.get('info_source_name'):
        info_source['info_source_name'] = channel_name.removesuffix('-公众号')
    if channel.get('surface_url') and not entry.get('surface_url'):
        entry['surface_url'] = channel.get('surface_url')
    return entry


def _priority_group_names():
    raw = os.environ.get('INFO2ACTION_LINGOWHALE_PRIORITY_GROUPS')
    if raw is None:
        raw = LW.get('priority_groups')
    if isinstance(raw, list):
        values = raw
    elif isinstance(raw, str) and raw.strip():
        values = [v.strip() for v in raw.split(',')]
    else:
        values = DEFAULT_PRIORITY_GROUPS
    return [v for v in values if v]


def _priority_channel_ids():
    raw = os.environ.get('INFO2ACTION_LINGOWHALE_PRIORITY_CHANNEL_IDS') or LW.get('priority_channel_ids') or ''
    if isinstance(raw, list):
        values = raw
    elif isinstance(raw, str) and raw.strip():
        values = [v.strip() for v in raw.split(',')]
    else:
        values = []
    return [v for v in values if v]


def _channel_ids_from_groups(groups_info, group_names=None):
    seen = set()
    channel_ids = []
    allowed = set(group_names or [])
    for group in groups_info or []:
        if allowed and group.get('name') not in allowed:
            continue
        for channel in group.get('channels', []) or []:
            cid = channel.get('channel_id')
            if cid and cid not in ('all', 'topic') and cid not in seen:
                seen.add(cid)
                channel_ids.append(cid)
    return channel_ids


def _prioritize_channel_ids(channel_ids, priority_ids):
    seen = set()
    ordered = []
    for cid in list(priority_ids or []) + list(channel_ids or []):
        if cid and cid not in seen:
            seen.add(cid)
            ordered.append(cid)
    return ordered


# ── Groups ──────────────────────────────────────────────────────────────

def _load_groups_fallback():
    """BF-0419-8: 当 fetch_groups 失败时,从 groups.json 旧快照恢复 channel_to_group。
    返回 ({}, []) 如果快照不存在/为空。"""
    fpath = os.path.join(OUT_DIR, 'groups.json')
    try:
        with open(fpath) as f:
            old = json.load(f)
    except Exception:
        print(f"  ℹ️  无 groups.json 旧快照可 fallback")
        return {}, []
    if not old:
        print(f"  ℹ️  groups.json 旧快照为空,无可用 fallback")
        return {}, []
    ch_map = {}
    for g in old:
        for ch in g.get('channels', []):
            cid = ch.get('channel_id')
            if cid:
                ch_map[cid] = g.get('name', '')
    print(f"  ↩️  fallback 到旧 groups.json: {len(old)} groups, {len(ch_map)} channels")
    return ch_map, old


def _sync_groups_metadata_remote(groups_info):
    """Best-effort sync so worktrees do not depend on a local groups.json copy."""
    try:
        import remote_db
        if not (
            remote_db.feed_read_from_remote()
            or remote_db.app_state_to_remote()
            or remote_db.remote_authority_enabled()
        ):
            return
        remote_db.set_lingowhale_groups_metadata_remote(groups_info)
        print(f"  ☁️  分组 metadata 已同步远程 settings: {len(groups_info)} groups")
    except Exception as e:
        print(f"  ⚠️  分组 metadata 远程同步失败(不影响本次抓取): {e}")


def fetch_groups():
    """Fetch subscription groups → channel_id→group_name mapping + groups.json.

    BF-0419-8: 增加 API code 检查 + 失败时不覆盖 groups.json + fallback 旧快照。
    历史 bug: 缺 code 检查时 API 返回 code=10010 token failure 被静默吞掉,
    items 全打"未分组"且每次失败覆盖 groups.json 为 [] 洗掉历史快照。
    """
    data = None
    last_error = ''
    for endpoint in GROUP_ENDPOINTS:
        try:
            data = _post_json(endpoint, {}, timeout=30)
        except Exception as e:
            last_error = f"{endpoint}: {e}"
            print(f"  ❌ 获取分组失败 (网络/解析): {last_error}")
            continue

        code = data.get('code', -1)
        if code == 0:
            break
        msg = data.get('msg', '')
        last_error = f"{endpoint}: code={code}, msg={msg}"
        print(f"  ❌ 获取分组失败: {last_error}")
        if code == 10010 or 'token' in msg.lower():
            print(f"     💡 token 失效,请去公众号网页/App 重新登录,刷新 .env 的 LINGOWHALE_AUTH_TOKEN / LINGOWHALE_ACCESS_TOKEN(取值步骤见 docs/配置指南.md)")
    else:
        print(f"  ❌ 获取分组失败, fallback 旧快照: {last_error}")
        return _load_groups_fallback()

    subs = _api_data(data).get('user_subscribes') or []
    channel_to_group = {}  # channel_id → group_name
    groups_info = []       # [{name, group_id, channels: [{channel_id, name}]}]

    for s in subs:
        if 'subscription_group' in s:
            g = s['subscription_group']
            group_name = g.get('name', '')
            group_id = g.get('group_id', '')
            channels = g.get('channels', [])
            ch_list = []
            for ch in channels:
                cid = ch.get('channel_id', '')
                cname = ch.get('name', '')
                if cid:
                    channel_to_group[cid] = group_name
                    ch_list.append({'channel_id': cid, 'name': cname})
            groups_info.append({
                'name': group_name,
                'group_id': group_id,
                'channels': ch_list,
            })
        elif 'subscription_channel' in s:
            ch = s['subscription_channel']
            cid = ch.get('channel_id', '')
            cname = ch.get('name', '')
            if cid and cid not in ('all', 'topic'):
                channel_to_group[cid] = '独立频道'
                groups_info.append({
                    'name': cname,
                    'group_id': cid,
                    'channels': [{'channel_id': cid, 'name': cname}],
                    'is_standalone': True,
                })

    # BF-0419-8: 解析为空(API 200 但响应结构变了)也视为失败,不覆盖快照
    if not groups_info:
        print(f"  ❌ API 返回 code=0 但解析无分组(响应结构可能改版),保留旧 groups.json 不覆盖")
        return _load_groups_fallback()

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, 'groups.json'), 'w') as f:
        json.dump(groups_info, f, ensure_ascii=False, indent=2)
    _sync_groups_metadata_remote(groups_info)

    print(f"  📂 分组: {len(groups_info)} groups, {len(channel_to_group)} channels")
    return channel_to_group, groups_info


# ── Feed ────────────────────────────────────────────────────────────────

def _fetch_feed_page(endpoint, channel_ids, cursor, timeout=30):
    data = _post_json(endpoint, {
        'channel_ids': channel_ids,
        'sort_type': 0,
        'cursor': cursor,
    }, timeout=timeout)
    code = data.get('code', -1)
    if code != 0:
        raise RuntimeError(f"code={code}, msg={data.get('msg', '')}")
    return _api_data(data)


INCREMENTAL_OVERLAP_SEC = int(LW.get('incremental_overlap_sec', 1800))  # 回填/乱序兜底重叠


def _lingowhale_incremental_enabled():
    """v20.0 增量抓取开关(默认关,设 env 才启用,便于灰度/回退)。"""
    raw = os.environ.get('INFO2ACTION_LINGOWHALE_INCREMENTAL')
    return str(raw).strip().lower() in {'1', 'true', 'yes', 'on'} if raw is not None else False


def _parse_watermark_to_ts(raw):
    """把 items.published_at(ISO 字符串 / None / 垃圾)解析成 unix ts;失败 → None(fail-safe 回退全窗口)。"""
    if not raw:
        return None
    try:
        s = str(raw).strip()
        if not s:
            return None
        return datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()
    except (ValueError, OSError, TypeError):
        return None


def _lingowhale_watermark_ts():
    """已入库语鲸内容的最新 published_at(unix ts);无数据/出错 → None(冷启动/全窗口回退)。"""
    try:
        import remote_db
        if remote_db.fetch_write_to_remote():
            schema = remote_db.remote_schema()
            with remote_db.connect() as conn:
                row = conn.execute(
                    f"SELECT max(published_at) AS mx FROM {schema}.items WHERE platform = 'lingowhale'"
                ).fetchone()
        else:
            import db
            row = db.get_conn().execute(
                "SELECT max(published_at) AS mx FROM items WHERE platform = 'lingowhale'"
            ).fetchone()
        if row is None:
            return None
        raw = row['mx'] if isinstance(row, dict) else row[0]
        return _parse_watermark_to_ts(raw)
    except Exception as exc:  # noqa: BLE001 — 读不到水位线时退全窗口,不阻断抓取
        print(f"  ⚠️  语鲸水位线读取失败,回退全窗口抓取: {exc}")
        return None


def _fetch_subscription_feed_from_endpoint(endpoint, channel_ids, label, timeout=30,
                                           since_ts=None):
    """Fetch subscription feed with cursor pagination.

    Early-stop conditions:
    - ``since_ts`` 提供时(增量抓取,v20.0):翻到 pub_time < 水位线 即停,只保留更新的 entry。
      调用方可传 ``watermark - overlap`` 留一点重叠兜底回填(入库去重)。
    - ``since_ts`` 为 None 时(冷启动/兜底):按 FETCH_LOOKBACK_HOURS 时间窗早停(原行为)。
    - page > MAX_PAGES (hard cap)
    - has_more=false / empty cursor / empty page
    - len(all_entries) >= MAX_ITEMS
    """
    all_entries = []
    cursor = ''
    page = 0
    incremental = since_ts is not None
    cutoff_ts = since_ts if incremental else time.time() - FETCH_LOOKBACK_HOURS * 3600
    stop_reason = ''

    while len(all_entries) < MAX_ITEMS:
        if page >= MAX_PAGES:
            stop_reason = f'max_pages={MAX_PAGES} 兜底触发'
            break

        page += 1
        try:
            result = _fetch_feed_page(endpoint, channel_ids, cursor, timeout=timeout)
        except Exception as e:
            print(f"  ❌ 公众号 API 请求失败 ({label}, page {page}): {e}")
            break

        entries = [_normalize_entry(e) for e in (result.get('feed_list') or [])]
        if not entries:
            break

        # Date early-stop: keep only entries newer than cutoff on this page.
        # Entries are returned newest-first; once we see one older than cutoff, the rest are too.
        fresh = [e for e in entries if e.get('pub_time', 0) >= cutoff_ts]
        reached_old = len(fresh) < len(entries)
        all_entries.extend(fresh)

        # Print progress every 20 pages
        if page % 20 == 0 or page <= 2:
            oldest = ''
            if entries:
                pt = entries[-1].get('pub_time', 0)
                if pt:
                    oldest = datetime.fromtimestamp(pt, tz=timezone.utc).strftime('%m-%d')
            print(f"  📄 {label} Page {page}: {len(all_entries)} entries (oldest: {oldest})")
            sys.stdout.flush()

        if reached_old:
            dropped = len(entries) - len(fresh)
            if incremental:
                stop_reason = f'watermark early-stop (since_ts={since_ts}),本页丢弃 {dropped} 条已见 entry'
            else:
                stop_reason = f'lookback={FETCH_LOOKBACK_HOURS}h 早停,本页丢弃 {dropped} 条旧 entry'
            break

        has_more = result.get('has_more', False)
        cursor = result.get('cursor', '')
        if not has_more or not cursor:
            stop_reason = 'has_more=false 或 cursor 空'
            break

        time.sleep(0.3)

    if not stop_reason:
        stop_reason = f'达到 max_items={MAX_ITEMS}' if len(all_entries) >= MAX_ITEMS else '未知'

    return all_entries[:MAX_ITEMS], page, stop_reason


def fetch_subscription_feed(groups_info=None, since_ts=None):
    """Fetch Lingowhale subscription feed.

    Lingowhale's current web app reads `/api/lingowhale/v1/feed/subscription`.
    Its `channel_ids=["all"]` path can be stale/slow, so we fetch configured
    priority groups from the current endpoint and keep the legacy all-feed as the
    broad coverage leg.
    """
    priority_groups = _priority_group_names()
    channel_ids = _prioritize_channel_ids(
        _channel_ids_from_groups(groups_info, priority_groups),
        _priority_channel_ids(),
    )
    all_entries = []
    seen = set()
    total_pages = 0
    endpoint = FEED_ENDPOINTS[0]

    if channel_ids:
        print(f"  📡 当前接口重点分组抓取: {','.join(priority_groups)} ({len(channel_ids)} channels)")
        for idx, channel_id in enumerate(channel_ids, start=1):
            entries, pages, stop_reason = _fetch_subscription_feed_from_endpoint(
                endpoint,
                [channel_id],
                f"channel {idx}/{len(channel_ids)}",
                timeout=CHANNEL_TIMEOUT_SEC,
                since_ts=since_ts,
            )
            total_pages += pages
            for entry in entries:
                entry_id = entry.get('entry_id')
                if entry_id and entry_id not in seen:
                    seen.add(entry_id)
                    all_entries.append(entry)
            if entries:
                print(f"    channel {idx}/{len(channel_ids)}: {len(entries)} fresh ({stop_reason})")
            time.sleep(0.2)

    legacy_entries, pages, legacy_stop_reason = _fetch_subscription_feed_from_endpoint(
        FEED_ENDPOINTS[1],
        ['all'],
        'legacy all',
        since_ts=since_ts,
    )
    total_pages += pages
    for entry in legacy_entries:
        entry_id = entry.get('entry_id')
        if entry_id and entry_id not in seen:
            seen.add(entry_id)
            all_entries.append(entry)
    stop_reason = (
        f'priority_groups={len(channel_ids)} channels + legacy all ({legacy_stop_reason})'
    )

    result = sorted(
        all_entries,
        key=lambda e: e.get('pub_time') or 0,
        reverse=True,
    )[:MAX_ITEMS]
    print(f"  📥 Feed: {len(result)} entries in {total_pages} pages ({stop_reason})")
    return result


# ── Enrich ──────────────────────────────────────────────────────────────

_DETAIL_FRAGMENT_MIN_LINES = 8
_DETAIL_FRAGMENT_MIN_SHORT_LINES = 4
_DETAIL_FRAGMENT_SHORT_LINE_RATIO = 0.18
_DETAIL_RATING_LABELS = {'夯', '夯爆了', '顶级', 'NPC', '拉➡️NPC', '人上人'}
_ASCII_WORD_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_.+-]*$')
_CLOSING_PUNCT = '，。！？；：、,.!?;:%)]}）】』」》'
_OPENING_PUNCT = '（【「『《([{'


def _is_detail_structural_line(line):
    if re.fullmatch(r'【[^】]{2,60}】', line):
        return True
    if re.fullmatch(r'\d{1,2}[、.．][^。！？\n]{1,50}[：:]?', line):
        return True
    return False


def _looks_like_fragmented_detail_text(lines):
    nonblank = [line.strip() for line in lines if line.strip()]
    if len(nonblank) < _DETAIL_FRAGMENT_MIN_LINES:
        tiny = sum(1 for line in nonblank if len(line) <= 2)
        return len(nonblank) >= 5 and tiny >= 3

    short = sum(1 for line in nonblank if len(line) <= 4)
    tiny = sum(1 for line in nonblank if len(line) <= 2)
    return short >= _DETAIL_FRAGMENT_MIN_SHORT_LINES and (
        short / len(nonblank) >= _DETAIL_FRAGMENT_SHORT_LINE_RATIO or tiny >= 3
    )


def _append_detail_fragment(text, fragment):
    if not text:
        return fragment
    if not fragment:
        return text
    if fragment[0] in _CLOSING_PUNCT or text[-1] in _OPENING_PUNCT:
        return text + fragment
    ascii_tail = re.search(r'[A-Za-z0-9]+$', text)
    if ascii_tail and re.match(r'[A-Za-z0-9]', fragment):
        if fragment[0].islower() or len(ascii_tail.group(0)) == 1:
            return text + fragment
        return text + ' ' + fragment
    if text in _DETAIL_RATING_LABELS:
        return text + ' ' + fragment
    if _ASCII_WORD_RE.fullmatch(fragment):
        return text + ' ' + fragment
    if re.search(r'[A-Za-z]$', text) and re.match(r'[\u4e00-\u9fff]', fragment):
        return text + ' ' + fragment
    return text + fragment


def _join_detail_fragments(parts):
    text = ''
    for part in parts:
        text = _append_detail_fragment(text, part)
    return text.strip()


def _normalize_detail_content_text(content):
    if not isinstance(content, str):
        return ''

    text = content.replace('\r\n', '\n').replace('\r', '\n').strip()
    raw_lines = text.split('\n')
    if not _looks_like_fragmented_detail_text(raw_lines):
        return text

    cleaned = []
    buffer = []

    def flush_buffer():
        if buffer:
            cleaned.append(_join_detail_fragments(buffer))
            buffer.clear()

    for raw_line in raw_lines:
        line = raw_line.strip()
        if not line:
            flush_buffer()
            if cleaned and cleaned[-1] != '':
                cleaned.append('')
            continue
        if _is_detail_structural_line(line):
            flush_buffer()
            cleaned.append(line)
        else:
            buffer.append(line)

    flush_buffer()
    deduped = []
    for line in cleaned:
        if line == '' and (not deduped or deduped[-1] == ''):
            continue
        deduped.append(line)
    return '\n'.join(deduped).strip()


def _fetch_detail(entry):
    """Fetch detail for a single entry. Returns (entry, success)."""
    entry_id = entry.get('entry_id', '')
    if not entry_id:
        return entry, False
    body = json.dumps({'entry_id': entry_id, 'need_content': True}).encode('utf-8')
    for endpoint in DETAIL_ENDPOINTS:
        url = f'{API_BASE}{endpoint}'
        req = urllib.request.Request(url, data=body, headers=HEADERS, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            resource = (_api_data(data).get('resource') or _api_data(data) or {})
            if resource.get('title') and not entry.get('title'):
                entry['title'] = resource['title']
            if resource.get('content'):
                entry['content'] = _normalize_detail_content_text(resource['content'])
            if resource.get('abstract'):
                entry['abstract'] = resource['abstract']
            if resource.get('viewpoint'):
                entry['viewpoint'] = resource['viewpoint']
            return _normalize_entry(entry), True
        except Exception:
            continue
    return entry, False


def enrich_entries(entries):
    """Fetch detail for every entry so content comes from the article endpoint."""
    to_enrich = list(entries)

    if not to_enrich:
        print(f"  📖 详情补全: 无 entries")
        return entries

    enriched = 0
    WORKERS = 10  # parallel requests
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(_fetch_detail, e): e for e in to_enrich}
        done = 0
        for f in as_completed(futures):
            original = futures[f]
            entry, ok = f.result()
            if ok:
                if entry is not original:
                    original.update(entry)
                enriched += 1
            done += 1
            if done % 100 == 0:
                print(f"    详情: {done}/{len(to_enrich)}")

    print(f"  📖 详情补全: {enriched}/{len(to_enrich)} entries enriched")
    return entries


def enrich_wechat_urls(entries):
    """Use wechat-article-exporter API to get original WeChat article URLs."""
    wx_cfg = CONFIG.get('wechat_exporter', {})
    # oss-release F3c: env/.env 优先，config.json 只留空模板
    from env_utils import load_project_env
    auth_key = (
        os.environ.get('WECHAT_EXPORTER_AUTH_KEY')
        or load_project_env(BASE).get('WECHAT_EXPORTER_AUTH_KEY')
        or wx_cfg.get('auth_key', '')
        or ''
    ).strip()
    if not wx_cfg.get('enabled') or not auth_key:
        print("  ⚠️  wechat_exporter 未启用，跳过原文URL补全")
        return entries

    api_base = wx_cfg['api_base'].rstrip('/')
    wx_headers = {'X-Auth-Key': auth_key, 'User-Agent': 'info2action/1.0'}

    # Group entries by WeChat source name
    wechat_entries = {}
    for e in entries:
        root = (e.get('info_source') or {}).get('info_source_root', '')
        if 'mp.weixin.qq.com' not in root:
            continue
        name = (e.get('info_source') or {}).get('info_source_name', '')
        if name:
            wechat_entries.setdefault(name, []).append(e)

    if not wechat_entries:
        return entries

    print(f"  🔗 补全微信原文URL: {len(wechat_entries)} 个公众号...")
    matched = 0
    failed_accounts = []

    for account_name, account_entries in wechat_entries.items():
        # Step 1: Search account → get fakeid
        try:
            search_url = f'{api_base}/api/public/v1/account?keyword={urllib.parse.quote(account_name)}&size=5'
            req = urllib.request.Request(search_url, headers=wx_headers)
            with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
                data = json.loads(resp.read().decode('utf-8'))
        except Exception as e:
            print(f"    ❌ 搜索公众号失败 [{account_name}]: {e}")
            failed_accounts.append(account_name)
            continue

        account_list = data.get('list', [])
        fakeid = None
        for acc in account_list:
            if acc.get('nickname') == account_name:
                fakeid = acc['fakeid']
                break
        if not fakeid and account_list:
            fakeid = account_list[0].get('fakeid')

        if not fakeid:
            failed_accounts.append(account_name)
            continue

        time.sleep(1)

        # Step 2: Get article list → match by title
        try:
            art_url = f'{api_base}/api/public/v1/article?fakeid={fakeid}&begin=0&size=50'
            req = urllib.request.Request(art_url, headers=wx_headers)
            with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
                data = json.loads(resp.read().decode('utf-8'))
        except Exception as e:
            failed_accounts.append(account_name)
            continue

        articles = data.get('articles', [])
        title_links = {}
        for art in articles:
            t = art.get('title', '').strip()
            link = art.get('link', '')
            if t and link:
                title_links[t] = link

        for e in account_entries:
            entry_title = e.get('title', '').strip()
            if entry_title in title_links:
                e['wechat_url'] = title_links[entry_title]
                matched += 1

        time.sleep(1)

    print(f"  🔗 微信原文URL: {matched} matched, {len(failed_accounts)} failed")
    return entries


# ── Annotate groups ─────────────────────────────────────────────────────

def annotate_groups(entries, channel_to_group):
    """Add group_name to each entry based on channel_id→group mapping."""
    annotated = 0
    for e in entries:
        ch = e.get('channel', {})
        cid = ch.get('channel_id', '')
        if cid and cid in channel_to_group:
            e['group_name'] = channel_to_group[cid]
            annotated += 1
        else:
            e['group_name'] = '未分组'
    print(f"  🏷️  分组标注: {annotated}/{len(entries)} entries")
    return entries


# ── Main ────────────────────────────────────────────────────────────────

def _save_feed(entries, label=''):
    """Save feed.json (called after each major step for resilience)."""
    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, 'feed.json')
    with open(out_path, 'w') as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    if label:
        print(f"  💾 已保存 {len(entries)} entries ({label})")


def main():
    if not HEADERS.get('Auth-Token'):
        print("  ⚠️  公众号 auth_token 未配置(检查 .env LINGOWHALE_AUTH_TOKEN 或 source .env 后再跑),跳过")
        return

    t0 = time.time()
    print("🐋 公众号订阅 Feed...")

    # 1. Fetch groups
    channel_to_group, groups_info = fetch_groups()

    # 2. Fetch feed (pagination) — v20.0 增量:开关开时按已入库水位线只拉新增
    since_ts = None
    if _lingowhale_incremental_enabled():
        watermark = _lingowhale_watermark_ts()
        if watermark is not None:
            since_ts = watermark - INCREMENTAL_OVERLAP_SEC
            wm_iso = datetime.fromtimestamp(watermark, tz=timezone.utc).isoformat()
            print(f"  🔖 增量抓取: 水位线 {wm_iso} (overlap {INCREMENTAL_OVERLAP_SEC}s)")
        else:
            print("  🆕 增量已开但无水位线(冷启动): 全窗口抓取")
    entries = fetch_subscription_feed(groups_info, since_ts=since_ts)
    if not entries:
        return

    # 3. Annotate groups
    entries = annotate_groups(entries, channel_to_group)
    _save_feed(entries, '翻页+分组完成')  # ← 第一次保存，确保数据不丢

    # 4. Enrich recent entries with detail API
    entries = enrich_entries(entries)
    _save_feed(entries, '详情补全完成')  # ← 第二次保存

    # 5. Enrich WeChat URLs (best-effort, 失败不影响整体)
    try:
        now = time.time()
        cutoff = now - ENRICH_DAYS * 86400
        recent_wx = [e for e in entries if e.get('pub_time', 0) >= cutoff]
        enrich_wechat_urls(recent_wx)
        _save_feed(entries, 'URL补全完成')  # ← 第三次保存
    except Exception as e:
        print(f"  ⚠️  微信URL补全异常 (已跳过): {e}")

    elapsed = time.time() - t0
    print(f"  ✅ 公众号: {len(entries)} entries, 耗时 {elapsed:.0f}s")


if __name__ == '__main__':
    if '--enrich-only' in sys.argv:
        # Load existing feed.json, run enrichment on un-enriched items, save, exit
        feed_path = os.path.join(OUT_DIR, 'feed.json')
        if not os.path.exists(feed_path):
            print("  ❌ data/lingowhale/feed.json 不存在，请先运行完整抓取")
            sys.exit(1)
        with open(feed_path) as f:
            entries = json.load(f)
        print(f"🐋 公众号 --enrich-only: 加载 {len(entries)} entries...")
        entries = enrich_entries(entries)
        _save_feed(entries, 'enrich-only 完成')
        print(f"  ✅ enrich-only 完成: {len(entries)} entries")
    else:
        main()
