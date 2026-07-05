"""User context and project directory settings endpoints."""

import json
import os

from fastapi import APIRouter, Request

import execute_action
from authz import require_admin

router = APIRouter()


@router.get('/api/user-context')
def get_user_context(request: Request):
    err = require_admin(request)
    if err:
        return err
    manifest_path = os.path.expanduser('~/claudecode_workspace/WORKSPACE-MANIFEST.md')
    pulse_path = os.path.expanduser('~/claudecode_workspace/WORKSPACE-PULSE.json')
    manifest_content = ''
    pulse_content = None
    try:
        with open(manifest_path) as f:
            manifest_content = f.read()
    except FileNotFoundError:
        pass
    try:
        with open(pulse_path) as f:
            pulse_content = json.load(f)
    except (FileNotFoundError, ValueError):
        pass
    return {
        'manifest': {'source': 'WORKSPACE-MANIFEST.md', 'content': manifest_content},
        'pulse': {'source': 'WORKSPACE-PULSE.json', 'content': pulse_content},
    }


@router.get('/api/settings/project-dirs')
def get_project_dirs(request: Request):
    err = require_admin(request)
    if err:
        return err
    dirs = execute_action.get_project_dirs()
    return {'project_dirs': dirs}


@router.post('/api/settings/project-dirs')
async def post_project_dirs(request: Request):
    err = require_admin(request)
    if err:
        return err
    body = await request.json()
    dirs = body.get('project_dirs', [])
    execute_action.set_project_dirs(dirs)
    return {'ok': True}
