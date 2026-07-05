"""Config, token, topics, and classification endpoints."""

from copy import deepcopy
import json
import os
import re
from typing import Any

from fastapi import APIRouter, Request

import db
import remote_db
from authz import require_admin
from deps import BASE

router = APIRouter()


def load_json(path):
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except Exception:
        return None


@router.get('/api/config')
def get_config(request: Request):
    error = require_admin(request)
    if error:
        return error
    return load_json(os.path.join(BASE, 'config', 'config.json')) or {}


@router.post('/api/config')
async def post_config(request: Request):
    error = require_admin(request)
    if error:
        return error
    body = await request.json()
    with open(os.path.join(BASE, 'config', 'config.json'), 'w') as f:
        json.dump(body, f, indent=2, ensure_ascii=False)
    return {'ok': True}


@router.get('/api/token')
def get_token(request: Request):
    error = require_admin(request)
    if error:
        return error
    token_path = os.path.join(BASE, '.api_token')
    token = ''
    if os.path.exists(token_path):
        with open(token_path) as f:
            token = f.read().strip()
    return {'token': token}


@router.get('/api/topics')
def get_topics():
    return load_json(os.path.join(BASE, 'config', 'topics.json')) or {}


@router.get('/api/classification')
def get_classification():
    return load_json(os.path.join(BASE, 'config', 'classification.json')) or {}


def _normalize_lingowhale_groups(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    groups: list[dict[str, Any]] = []
    for raw_group in value:
        if not isinstance(raw_group, dict):
            continue
        name = str(raw_group.get('name') or '').strip()
        if not name:
            continue
        channels = []
        for raw_channel in raw_group.get('channels') or []:
            if not isinstance(raw_channel, dict):
                continue
            channel_id = str(raw_channel.get('channel_id') or '').strip()
            channel_name = str(raw_channel.get('name') or '').strip()
            if channel_id or channel_name:
                channels.append({
                    'channel_id': channel_id,
                    'name': channel_name,
                })
        group = {
            'name': name,
            'group_id': str(raw_group.get('group_id') or '').strip(),
            'channels': channels,
        }
        if raw_group.get('is_standalone'):
            group['is_standalone'] = True
        groups.append(group)
    return groups


def _load_local_lingowhale_groups() -> list[dict[str, Any]]:
    groups_path = os.path.join(BASE, 'data', 'lingowhale', 'groups.json')
    return _normalize_lingowhale_groups(load_json(groups_path))


def _load_lingowhale_groups_metadata() -> tuple[list[dict[str, Any]], str]:
    """Remote settings are the authority; local groups.json is a dev fallback."""
    should_try_remote = remote_db.feed_read_from_remote() or remote_db.app_state_to_remote()
    if should_try_remote:
        try:
            remote_groups = _normalize_lingowhale_groups(
                remote_db.get_lingowhale_groups_metadata_remote()
            )
            if remote_groups:
                return remote_groups, 'remote_settings'
        except remote_db.RemoteDBError:
            pass

    local_groups = _load_local_lingowhale_groups()
    if local_groups:
        return local_groups, 'local_file'
    return [], 'empty'


def _lingowhale_channel_map(groups_data: list[dict[str, Any]]) -> dict[str, str]:
    ch_map = {}
    for g in groups_data:
        gname = g.get('name', '')
        for ch in g.get('channels', []):
            cname = ch.get('name', '')
            if cname:
                ch_map[cname] = gname
                bare = re.sub(r'[-\s]*(公众号|播客|RSS|视频号|网站|微博)$', '', cname).strip()
                if bare and bare != cname:
                    ch_map[bare] = gname
    return ch_map


@router.get('/api/lingowhale/groups')
def get_lingowhale_groups():
    groups_data, metadata_backend = _load_lingowhale_groups_metadata()
    groups_data = deepcopy(groups_data)
    ch_map = _lingowhale_channel_map(groups_data)

    # BF-0419-11 续: 给每个 group 带上 DB 里实际 item 数 + 暴露 ungrouped 桶
    # 用户视角:pill 数字应该是"点这个能看到几篇",不是"组里几个频道"
    if remote_db.feed_read_from_remote() or remote_db.app_state_to_remote():
        counts = remote_db.lingowhale_group_counts_remote()
    else:
        conn = db.get_conn()
        try:
            rows = conn.execute(
                "SELECT COALESCE(json_extract(detail_json,'$.group'),'') AS g, COUNT(*) "
                "FROM items WHERE platform='lingowhale' GROUP BY g"
            ).fetchall()
        finally:
            conn.close()
        counts = {r['g']: r['COUNT(*)'] for r in rows}
    # BF-0419-11: 把"独立频道"(historical literal "独立频道" stored by old fetch code)
    # 也合并到 ungrouped 桶,避免 109 条对不上账(后续 N-LINGOWHALE-INDIE 立项时再彻底治)
    ungrouped_count = counts.get('未分组', 0) + counts.get('', 0) + counts.get('独立频道', 0)

    for g in groups_data:
        gname = g.get('name', '')
        g['item_count'] = counts.get(gname, 0)

    return {
        'groups': groups_data,
        'channel_map': ch_map,
        'ungrouped_count': ungrouped_count,
        'metadata_backend': metadata_backend,
    }
