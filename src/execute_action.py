#!/usr/bin/env python3
"""
v8.0 — Async Action Execution Engine (execute_action.py)

Executes confirmed action points using Codex CLI (default) or Claude CLI.
Manages concurrency (max 3), timeouts (15 min no-activity), and result capture.

Not run standalone — imported by serve.py and called via API endpoints.
"""

import json
import os
import subprocess
import threading
import time
import uuid
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STREAM_DIR = os.path.join(BASE_DIR, 'data', 'action_streams')

import sys
sys.path.insert(0, BASE_DIR)
import db
import remote_db

# Ensure stream log directory exists
if not remote_db.app_state_to_remote():
    os.makedirs(STREAM_DIR, exist_ok=True)

# ── Concurrency control ──
MAX_CONCURRENT = 3
_semaphore = threading.Semaphore(MAX_CONCURRENT)
_runners = {}  # action_id → {thread, proc, started_at, status}
_runners_lock = threading.Lock()
_queue = []  # action_ids waiting to execute
_queue_lock = threading.Lock()

# ── Config ──
DEFAULT_TOOL = 'codex'  # 'codex' or 'claude'
DEFAULT_BUDGET_USD = 0.5
INACTIVITY_TIMEOUT = 15 * 60  # 15 minutes in seconds

# ── Project directory config ──
SETTINGS_PATH = os.path.join(BASE_DIR, 'data', 'settings.json')


def _load_settings():
    """Load settings from data/settings.json."""
    if remote_db.app_state_to_remote():
        return remote_db.get_setting_remote('project_dirs') or {}
    try:
        with open(SETTINGS_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_settings(settings):
    """Save settings to data/settings.json."""
    if remote_db.app_state_to_remote():
        remote_db.set_setting_remote('project_dirs', settings)
        return
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    with open(SETTINGS_PATH, 'w') as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)


def get_project_dirs():
    """Get configured project directories."""
    settings = _load_settings()
    dirs = settings.get('project_dirs', [
        {"path": "~/claudecode_workspace", "alias": "主工作区"}
    ])
    return dirs


def set_project_dirs(dirs):
    """Update project directory configuration."""
    settings = _load_settings()
    settings['project_dirs'] = dirs
    _save_settings(settings)


def resolve_project_path(related_project):
    """Resolve a related_project string to an absolute path."""
    if not related_project:
        return os.path.expanduser("~/claudecode_workspace")

    dirs = get_project_dirs()
    # Try to match project within configured directories
    for d in dirs:
        base = os.path.expanduser(d['path'])
        candidate = os.path.join(base, related_project)
        if os.path.isdir(candidate):
            return candidate
        # Try without nested path
        if os.path.isdir(base) and related_project in d.get('alias', ''):
            return base

    # Fallback: try as absolute path
    expanded = os.path.expanduser(related_project)
    if os.path.isdir(expanded):
        return expanded

    # Default to first configured dir
    if dirs:
        return os.path.expanduser(dirs[0]['path'])
    return os.path.expanduser("~/claudecode_workspace")


def _stream_log_path(action_id):
    """Return the path to the stream log file for an action."""
    return os.path.join(STREAM_DIR, f'{action_id}.log')


def _append_stream_log(action_id, text):
    """Append text to the stream log file for an action."""
    if not text:
        return
    try:
        with open(_stream_log_path(action_id), 'a', encoding='utf-8') as f:
            f.write(text)
            f.flush()
    except Exception:
        pass


def read_stream_log(action_id, offset=0):
    """Read stream log from a given line offset. Returns (lines, total_lines)."""
    path = _stream_log_path(action_id)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            all_lines = f.readlines()
        total = len(all_lines)
        return all_lines[offset:], total
    except FileNotFoundError:
        return [], 0
    except Exception:
        return [], 0


_ACTION_EXEC_SAFE_ENV = {
    'PATH', 'HOME', 'USER', 'LOGNAME', 'SHELL', 'TERM', 'LANG', 'LC_ALL',
    'LC_CTYPE', 'TMPDIR', 'TMP', 'TEMP', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME',
    'XDG_DATA_HOME', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR',
}


