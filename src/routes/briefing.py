"""Briefing endpoints: get and generate daily briefings."""

import os
import subprocess
import sys
import threading

from fastapi import APIRouter, Query, Request

import db
import remote_db
from authz import require_admin
from deps import BASE

router = APIRouter()


@router.get('/api/briefing')
def get_briefing(date: str = Query(None)):
    if remote_db.app_state_to_remote():
        return {
            'briefing': remote_db.get_briefing_remote(date),
            'dates': remote_db.list_briefing_dates_remote(),
        }
    conn = db.get_conn()
    briefing = db.get_briefing(conn, date)
    dates = db.list_briefing_dates(conn)
    conn.close()
    return {'briefing': briefing, 'dates': dates}


@router.post('/api/briefing/generate')
async def post_briefing_generate(request: Request):
    err = require_admin(request)
    if err:
        return err

    def _bg_generate_briefing():
        try:
            subprocess.run([sys.executable or 'python3', os.path.join(BASE, 'src', 'generate_briefing.py')],
                cwd=BASE, timeout=120, stderr=subprocess.STDOUT)
        except Exception as e:
            print(f"Briefing generation error: {e}")
    threading.Thread(target=_bg_generate_briefing, daemon=True).start()
    return {'ok': True, 'msg': 'Briefing generation started'}
