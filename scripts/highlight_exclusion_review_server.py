#!/usr/bin/env python3
"""Local review server for excluded Highlights clusters backed by Supabase."""
from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import remote_db  # noqa: E402


def _load_env_file(path: str | None) -> None:
    if not path:
        return
    env_path = Path(path).expanduser()
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Highlight Exclusion Review</title>
  <style>
    body { margin:0; font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:#172033; background:#f7f8fb; }
    header { height:56px; display:flex; align-items:center; gap:16px; padding:0 20px; border-bottom:1px solid #e4e7ee; background:#fff; }
    header strong { font-size:18px; white-space:nowrap; }
    header select { width:auto; min-width:140px; margin:0; }
    main { display:grid; grid-template-columns:360px minmax(480px,1fr) 320px; height:calc(100vh - 57px); }
    aside, section { overflow:auto; }
    aside { border-right:1px solid #e4e7ee; background:#fff; }
    .row { position:relative; padding:14px 16px; border-bottom:1px solid #edf0f5; cursor:pointer; }
    .row:hover { background:#f8fbff; }
    .row.active { border-left:4px solid #2563eb; background:#eef4ff; padding-left:12px; }
    .row.reviewed::after { content:""; position:absolute; top:14px; right:14px; width:8px; height:8px; border-radius:999px; background:#12b76a; }
    .title { font-weight:700; color:#111827; }
    .meta { color:#667085; font-size:12px; margin-top:6px; }
    .content { padding:24px 28px; }
    .card { background:#fff; border:1px solid #e2e6ef; border-radius:8px; padding:18px; margin-bottom:16px; }
    .doc { border-top:1px solid #edf0f5; padding:14px 0; }
    .doc:first-child { border-top:0; }
    a { color:#0b5bd3; text-decoration:none; }
    .panel { background:#fff; border-left:1px solid #e4e7ee; padding:24px 18px; }
    .buttons { display:grid; gap:10px; margin-top:14px; }
    button { border:1px solid #cfd7e6; background:#fff; border-radius:8px; padding:12px; font-weight:700; cursor:pointer; transition:box-shadow .15s ease, transform .15s ease, border-color .15s ease; }
    button:hover { box-shadow:0 5px 18px rgba(15,23,42,.08); transform:translateY(-1px); }
    button:disabled { cursor:wait; opacity:.62; transform:none; box-shadow:none; }
    button.primary { background:#1458e8; color:#fff; border-color:#1458e8; }
    button.danger { color:#b42318; border-color:#f3b6b0; }
    .verdict-btn { display:flex; align-items:center; justify-content:space-between; gap:10px; }
    .verdict-btn.selected { box-shadow:0 0 0 3px rgba(20,88,232,.16); border-color:#1458e8; }
    textarea, select, input { width:100%; box-sizing:border-box; border:1px solid #cfd7e6; border-radius:8px; padding:10px; margin-top:8px; }
    label { display:block; font-weight:700; margin-top:16px; color:#344054; }
    .pill { display:inline-block; padding:2px 8px; border-radius:999px; background:#eef2f7; color:#475467; font-size:12px; }
    .pill.reviewed { background:#e8f7ef; color:#067647; }
    .progress { margin-left:auto; color:#344054; font-weight:700; white-space:nowrap; }
    .shortcut-hint { display:flex; flex-wrap:wrap; gap:8px; margin:6px 0 12px; color:#667085; font-size:12px; }
    kbd { display:inline-flex; min-width:20px; height:20px; align-items:center; justify-content:center; border:1px solid #cfd7e6; border-bottom-width:2px; border-radius:5px; background:#fff; color:#344054; font:12px/1 ui-monospace,SFMono-Regular,Menlo,monospace; }
    .save-status { min-height:22px; margin-top:14px; border-radius:8px; padding:10px 12px; color:#667085; background:#f8fafc; border:1px solid #edf0f5; }
    .save-status.saving { color:#175cd3; background:#eff6ff; border-color:#bfdbfe; }
    .save-status.saved { color:#067647; background:#ecfdf3; border-color:#abefc6; }
    .save-status.error { color:#b42318; background:#fef3f2; border-color:#fecdca; }
    .toast { position:fixed; top:72px; right:24px; z-index:10; max-width:320px; padding:12px 14px; border-radius:10px; background:#101828; color:#fff; box-shadow:0 14px 36px rgba(15,23,42,.22); opacity:0; transform:translateY(-8px); transition:opacity .16s ease, transform .16s ease; pointer-events:none; }
    .toast.show { opacity:1; transform:translateY(0); }
  </style>
</head>
<body>
  <header>
    <strong>未进精选 Review</strong>
    <select id="decision"><option value="excluded">excluded</option><option value="pending">pending</option></select>
    <select id="clusterVerdict">
      <option value="drop">drop</option>
      <option value="risk_borderline">risk_borderline</option>
      <option value="">全部</option>
    </select>
    <select id="recentDays">
      <option value="3">最近3天</option>
      <option value="7">最近7天</option>
      <option value="30">最近30天</option>
      <option value="">全部</option>
    </select>
    <select id="rowLimit">
      <option value="200">200条</option>
      <option value="500">500条</option>
    </select>
    <span id="status" class="progress"></span>
  </header>
  <div id="toast" class="toast"></div>
  <main>
    <aside id="list"></aside>
    <section class="content">
      <div id="detail" class="card">选择左侧 cluster。</div>
      <div id="docs" class="card"></div>
    </section>
    <section class="panel">
      <h3>人工复盘</h3>
      <label>判断</label>
      <div class="shortcut-hint"><span><kbd>F</kbd> 或 <kbd>1</kbd> 应该进</span><span><kbd>D</kbd> 或 <kbd>2</kbd> 确认不进</span><span><kbd>U</kbd> 或 <kbd>3</kbd> 不确定</span></div>
      <div class="buttons">
        <button type="button" class="verdict-btn primary" data-verdict="should_feature" onclick="saveReview('should_feature')"><span>应该进精选</span><kbd>F</kbd></button>
        <button type="button" class="verdict-btn" data-verdict="confirmed_drop" onclick="saveReview('confirmed_drop')"><span>确认不进</span><kbd>D</kbd></button>
        <button type="button" class="verdict-btn danger" data-verdict="unsure" onclick="saveReview('unsure')"><span>不确定</span><kbd>U</kbd></button>
      </div>
      <label>主要问题</label>
      <select id="errorKind">
        <option value="">无</option>
        <option value="value_path">价值路径判断错</option>
        <option value="uncertainty">边界风险判断错</option>
        <option value="relevance">相关性判断错</option>
        <option value="evidence">证据不足/误判</option>
        <option value="other">其他</option>
      </select>
      <label>说明</label>
      <textarea id="notes" rows="7" placeholder="一句话说明为什么误杀，或为什么确认不进。"></textarea>
      <div id="saveStatus" class="save-status">尚未标注当前 cluster。</div>
    </section>
  </main>
<script>
let rows = [];
let current = null;
let saving = false;
const verdictLabels = {
  should_feature: '应该进精选',
  confirmed_drop: '确认不进',
  unsure: '不确定'
};
async function loadRows() {
  const decision = document.getElementById('decision').value;
  const clusterVerdict = document.getElementById('clusterVerdict').value;
  const recentDays = document.getElementById('recentDays').value;
  const rowLimit = document.getElementById('rowLimit').value;
  const res = await fetch(`/api/decisions?decision=${encodeURIComponent(decision)}&cluster_verdict=${encodeURIComponent(clusterVerdict)}&recent_days=${encodeURIComponent(recentDays)}&limit=${encodeURIComponent(rowLimit)}`);
  rows = await res.json();
  current = null;
  updateProgress();
  renderList();
  if (rows.length) selectRow(rows[0].cluster_id);
}
function hasReview(row) {
  return Boolean(row && row.latest_human_verdict);
}
function reviewLabel(verdict) {
  return verdictLabels[verdict] || '未标注';
}
function updateProgress() {
  const done = rows.filter(hasReview).length;
  document.getElementById('status').textContent = `${done}/${rows.length} 已标`;
}
function renderList() {
  updateProgress();
  document.getElementById('list').innerHTML = rows.map(r => `
    <div class="row ${current && current.cluster_id === r.cluster_id ? 'active' : ''} ${hasReview(r) ? 'reviewed' : ''}" onclick="selectRow(${r.cluster_id})">
      <div class="title">${escapeHtml(r.ai_title || r.snapshot_json?.ai_title || `Cluster ${r.cluster_id}`)}</div>
      <div class="meta"><span class="pill">${r.cluster_verdict}</span> ${r.doc_count || ''} docs ${hasReview(r) ? `<span class="pill reviewed">已标：${escapeHtml(reviewLabel(r.latest_human_verdict))}</span>` : ''}</div>
    </div>`).join('');
}
async function selectRow(id, options = {}) {
  current = rows.find(r => r.cluster_id === id);
  if (!current) return;
  renderList();
  document.getElementById('errorKind').value = current.latest_error_kind || '';
  document.getElementById('notes').value = current.latest_notes || '';
  setSaveStatus(options.statusKind || (hasReview(current) ? 'saved' : ''), options.message || (hasReview(current) ? `最新标注：${reviewLabel(current.latest_human_verdict)}` : '尚未标注当前 cluster。'));
  renderButtons();
  document.getElementById('detail').innerHTML = `
    <h2>${escapeHtml(current.ai_title || current.snapshot_json?.ai_title || `Cluster ${id}`)}</h2>
    <p>${escapeHtml(current.ai_summary || current.snapshot_json?.ai_summary || '')}</p>
    <p><span class="pill">${current.decision}</span> <span class="pill">${current.cluster_verdict}</span></p>
    <p><strong>机器原因：</strong>${escapeHtml(current.reason || '')}</p>`;
  const docs = await (await fetch(`/api/docs?cluster_id=${id}`)).json();
  document.getElementById('docs').innerHTML = `<h3>原始 docs (${docs.length})</h3>` + docs.map(d => {
    const url = safeUrl(d.url || '');
    const title = escapeHtml(d.title || d.id);
    return `
    <div class="doc">
      ${url ? `<a href="${escapeAttr(url)}" target="_blank" rel="noopener noreferrer">${title}</a>` : `<strong>${title}</strong>`}
      <div class="meta">${escapeHtml(d.platform || '')} · ${escapeHtml(d.source || d.author_name || '')}</div>
      <p>${escapeHtml(d.ai_summary || (d.content || '').slice(0, 360))}</p>
    </div>`;
  }).join('');
}
function renderButtons() {
  document.querySelectorAll('[data-verdict]').forEach(btn => {
    const selected = current && btn.dataset.verdict === current.latest_human_verdict;
    btn.classList.toggle('selected', Boolean(selected));
    btn.disabled = saving;
  });
}
function setSaveStatus(kind, text) {
  const el = document.getElementById('saveStatus');
  el.className = `save-status ${kind || ''}`;
  el.textContent = text;
}
function showToast(message) {
  const toast = document.getElementById('toast');
  toast.textContent = message;
  toast.classList.add('show');
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => toast.classList.remove('show'), 1800);
}
function nextUnreviewed(afterClusterId) {
  const start = rows.findIndex(r => r.cluster_id === afterClusterId);
  if (start < 0) return rows.find(r => !hasReview(r)) || null;
  for (let i = 1; i <= rows.length; i++) {
    const candidate = rows[(start + i) % rows.length];
    if (!hasReview(candidate)) return candidate;
  }
  return null;
}
async function saveReview(verdict) {
  if (!current || saving) return;
  saving = true;
  renderButtons();
  setSaveStatus('saving', `保存中：${reviewLabel(verdict)}...`);
  const payload = {
    cluster_id: current.cluster_id,
    machine_decision_at: current.decided_at,
    human_verdict: verdict,
    error_kind: document.getElementById('errorKind').value,
    notes: document.getElementById('notes').value
  };
  try {
    const res = await fetch('/api/review', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    let body = {};
    try { body = await res.json(); } catch (_) {}
    if (!res.ok) throw new Error(body.error || '保存失败');
    current.latest_human_verdict = verdict;
    current.latest_error_kind = payload.error_kind || '';
    current.latest_notes = payload.notes || '';
    current.latest_reviewed_at = body.reviewed_at || new Date().toISOString();
    renderList();
    setSaveStatus('saved', `已保存：${reviewLabel(verdict)}。`);
    showToast(`已保存：${reviewLabel(verdict)}`);
    const next = nextUnreviewed(current.cluster_id);
    if (next) {
      setTimeout(() => selectRow(next.cluster_id, {statusKind: 'saved', message: `上一条已保存：${reviewLabel(verdict)}，已跳到下一条未标。`}), 360);
    } else {
      setSaveStatus('saved', `已保存：${reviewLabel(verdict)}。当前筛选已全部标注完成。`);
      showToast('当前筛选已全部标注完成');
    }
  } catch (err) {
    setSaveStatus('error', `保存失败：${err.message || err}`);
    showToast('保存失败，请重试');
  } finally {
    saving = false;
    renderButtons();
  }
}
function escapeHtml(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function escapeAttr(s) { return escapeHtml(s).replace(/"/g, '&quot;'); }
function safeUrl(raw) {
  try {
    const url = new URL(String(raw || ''), window.location.href);
    return (url.protocol === 'http:' || url.protocol === 'https:') ? url.href : '';
  } catch (_) {
    return '';
  }
}
document.getElementById('decision').addEventListener('change', loadRows);
document.getElementById('clusterVerdict').addEventListener('change', loadRows);
document.getElementById('recentDays').addEventListener('change', loadRows);
document.getElementById('rowLimit').addEventListener('change', loadRows);
document.addEventListener('keydown', e => {
  const target = e.target || {};
  const tag = (target.tagName || '').toUpperCase();
  if (tag === 'TEXTAREA' || tag === 'INPUT' || tag === 'SELECT' || target.isContentEditable) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const key = e.key.toLowerCase();
  if (key === 'f' || key === '1') { e.preventDefault(); saveReview('should_feature'); }
  if (key === 'd' || key === '2') { e.preventDefault(); saveReview('confirmed_drop'); }
  if (key === 'u' || key === '3') { e.preventDefault(); saveReview('unsure'); }
});
loadRows();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def _json(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/decisions":
                rows = remote_db.query_highlight_cluster_decisions_remote(
                    decision=(query.get("decision") or ["excluded"])[0],
                    cluster_verdict=(query.get("cluster_verdict") or [None])[0] or None,
                    recent_days=int((query.get("recent_days") or ["0"])[0] or "0"),
                    limit=int((query.get("limit") or ["100"])[0]),
                )
                self._json(rows)
                return
            if parsed.path == "/api/docs":
                rows = remote_db.query_highlight_review_docs_remote(int((query.get("cluster_id") or ["0"])[0]))
                self._json(rows)
                return
        except Exception as exc:
            self._json({"error": str(exc)}, status=500)
            return
        self._json({"error": "not_found"}, status=404)

    def do_POST(self):
        if urlparse(self.path).path != "/api/review":
            self._json({"error": "not_found"}, status=404)
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            remote_db.write_highlight_exclusion_review_remote(
                None,
                cluster_id=int(payload["cluster_id"]),
                machine_decision_at=payload.get("machine_decision_at"),
                human_verdict=str(payload["human_verdict"]),
                error_kind=payload.get("error_kind"),
                notes=payload.get("notes"),
                reviewer=os.environ.get("USER") or "local",
            )
            self._json({"ok": True})
        except Exception as exc:
            self._json({"error": str(exc)}, status=500)

    def log_message(self, fmt, *args):
        print(fmt % args)


def main() -> int:
    parser = argparse.ArgumentParser(description="Review excluded Highlights clusters")
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8768)
    args = parser.parse_args()
    _load_env_file(args.env_file)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Highlight exclusion review server: http://{args.host}:{args.port}/", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