def _clean_env():
    """Create a minimal subprocess env without inheriting host secrets."""
    env = {
        key: os.environ[key]
        for key in _ACTION_EXEC_SAFE_ENV
        if key in os.environ
    }
    allowlist = os.environ.get('ACTION_EXEC_ENV_ALLOWLIST', '')
    for key in (k.strip() for k in allowlist.replace(',', ' ').split()):
        if key and key in os.environ:
            env[key] = os.environ[key]
    return env


def _execute_with_codex(action, project_path, session_id):
    """Execute action using Codex CLI."""
    action_id = action['id']
    cmd = ['codex', 'exec']
    if project_path:
        cmd.extend(['-C', project_path])
    cmd.append(action['prompt'])

    # Initialize stream log
    _append_stream_log(action_id, f"[codex] Starting execution...\n[codex] Project: {project_path}\n")

    start_time = time.time()
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=_clean_env(), text=True
        )

        # Store proc for potential kill
        with _runners_lock:
            if action_id in _runners:
                _runners[action_id]['proc'] = proc

        # Read output line by line and write to stream log
        output_lines = []
        last_activity = time.time()
        while True:
            line = proc.stdout.readline()
            if line:
                output_lines.append(line)
                _append_stream_log(action_id, line)
                last_activity = time.time()
            elif proc.poll() is not None:
                break
            else:
                if time.time() - last_activity > INACTIVITY_TIMEOUT:
                    proc.kill()
                    proc.communicate()
                    duration = int(time.time() - start_time)
                    _append_stream_log(action_id, "\n[timeout] No activity for 15 minutes.\n")
                    partial = "".join(output_lines)
                    return -1, f"[timeout] No activity for 15 minutes.\n\nPartial output:\n{partial}", duration, session_id
                time.sleep(0.1)

        duration = int(time.time() - start_time)
        exit_code = proc.returncode
        result_text = "".join(output_lines) or "(no output)"
        _append_stream_log(action_id, f"\n[codex] Finished with exit code {exit_code} ({duration}s)\n")
        return exit_code, result_text, duration, session_id

    except FileNotFoundError:
        _append_stream_log(action_id, "[error] codex CLI not found\n")
        return -1, "[error] codex CLI not found. Install with: npm i -g @openai/codex", 0, session_id
    except Exception as e:
        duration = int(time.time() - start_time)
        _append_stream_log(action_id, f"[error] {str(e)}\n")
        return -1, f"[error] {str(e)}", duration, session_id


def _execute_with_claude(action, project_path, session_id):
    """Execute action using Claude CLI."""
    action_id = action['id']
    cmd = [
        'claude', '--print',
        '--session-id', session_id,
        '--output-format', 'json',
        '--max-budget-usd', str(DEFAULT_BUDGET_USD),
    ]
    if project_path:
        cmd.extend(['--cwd', project_path])
    cmd.extend(['-p', action['prompt']])

    # Initialize stream log
    _append_stream_log(action_id, f"[claude] Starting execution...\n[claude] Project: {project_path}\n")

    start_time = time.time()
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=_clean_env(), text=True
        )

        with _runners_lock:
            if action_id in _runners:
                _runners[action_id]['proc'] = proc

        # Monitor with inactivity timeout
        last_activity = time.time()
        output_lines = []

        while True:
            # Check if process completed
            ret = proc.poll()
            if ret is not None:
                remaining_out, remaining_err = proc.communicate()
                if remaining_out:
                    output_lines.append(remaining_out)
                    _append_stream_log(action_id, remaining_out)
                if remaining_err:
                    output_lines.append(remaining_err)
                    _append_stream_log(action_id, remaining_err)
                break

            # Try to read output (non-blocking via timeout)
            try:
                line = proc.stdout.readline()
                if line:
                    output_lines.append(line)
                    _append_stream_log(action_id, line)
                    last_activity = time.time()
            except Exception:
                pass

            # Check inactivity timeout
            if time.time() - last_activity > INACTIVITY_TIMEOUT:
                proc.kill()
                proc.communicate()
                duration = int(time.time() - start_time)
                partial = "".join(output_lines)
                _append_stream_log(action_id, "\n[timeout] No activity for 15 minutes.\n")
                return -1, f"[timeout] No activity for 15 minutes.\n\nPartial output:\n{partial}", duration, session_id

            time.sleep(0.5)

        duration = int(time.time() - start_time)
        exit_code = proc.returncode
        full_output = "".join(output_lines)

        # Try to parse JSON output from Claude
        result_text = full_output
        try:
            data = json.loads(full_output)
            if isinstance(data, dict) and 'result' in data:
                result_text = data['result']
        except (json.JSONDecodeError, TypeError):
            pass

        _append_stream_log(action_id, f"\n[claude] Finished with exit code {exit_code} ({duration}s)\n")
        return exit_code, result_text, duration, session_id

    except FileNotFoundError:
        _append_stream_log(action_id, "[error] claude CLI not found\n")
        return -1, "[error] claude CLI not found. Install Claude CLI first.", 0, session_id
    except Exception as e:
        duration = int(time.time() - start_time)
        _append_stream_log(action_id, f"[error] {str(e)}\n")
        return -1, f"[error] {str(e)}", duration, session_id


