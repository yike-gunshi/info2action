"""PTY, ttyd, and CLI terminal endpoints."""

import base64
import json
import os
import pty
import select
import signal
import subprocess
import shlex
import threading
import time

from datetime import datetime

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

import db
import remote_db
from authz import require_admin
from deps import BASE

router = APIRouter()


# ── Module-level state ──────────────────────────────────────

LOCAL_BIN_PATH = ':'.join([
    os.path.expanduser('~/.local/bin'),
    '/opt/homebrew/bin',
    '/usr/local/bin',
])

# PTY session manager
_pty_sessions = {}  # id -> { 'pid', 'fd', 'alive', 'exit_code', 'buffer': [bytes], 'buf_event': Event }
_pty_lock = threading.Lock()

# tmux + ttyd session manager
_ttyd_sessions = {}  # action_id -> {tmux_name, ttyd_pid, ttyd_proc, port, status, last_activity, ...}
_ttyd_lock = threading.Lock()

TTYD_IDLE_TIMEOUT_SEC = 600

# CLI processes (shared across requests via module state)
_cli_procs = {}


def _resolve_bin(name):
    for candidate in [
        os.path.expanduser(f'~/.local/bin/{name}'),
        f'/opt/homebrew/bin/{name}',
        f'/usr/local/bin/{name}',
    ]:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return name


# ── tmux helpers ────────────────────────────────────────────

def _tmux_session_name(action_id):
    return f"act_{action_id[:8]}"


def _tmux_has_session(tmux_name):
    result = subprocess.run(
        ['tmux', 'has-session', '-t', tmux_name],
        capture_output=True
    )
    return result.returncode == 0


def _escape_prompt_for_shell(prompt):
    return prompt.replace("'", "'\\''")


