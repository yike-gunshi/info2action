"""v21.0 action-revival: 行动点生成每日配额编排层。

在 db(local sqlite)/remote_db(Supabase)两个后端之上做统一入口,
并处理 admin 豁免与未登录兜底。路由层只调这里的两个函数:

  usage_for_request(request)        -> 快照(供 GET /api/user/action-quota)
  try_consume_for_request(request)  -> (allowed, 快照);"发起即计"

配额口径见 .features/action-revival/feature-spec.md §3.3 / §9 Q7-Q8。
"""
from typing import Any

from fastapi import Request

import db
import remote_db
from authz import can_access_all, current_user_id


def _unlimited_snapshot(limit: int) -> dict[str, Any]:
    return {
        'day_cst': db._asr_today_cst(),
        'used': 0,
        'limit': int(limit),
        'remaining': int(limit),
        'over_limit': False,
        'unlimited': True,
        'reset_at': None,
    }


def _blocked_anonymous_snapshot(limit: int) -> dict[str, Any]:
    return {
        'day_cst': db._asr_today_cst(),
        'used': int(limit),
        'limit': int(limit),
        'remaining': 0,
        'over_limit': True,
        'reset_at': None,
    }


def usage_for_request(request: Request) -> dict[str, Any]:
    """只读快照:admin 返回不限额;普通用户返回当日用量。"""
    limit = db.action_gen_daily_limit()
    if can_access_all(request):
        return _unlimited_snapshot(limit)
    user_id = current_user_id(request)
    if not user_id:
        return _blocked_anonymous_snapshot(limit)
    if remote_db.app_state_to_remote():
        return remote_db.get_generation_usage_today_remote(user_id=user_id, limit=limit)
    conn = db.get_conn()
    try:
        return db.get_generation_usage_today(conn, user_id)
    finally:
        conn.close()


def try_consume_for_request(request: Request) -> tuple[bool, dict[str, Any]]:
    """发起即计:admin 直接放行;普通用户原子 +1(超限则拒绝且不计)。"""
    limit = db.action_gen_daily_limit()
    if can_access_all(request):
        return True, _unlimited_snapshot(limit)
    user_id = current_user_id(request)
    if not user_id:
        return False, _blocked_anonymous_snapshot(limit)
    if remote_db.app_state_to_remote():
        return remote_db.try_consume_generation_quota_remote(None, user_id=user_id, limit=limit)
    conn = db.get_conn()
    try:
        return db.try_consume_generation_quota(conn, user_id)
    finally:
        conn.close()