def _run_action(action_id, tool='codex'):
    """Background thread function to execute an action."""
    _semaphore.acquire()
    try:
        conn = None
        if remote_db.app_state_to_remote():
            action = remote_db.get_action_remote(action_id)
        else:
            conn = db.get_conn()
            action = db.get_action(conn, action_id)
        if not action:
            if conn:
                conn.close()
            return

        session_id = action.get('session_id') or str(uuid.uuid4())
        project_path = resolve_project_path(action.get('related_project'))

        # Update status to executing
        if remote_db.app_state_to_remote():
            remote_db.update_action_remote(
                action_id,
                status='executing',
                session_id=session_id,
                execution_tool=tool,
                executed_at=datetime.now().isoformat(),
            )
            remote_db.log_action_event_remote(None, action_id, 'executing', {
                'tool': tool, 'project_path': project_path
            })
        else:
            db.update_action(conn, action_id,
                             status='executing',
                             session_id=session_id,
                             execution_tool=tool,
                             executed_at=datetime.now().isoformat())
            db._log_action_event(conn, action_id, 'executing', {
                'tool': tool, 'project_path': project_path
            })
            conn.close()

        # Execute
        if tool == 'claude':
            exit_code, result, duration, sid = _execute_with_claude(action, project_path, session_id)
        else:
            exit_code, result, duration, sid = _execute_with_codex(action, project_path, session_id)

        # Update result
        status = 'done' if exit_code == 0 else 'failed'
        if remote_db.app_state_to_remote():
            remote_db.update_action_remote(
                action_id,
                status=status,
                execution_result=result,
                execution_exit_code=exit_code,
                execution_duration_seconds=duration,
                session_id=sid,
                completed_at=datetime.now().isoformat(),
            )
            remote_db.log_action_event_remote(None, action_id, status, {
                'exit_code': exit_code,
                'duration_seconds': duration,
                'result_summary': (result or '')[:500]
            })
        else:
            conn = db.get_conn()
            db.update_action(conn, action_id,
                             status=status,
                             execution_result=result,
                             execution_exit_code=exit_code,
                             execution_duration_seconds=duration,
                             session_id=sid,
                             completed_at=datetime.now().isoformat())
            db._log_action_event(conn, action_id, status, {
                'exit_code': exit_code,
                'duration_seconds': duration,
                'result_summary': (result or '')[:500]
            })
            conn.close()

    except Exception as e:
        try:
            if remote_db.app_state_to_remote():
                remote_db.update_action_remote(
                    action_id,
                    status='failed',
                    execution_result=f"[error] Execution thread error: {e}",
                    completed_at=datetime.now().isoformat(),
                )
            else:
                conn = db.get_conn()
                db.update_action(conn, action_id,
                                 status='failed',
                                 execution_result=f"[error] Execution thread error: {e}",
                                 completed_at=datetime.now().isoformat())
                conn.close()
        except Exception:
            pass
    finally:
        _semaphore.release()
        with _runners_lock:
            _runners.pop(action_id, None)
        # Process queue
        _process_queue()


