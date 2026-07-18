#!/usr/bin/env python3
"""订阅配置 v22.0 — 信源注册表种子导入(幂等)。

从现有 config 把名单型信源灌入 sources 表(方案 B 单一真相源):
  - config.json  rss.feeds        → platform=rss,         status=active
  - config.json  reddit.subreddits→ platform=reddit,      status=active
  - github_tracking.json awesome_repos → platform=github_repo, status=active
  - config.json  bilibili.up_list → platform=bilibili_up,  status=not_fetched(v16 后未在抓)
  - data/lingowhale/groups.json    → platform=wechat_mp,   status=active, backend=lingowhale(文件不存在则跳过)

幂等: 按 (platform, source_key) 判断存在与否。
  - 不存在 → INSERT(用种子指定的 status)
  - 已存在 → 只更新 display_name/config_json/updated_at,**保留 status 与 origin**
             (不覆盖 admin 手工改过的状态)

X 注册表账号不在本脚本: 需由管理页配置或显式同步后交给 fetch_x_users。

用法: python3 scripts/seed_sources_registry.py
测试: 先设 db.DB_PATH 指向临时库再 import 调用 seed()。
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, 'src'))

import db  # noqa: E402
import remote_db  # noqa: E402


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _load_json(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def _collect_seeds(base=BASE):
    """Return list of dicts: platform, source_key, display_name, status, config_json."""
    seeds = []
    config = _load_json(os.path.join(base, 'config', 'config.json')) or {}
    gh = _load_json(os.path.join(base, 'config', 'github_tracking.json')) or {}

    # RSS
    for feed in config.get('rss', {}).get('feeds', []):
        url = feed.get('url')
        if not url:
            continue
        seeds.append({
            'platform': 'rss',
            'source_key': url,
            'display_name': feed.get('name') or url,
            'status': 'active',
            'config_json': json.dumps({'slug': feed.get('slug')}, ensure_ascii=False)
            if feed.get('slug') else None,
        })

    # Reddit
    for sub in config.get('reddit', {}).get('subreddits', []):
        if not sub:
            continue
        seeds.append({
            'platform': 'reddit',
            'source_key': sub,
            'display_name': sub,
            'status': 'active',
            'config_json': None,
        })

    # GitHub awesome repos (named-list sources; trending is an algo source, not seeded)
    for repo in gh.get('awesome_repos', []):
        if not repo:
            continue
        seeds.append({
            'platform': 'github_repo',
            'source_key': repo,                      # owner/repo
            'display_name': repo.split('/')[-1],
            'status': 'active',
            'config_json': None,
        })

    # Bilibili up_list — config exists but not crawled since v16 → not_fetched
    for up in config.get('bilibili', {}).get('up_list', []):
        uid = str(up.get('uid') or '').strip()
        if not uid:
            continue
        seeds.append({
            'platform': 'bilibili_up',
            'source_key': uid,
            'display_name': up.get('name') or uid,
            'status': 'not_fetched',
            'config_json': json.dumps({'tags': up.get('tags', [])}, ensure_ascii=False)
            if up.get('tags') else None,
        })

    # WeChat public accounts — from lingowhale groups mirror if present
    groups = _load_json(os.path.join(base, 'data', 'lingowhale', 'groups.json'))
    if groups:
        for ch in _iter_lingowhale_channels(groups):
            cid = ch.get('channel_id')
            if not cid:
                continue
            seeds.append({
                'platform': 'wechat_mp',
                'source_key': cid,
                'display_name': ch.get('name') or cid,
                'status': 'active',
                'config_json': json.dumps({'backend': 'lingowhale'}, ensure_ascii=False),
            })

    return seeds


def _iter_lingowhale_channels(groups):
    """Yield channel dicts from lingowhale groups.json (tolerant of shape)."""
    # groups.json 结构未知/可能变化; 尽量宽容地挖 channel_id + name。
    def walk(obj):
        if isinstance(obj, dict):
            if obj.get('channel_id'):
                yield obj
            for v in obj.values():
                yield from walk(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from walk(v)
    yield from walk(groups)


def _seed_into(conn, *, remote):
    seeds = _collect_seeds()
    summary = {}
    lingowhale_present = os.path.exists(
        os.path.join(BASE, 'data', 'lingowhale', 'groups.json'))

    for s in seeds:
        plat = s['platform']
        summary.setdefault(plat, {'inserted': 0, 'updated': 0})
        now = _now()
        if remote:
            action = remote_db.upsert_source_registry_remote(
                conn,
                platform=plat,
                source_key=s['source_key'],
                display_name=s['display_name'],
                status=s['status'],
                config_json=s['config_json'],
                origin='seed_import',
                now=now,
            )
        else:
            row = conn.execute(
                "SELECT id FROM sources WHERE platform=? AND source_key=?",
                (plat, s['source_key']),
            ).fetchone()
            if row:
                # 已存在: 更新展示信息, 保留 status/origin(不覆盖 admin 改动)
                conn.execute(
                    "UPDATE sources SET display_name=?, config_json=?, updated_at=? "
                    "WHERE platform=? AND source_key=?",
                    (s['display_name'], s['config_json'], now,
                     plat, s['source_key']),
                )
                action = 'updated'
            else:
                conn.execute(
                    "INSERT INTO sources(platform, source_key, display_name, status, "
                    "config_json, origin, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (plat, s['source_key'], s['display_name'], s['status'],
                     s['config_json'], 'seed_import', now, now),
                )
                action = 'inserted'
        summary[plat][action] += 1

    conn.commit()
    summary['_lingowhale_groups_present'] = lingowhale_present
    return summary


def seed(conn=None):
    """Idempotent seed. Returns summary dict {platform: {inserted, updated}} + skipped notes."""
    if conn is not None:
        return _seed_into(conn, remote=False)
    if remote_db.fetch_write_to_remote():
        with remote_db.connect() as remote_conn:
            return _seed_into(remote_conn, remote=True)

    local_conn = db.get_conn()
    try:
        return _seed_into(local_conn, remote=False)
    finally:
        local_conn.close()


def main():
    summary = seed()
    print("=== 信源注册表种子导入完成 ===")
    total_ins = total_upd = 0
    for plat in ('rss', 'reddit', 'github_repo', 'bilibili_up', 'wechat_mp'):
        s = summary.get(plat)
        if not s:
            continue
        print(f"  {plat:14} 新增 {s['inserted']:3}  更新 {s['updated']:3}")
        total_ins += s['inserted']
        total_upd += s['updated']
    print(f"  {'合计':14} 新增 {total_ins:3}  更新 {total_upd:3}")
    if not summary.get('_lingowhale_groups_present'):
        print("  注: data/lingowhale/groups.json 不存在 → 公众号(wechat_mp)本轮跳过,"
              "待有语鲸频道数据后再导入。")
    print("  注: X 注册表账号由管理页配置，fetch_x_users 只消费注册表。")


if __name__ == '__main__':
    main()