def _wait_for_codex_ready(tmux_name, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = subprocess.run(
            ['tmux', 'capture-pane', '-t', tmux_name, '-p'],
            capture_output=True, text=True
        )
        if r.returncode == 0:
            content = r.stdout
            if '\u203a' in content:
                return True
        time.sleep(0.5)
    return False


def _find_free_ttyd_port():
    import socket
    for port in range(7700, 7800):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(('127.0.0.1', port))
            s.close()
            return port
        except OSError:
            continue
    return None


def _start_tmux_ttyd(action_id, prompt, cwd=None, thread_id=None):
    port = _find_free_ttyd_port()
    if not port:
        return None

    work_dir = cwd or BASE
    tmux_name = _tmux_session_name(action_id)

    if not _tmux_has_session(tmux_name):
        tmux_create = subprocess.run(
            ['tmux', 'new-session', '-d', '-s', tmux_name, '-c', work_dir],
            capture_output=True
        )
        if tmux_create.returncode != 0:
            return None

        subprocess.run(
            ['tmux', 'set-option', '-t', tmux_name, 'mouse', 'on'],
            capture_output=True
        )

        codex_start = 'codex --full-auto'
        if thread_id:
            codex_start = f'codex --full-auto --resume {shlex.quote(thread_id)}'
        subprocess.run(
            ['tmux', 'send-keys', '-t', tmux_name, codex_start, 'Enter'],
            capture_output=True
        )
        _wait_for_codex_ready(tmux_name, timeout=30)
        escaped_prompt = _escape_prompt_for_shell(prompt)
        subprocess.run(
            ['tmux', 'send-keys', '-t', tmux_name, escaped_prompt, 'Enter'],
            capture_output=True
        )

    ttyd_bin = _resolve_bin('ttyd')
    ttyd_proc = subprocess.Popen(
        [ttyd_bin, '-p', str(port), '-W',
         '-P', '30',
         '-t', 'fontSize=14', '-t', 'fontFamily=JetBrains Mono,Menlo,monospace',
         'tmux', 'attach-session', '-t', tmux_name],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    session = {
        'tmux_name': tmux_name,
        'ttyd_pid': ttyd_proc.pid,
        'ttyd_proc': ttyd_proc,
        'port': port,
        'thread_id': thread_id,
        'status': 'running',
        'started_at': datetime.now().isoformat(),
        'last_activity': time.time(),
        'cwd': work_dir,
        'prompt': prompt,
        'action_id': action_id,
    }

    with _ttyd_lock:
        old = _ttyd_sessions.get(action_id)
        if old and old.get('ttyd_proc'):
            try:
                old['ttyd_proc'].terminate()
            except:
                pass
        _ttyd_sessions[action_id] = session

    def _monitor_ttyd():
        ttyd_proc.wait()
        with _ttyd_lock:
            s = _ttyd_sessions.get(action_id)
            if s and s['ttyd_pid'] == ttyd_proc.pid:
                if _tmux_has_session(tmux_name):
                    s['status'] = 'disconnected'
                else:
                    s['status'] = 'exited'

    def _idle_watchdog():
        while True:
            time.sleep(60)
            with _ttyd_lock:
                s = _ttyd_sessions.get(action_id)
                if not s or s['ttyd_pid'] != ttyd_proc.pid:
                    break
                if s['status'] != 'running':
                    break
                if time.time() - s.get('last_activity', 0) > TTYD_IDLE_TIMEOUT_SEC:
                    try:
                        ttyd_proc.terminate()
                        s['status'] = 'disconnected'
                    except:
                        pass
                    break

    threading.Thread(target=_monitor_ttyd, daemon=True).start()
    threading.Thread(target=_idle_watchdog, daemon=True).start()

    return session


def _reconnect_ttyd(action_id):
    with _ttyd_lock:
        session = _ttyd_sessions.get(action_id)

    tmux_name = _tmux_session_name(action_id)
    if not _tmux_has_session(tmux_name):
        return None

    port = _find_free_ttyd_port()
    if not port:
        return None

    ttyd_bin = _resolve_bin('ttyd')
    ttyd_proc = subprocess.Popen(
        [ttyd_bin, '-p', str(port), '-W',
         '-P', '30',
         '-t', 'fontSize=14', '-t', 'fontFamily=JetBrains Mono,Menlo,monospace',
         'tmux', 'attach-session', '-t', tmux_name],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    new_session = {
        'tmux_name': tmux_name,
        'ttyd_pid': ttyd_proc.pid,
        'ttyd_proc': ttyd_proc,
        'port': port,
        'thread_id': session.get('thread_id') if session else None,
        'status': 'running',
        'started_at': session.get('started_at', datetime.now().isoformat()) if session else datetime.now().isoformat(),
        'last_activity': time.time(),
        'cwd': session.get('cwd', BASE) if session else BASE,
        'prompt': session.get('prompt', '') if session else '',
        'action_id': action_id,
    }

    with _ttyd_lock:
        _ttyd_sessions[action_id] = new_session

    def _monitor_ttyd():
        ttyd_proc.wait()
        with _ttyd_lock:
            s = _ttyd_sessions.get(action_id)
            if s and s['ttyd_pid'] == ttyd_proc.pid:
                if _tmux_has_session(tmux_name):
                    s['status'] = 'disconnected'
                else:
                    s['status'] = 'exited'

    def _idle_watchdog():
        while True:
            time.sleep(60)
            with _ttyd_lock:
                s = _ttyd_sessions.get(action_id)
                if not s or s['ttyd_pid'] != ttyd_proc.pid:
                    break
                if s['status'] != 'running':
                    break
                if time.time() - s.get('last_activity', 0) > TTYD_IDLE_TIMEOUT_SEC:
                    try:
                        ttyd_proc.terminate()
                        s['status'] = 'disconnected'
                    except:
                        pass
                    break

    threading.Thread(target=_monitor_ttyd, daemon=True).start()
    threading.Thread(target=_idle_watchdog, daemon=True).start()

    return new_session


def _stop_ttyd_only(action_id):
    with _ttyd_lock:
        session = _ttyd_sessions.get(action_id)
    if not session:
        return None
    ttyd_proc = session.get('ttyd_proc')
    if ttyd_proc and ttyd_proc.poll() is None:
        try:
            ttyd_proc.terminate()
        except:
            pass
    session['status'] = 'disconnected'
    return session


def _get_ttyd_status(action_id):
    with _ttyd_lock:
        session = _ttyd_sessions.get(action_id)

    tmux_name = _tmux_session_name(action_id)
    tmux_alive = _tmux_has_session(tmux_name)

    if not session:
        if tmux_alive:
            return {'tmux_alive': True, 'ttyd_alive': False, 'port': None, 'status': 'disconnected'}
        return {'tmux_alive': False, 'ttyd_alive': False, 'port': None, 'status': 'none'}

    ttyd_alive = session.get('ttyd_proc') and session['ttyd_proc'].poll() is None

    if ttyd_alive:
        session['last_activity'] = time.time()

    if tmux_alive and ttyd_alive:
        status = 'running'
    elif tmux_alive and not ttyd_alive:
        status = 'disconnected'
    else:
        status = 'exited'

    session['status'] = status

    return {
        'tmux_alive': tmux_alive,
        'ttyd_alive': ttyd_alive,
        'port': session['port'] if ttyd_alive else None,
        'status': status,
        'started_at': session.get('started_at'),
        'thread_id': session.get('thread_id'),
        'cwd': session.get('cwd'),
    }


def recover_tmux_sessions():
    """On server start, recover existing tmux sessions named act_*"""
    try:
        r = subprocess.run(['tmux', 'ls', '-F', '#{session_name}'], capture_output=True, text=True)
        if r.returncode != 0:
            return
        if remote_db.app_state_to_remote():
            # Remote action recovery by partial tmux name is intentionally
            # skipped; active PTY state is process-local, while canonical
            # action status still lives in Supabase.
            return
        conn = db.get_conn()
        for line in r.stdout.strip().split('\n'):
            name = line.strip()
            if not name.startswith('act_'):
                continue
            prefix = name[4:]
            row = conn.execute("SELECT id FROM actions WHERE id LIKE ? AND status='executing'", (prefix + '%',)).fetchone()
            if not row:
                continue
            action_id = row[0]
            with _ttyd_lock:
                if action_id not in _ttyd_sessions:
                    _ttyd_sessions[action_id] = {
                        'tmux_name': name,
                        'ttyd_proc': None,
                        'port': None,
                        'status': 'disconnected',
                        'last_activity': time.time(),
                    }
                    print(f'[ttyd] recovered tmux session: {name} -> {action_id}')
        conn.close()
    except Exception as e:
        print(f'[ttyd] tmux recovery error: {e}')


# ── Routes: CLI ─────────────────────────────────────────────

@router.get('/api/cli/status')
def get_cli_status(request: Request):
    err = require_admin(request)
    if err:
        return err
    result = {}
    for name, candidates in [
        ('codex', [_resolve_bin('codex'), '/opt/homebrew/bin/codex', '/usr/local/bin/codex']),
        ('claude', [_resolve_bin('claude'), '/opt/homebrew/bin/claude', '/usr/local/bin/claude']),
    ]:
        for p in candidates:
            if os.path.isfile(p) and os.access(p, os.X_OK):
                try:
                    ver = subprocess.check_output([p, '--version'], stderr=subprocess.STDOUT, timeout=5).decode().strip()
                    result[name] = f'{ver} ({p})'
                except Exception:
                    result[name] = p
                break
    return result


@router.get('/api/cli/exec')
def get_cli_exec(request: Request, tool: str = Query('codex'), prompt: str = Query(''), cwd: str = Query(None), auto: str = Query('1')):
    err = require_admin(request)
    if err:
        return err
    import uuid as _uuid
    exec_id = str(_uuid.uuid4())[:8]
    use_auto = auto == '1'
    use_cwd = cwd or BASE

    if tool == 'codex':
        codex_bin = _resolve_bin('codex')
        cmd = [codex_bin, 'exec', '--json', '-C', use_cwd]
        if use_auto:
            cmd.append('--full-auto')
        cmd.append(prompt)
    else:
        claude_bin = _resolve_bin('claude')
        cmd = [claude_bin, '-p', '--output-format', 'stream-json', '--verbose',
               '--model', 'sonnet']
        if use_auto:
            cmd.extend(['--dangerously-skip-permissions'])
        cmd.append(prompt)

    def generate():
        def sse(event, data):
            return f'event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'

        env = os.environ.copy()
        env['PATH'] = LOCAL_BIN_PATH + ':' + env.get('PATH', '')

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, cwd=use_cwd if tool == 'claude' else None, env=env)
            _cli_procs[exec_id] = proc

            yield sse('init', {'exec_id': exec_id, 'tool': tool, 'thread_id': '', 'session_id': ''})

            thread_id = ''
            cost = 0

            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    yield sse('error_msg', {'message': line})
                    continue

                if tool == 'codex':
                    etype = evt.get('type', '')
                    if etype == 'thread.started':
                        thread_id = evt.get('thread_id', '')
                        yield sse('init', {'exec_id': exec_id, 'tool': 'codex', 'thread_id': thread_id})
                    elif etype == 'item.completed':
                        item = evt.get('item', {})
                        itype = item.get('type', '')
                        if itype == 'agent_message':
                            yield sse('agent_message', {'text': item.get('text', '')})
                        elif itype == 'command_execution':
                            yield sse('command', {
                                'command': item.get('command', ''),
                                'output': item.get('aggregated_output', ''),
                                'exit_code': item.get('exit_code')
                            })
                    elif etype == 'turn.completed':
                        usage = evt.get('usage', {})
                        cost = usage.get('total_cost_usd', 0) if 'total_cost_usd' in usage else 0
                else:
                    etype = evt.get('type', '')
                    if etype == 'system' and evt.get('subtype') == 'init':
                        session_id = evt.get('session_id', '')
                        yield sse('init', {'exec_id': exec_id, 'tool': 'claude', 'session_id': session_id})
                    elif etype == 'assistant':
                        msg = evt.get('message', {})
                        for block in msg.get('content', []):
                            if block.get('type') == 'text':
                                yield sse('agent_message', {'text': block.get('text', '')})
                            elif block.get('type') == 'tool_use':
                                yield sse('command', {
                                    'command': block.get('name', '') + '(' + json.dumps(block.get('input', {}), ensure_ascii=False)[:200] + ')',
                                    'output': '',
                                    'exit_code': None
                                })
                    elif etype == 'result':
                        cost = evt.get('total_cost_usd', 0)
                        thread_id = evt.get('session_id', '')

            proc.wait()
            stderr_out = proc.stderr.read()
            if stderr_out and proc.returncode != 0:
                yield sse('error_msg', {'message': stderr_out[:500]})

            yield sse('done', {'thread_id': thread_id, 'cost': cost, 'exit_code': proc.returncode})

        except Exception as e:
            yield sse('error_msg', {'message': str(e)})
            yield sse('done', {'thread_id': '', 'cost': 0, 'exit_code': -1})
        finally:
            _cli_procs.pop(exec_id, None)

    return StreamingResponse(generate(), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no'})


@router.post('/api/cli/stop')
async def post_cli_stop(request: Request, exec_id: str = Query('')):
    err = require_admin(request)
    if err:
        return err
    proc = _cli_procs.get(exec_id)
    if proc:
        proc.send_signal(signal.SIGTERM)
        return {'ok': True, 'stopped': exec_id}
    else:
        return JSONResponse({'error': 'process not found'}, status_code=404)


# ── Routes: PTY ─────────────────────────────────────────────

@router.post('/api/pty/create')
async def post_pty_create(request: Request):
    err = require_admin(request)
    if err:
        return err
    import uuid as _uuid
    body = await request.json()
    tool = body.get('tool', 'codex')
    prompt = body.get('prompt', '')
    cwd = body.get('cwd', BASE)
    cols = body.get('cols', 120)
    rows = body.get('rows', 30)

    env = os.environ.copy()
    env['PATH'] = LOCAL_BIN_PATH + ':' + env.get('PATH', '')
    env['TERM'] = 'xterm-256color'
    env['COLUMNS'] = str(cols)
    env['LINES'] = str(rows)

    resume = body.get('resume', False)
    prev_thread_id = body.get('thread_id', '')
    if tool == 'codex':
        if resume and prev_thread_id:
            cmd = [_resolve_bin('codex'), 'exec', 'resume', prev_thread_id,
                   prompt, '--full-auto', '--json']
        else:
            cmd = [_resolve_bin('codex'), 'exec', '--full-auto', '-C', cwd, prompt]
    else:
        claude_bin = _resolve_bin('claude')
        if resume:
            cmd = [claude_bin, '--resume', '--verbose', '--model', 'sonnet',
                   '--dangerously-skip-permissions', '-p', prompt]
        else:
            cmd = [claude_bin, '-p', '--verbose', '--model', 'sonnet',
                   '--dangerously-skip-permissions', prompt]

    try:
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(cwd)
            os.execve(cmd[0], cmd, env)
        else:
            import fcntl, struct, termios
            winsize = struct.pack('HHHH', rows, cols, 0, 0)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)

            sid = str(_uuid.uuid4())[:12]
            buf_event = threading.Event()
            sess = {'pid': pid, 'fd': fd, 'alive': True,
                    'exit_code': 0, 'buffer': [], 'buf_event': buf_event}
            with _pty_lock:
                _pty_sessions[sid] = sess

            def _reader():
                while sess['alive']:
                    try:
                        r, _, _ = select.select([fd], [], [], 0.1)
                        if r:
                            data = os.read(fd, 16384)
                            if not data:
                                break
                            sess['buffer'].append(data)
                            buf_event.set()
                            if not sess.get('thread_id'):
                                try:
                                    import re as _re
                                    text = data.decode('utf-8', errors='replace')
                                    for line in text.split('\n'):
                                        line = line.strip()
                                        if '"thread.started"' in line and '"thread_id"' in line:
                                            m = _re.search(r'"thread_id"\s*:\s*"([^"]+)"', line)
                                            if m:
                                                sess['thread_id'] = m.group(1)
                                except Exception:
                                    pass
                    except (OSError, ValueError):
                        break
            threading.Thread(target=_reader, daemon=True).start()

            def _monitor():
                try:
                    _, status = os.waitpid(pid, 0)
                    code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
                except ChildProcessError:
                    code = 0
                sess['exit_code'] = code
                sess['alive'] = False
                buf_event.set()
            threading.Thread(target=_monitor, daemon=True).start()

            return {'id': sid, 'tool': tool}
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/pty/input')
async def post_pty_input(request: Request):
    err = require_admin(request)
    if err:
        return err
    body = await request.json()
    sid = body.get('id', '')
    data = body.get('data', '')
    with _pty_lock:
        sess = _pty_sessions.get(sid)
    if sess and sess['alive']:
        try:
            os.write(sess['fd'], data.encode())
            return {'ok': True}
        except OSError as e:
            return JSONResponse({'error': str(e)}, status_code=500)
    else:
        return JSONResponse({'error': 'session not found'}, status_code=404)


@router.post('/api/pty/resize')
async def post_pty_resize(request: Request):
    err = require_admin(request)
    if err:
        return err
    body = await request.json()
    sid = body.get('id', '')
    cols = body.get('cols', 120)
    rows = body.get('rows', 30)
    with _pty_lock:
        sess = _pty_sessions.get(sid)
    if sess and sess['alive']:
        try:
            import fcntl, struct, termios
            winsize = struct.pack('HHHH', rows, cols, 0, 0)
            fcntl.ioctl(sess['fd'], termios.TIOCSWINSZ, winsize)
            return {'ok': True}
        except OSError as e:
            return JSONResponse({'error': str(e)}, status_code=500)
    else:
        return JSONResponse({'error': 'session not found'}, status_code=404)


@router.post('/api/pty/kill')
async def post_pty_kill(request: Request):
    err = require_admin(request)
    if err:
        return err
    body = await request.json()
    sid = body.get('id', '')
    with _pty_lock:
        sess = _pty_sessions.get(sid)
    if sess:
        try:
            os.kill(sess['pid'], signal.SIGTERM)
        except ProcessLookupError:
            pass
        sess['alive'] = False
        return {'ok': True}
    else:
        return JSONResponse({'error': 'session not found'}, status_code=404)


@router.get('/api/pty/stream')
def get_pty_stream(request: Request, id: str = Query('')):
    err = require_admin(request)
    if err:
        return err
    with _pty_lock:
        sess = _pty_sessions.get(id)
    if not sess:
        return JSONResponse({'error': 'session not found'}, status_code=404)

    def generate():
        buf_event = sess['buf_event']
        read_pos = 0
        thread_id_sent = False
        try:
            while True:
                buf = sess['buffer']
                while read_pos < len(buf):
                    data = buf[read_pos]
                    read_pos += 1
                    encoded = base64.b64encode(data).decode('ascii')
                    yield f'event: output\ndata: {json.dumps({"data": encoded})}\n\n'

                if not thread_id_sent and sess.get('thread_id'):
                    yield f'event: thread_id\ndata: {json.dumps({"thread_id": sess["thread_id"]})}\n\n'
                    thread_id_sent = True

                if not sess['alive'] and read_pos >= len(buf):
                    break

                buf_event.clear()
                buf_event.wait(timeout=0.5)

            code = sess.get('exit_code', 0)
            exit_data = {"code": code}
            if sess.get('thread_id'):
                exit_data["thread_id"] = sess['thread_id']
            yield f'event: exit\ndata: {json.dumps(exit_data)}\n\n'
        except (BrokenPipeError, ConnectionResetError):
            pass

    return StreamingResponse(generate(), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no'})


# ── Routes: ttyd ────────────────────────────────────────────

@router.post('/api/ttyd/start')
async def post_ttyd_start(request: Request):
    err = require_admin(request)
    if err:
        return err
    body = await request.json()
    prompt = body.get('prompt', '')
    action_id = body.get('action_id', '')
    cwd = body.get('cwd')
    thread_id = body.get('thread_id')

    if not prompt or not action_id:
        return JSONResponse({'error': 'prompt and action_id required'}, status_code=400)

    session = _start_tmux_ttyd(action_id, prompt, cwd=cwd, thread_id=thread_id)
    if not session:
        return JSONResponse({'error': 'no free port or tmux failed'}, status_code=500)

    try:
        if remote_db.app_state_to_remote():
            remote_db.update_action_remote(
                action_id,
                status='executing',
                confirmed_at=session['started_at'],
            )
        else:
            import sqlite3
            with sqlite3.connect(db.DB_PATH) as conn:
                conn.execute("UPDATE actions SET status='executing', confirmed_at=? WHERE id=?",
                            (session['started_at'], action_id))
                conn.commit()
    except:
        pass

    return {
        'port': session['port'],
        'status': 'running',
        'started_at': session['started_at'],
    }


@router.post('/api/ttyd/reconnect')
async def post_ttyd_reconnect(request: Request):
    err = require_admin(request)
    if err:
        return err
    body = await request.json()
    action_id = body.get('action_id', '')
    if not action_id:
        return JSONResponse({'error': 'action_id required'}, status_code=400)

    session = _reconnect_ttyd(action_id)
    if not session:
        return JSONResponse({'error': 'tmux session not found \u2014 codex may have finished'}, status_code=404)

    return {
        'port': session['port'],
        'status': 'running',
    }


@router.post('/api/ttyd/send-keys')
async def post_ttyd_send_keys(request: Request):
    err = require_admin(request)
    if err:
        return err
    body = await request.json()
    action_id = body.get('action_id', '')
    keys = body.get('keys', '')
    if not action_id or not keys:
        return JSONResponse({'error': 'action_id and keys required'}, status_code=400)
    tmux_name = _tmux_session_name(action_id)
    if not _tmux_has_session(tmux_name):
        return JSONResponse({'error': 'no tmux session'}, status_code=404)
    subprocess.run(
        ['tmux', 'send-keys', '-t', tmux_name, keys, 'Enter'],
        capture_output=True
    )
    return {'ok': True}


@router.post('/api/ttyd/stop')
async def post_ttyd_stop(request: Request):
    err = require_admin(request)
    if err:
        return err
    body = await request.json()
    action_id = body.get('action_id', '')
    if not action_id:
        return JSONResponse({'error': 'action_id required'}, status_code=400)
    session = _stop_ttyd_only(action_id)
    if not session:
        return JSONResponse({'error': 'no active session'}, status_code=404)
    return {'status': 'disconnected'}


@router.get('/api/ttyd/status/{action_id}')
def get_ttyd_status(action_id: str, request: Request):
    err = require_admin(request)
    if err:
        return err
    return _get_ttyd_status(action_id)


@router.get('/api/ttyd/sessions')
def get_ttyd_sessions(request: Request):
    err = require_admin(request)
    if err:
        return err
    with _ttyd_lock:
        active = sum(1 for s in _ttyd_sessions.values() if s.get('ttyd_proc') and s['ttyd_proc'].poll() is None)
        total = len(_ttyd_sessions)
        tmux_names = [s.get('tmux_name', '') for s in _ttyd_sessions.values() if s.get('tmux_name')]
    tmux_alive = 0
    for tn in tmux_names:
        try:
            if subprocess.run(['tmux', 'has-session', '-t', tn], capture_output=True, timeout=3).returncode == 0:
                tmux_alive += 1
        except:
            pass
    return {'active_ttyd': active, 'active_tmux': tmux_alive, 'total': total}