def _process_queue():
    """Start next queued action if capacity available."""
    with _queue_lock:
        while _queue and _semaphore._value > 0:
            next_id, next_tool = _queue.pop(0)
            start_execution(next_id, tool=next_tool, _from_queue=True)


def start_execution(action_id, tool='codex', _from_queue=False):
    """Start executing an action. Returns status dict."""
    with _runners_lock:
        if action_id in _runners:
            return {'ok': False, 'msg': 'Action already executing'}

    # Check concurrency
    if not _from_queue and not _semaphore._value > 0:
        with _queue_lock:
            _queue.append((action_id, tool))
        return {'ok': True, 'msg': 'Queued (max concurrent reached)', 'queued': True}

    with _runners_lock:
        _runners[action_id] = {
            'started_at': datetime.now().isoformat(),
            'status': 'starting',
            'proc': None
        }

    t = threading.Thread(target=_run_action, args=(action_id, tool), daemon=True)
    t.start()

    with _runners_lock:
        if action_id in _runners:
            _runners[action_id]['thread'] = t

    return {'ok': True, 'msg': f'Execution started with {tool}'}


def get_execution_status(action_id):
    """Get real-time execution status."""
    with _runners_lock:
        runner = _runners.get(action_id)
        if runner:
            started = runner.get('started_at', '')
            elapsed = 0
            if started:
                try:
                    elapsed = int((datetime.now() - datetime.fromisoformat(started)).total_seconds())
                except Exception:
                    pass
            return {
                'executing': True,
                'elapsed_seconds': elapsed,
                'tool': runner.get('tool', 'unknown')
            }

    # Check queue
    with _queue_lock:
        for i, (qid, qtool) in enumerate(_queue):
            if qid == action_id:
                return {'queued': True, 'queue_position': i + 1}

    # v8.0.3: Check DB status — if DB says confirmed/executing but runner is gone,
    # the process might still be starting. Don't report false negative.
    try:
        if remote_db.app_state_to_remote():
            action = remote_db.get_action_remote(action_id)
        else:
            conn = db.get_conn()
            action = db.get_action(conn, action_id)
            conn.close()
        if action and action['status'] in ('confirmed', 'executing'):
            return {'executing': True, 'elapsed_seconds': 0, 'tool': 'unknown', 'starting': True}
    except Exception:
        pass

    return {'executing': False, 'queued': False}


def read_project_context(action_id):
    """Read project context using Codex CLI for an action."""
    if remote_db.app_state_to_remote():
        action = remote_db.get_action_remote(action_id)
    else:
        conn = db.get_conn()
        action = db.get_action(conn, action_id)
        conn.close()
    if not action:
        return None

    project_path = resolve_project_path(action.get('related_project'))
    title = action.get('title', '')

    prompt = (
        f"分析项目现状，重点关注与以下行动相关的部分：{title}。"
        f"输出：1. 最近 5 条 git 提交 2. PRD/TODO 中相关段落 3. 是否已有类似实现"
    )

    def _git_fallback(path):
        """Fast fallback: read git log directly without Codex."""
        try:
            result = subprocess.run(
                ['git', 'log', '--oneline', '-5'],
                capture_output=True, text=True, timeout=10,
                cwd=path
            )
            return f"最近 5 条 git 提交:\n{result.stdout}" if result.stdout else "(no git history)"
        except Exception:
            return "(无法读取项目现状)"

    try:
        result = subprocess.run(
            ['codex', 'exec', '-C', project_path, prompt],
            capture_output=True, text=True, timeout=180,
            env=_clean_env()
        )
        context = result.stdout or result.stderr or "(no output)"
    except FileNotFoundError:
        context = _git_fallback(project_path)
    except subprocess.TimeoutExpired:
        # Codex timed out — fall back to fast git log
        context = _git_fallback(project_path)
    except Exception as e:
        context = f"(读取失败: {str(e)[:100]})"

    # Update DB
    if remote_db.app_state_to_remote():
        remote_db.update_action_remote(
            action_id,
            project_context=context,
            project_context_updated_at=datetime.now().isoformat(),
        )
    else:
        conn = db.get_conn()
        db.update_action(conn, action_id,
                         project_context=context,
                         project_context_updated_at=datetime.now().isoformat())
        conn.close()

    return context
