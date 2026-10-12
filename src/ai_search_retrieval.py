"""Read-only AI search v2 retrieval; the worker owns progress and persistence.

run() returns a JSON-compatible dictionary. Inject Dependencies to replace every
external operation; constructing the production dependencies performs no I/O.
"""
from __future__ import annotations

import json
import math
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ai_search_digest import load_prompt, settings as digest_settings

BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULTS = {
    'AI_SEARCH_QU_MODEL': 'MiniMax-M3',
    'AI_SEARCH_QU_TIMEOUT_S': 15,
    'AI_SEARCH_PREVIEW_TITLES': 20,
    'AI_SEARCH_LEG_LIMIT': 200,
    'AI_SEARCH_DF_CAP': 1500,
    'AI_SEARCH_KEYWORDS_MAX': 8,
    'AI_SEARCH_RERANK_MODEL': 'voyageai/rerank-3',
    'AI_SEARCH_RERANK_FALLBACK_MODEL': 'cohere/rerank-4-pro',
    'AI_SEARCH_RERANK_DOC_CHARS': 700,
    'AI_SEARCH_TOP_N': 200,
    'AI_SEARCH_INSUFFICIENT_SCORE': .65,
    'AI_SEARCH_INSUFFICIENT_MIN': 3,
    'AI_SEARCH_INPUT_MAX_TOKENS': 300000,
    'AI_SEARCH_CHARS_PER_TOKEN': 1.7,
    'AI_SEARCH_RETRIEVAL_TIMEOUT_S': 60,
    'AI_SEARCH_WINDOW_DAYS': 90,
    'AI_SEARCH_ITEM_CHARS': 4000,
}
_SUFFIX = re.compile(r'(?:介绍|用法|做法|写法|编写|教程|案例|怎么用)$')
_FRAGMENT = re.compile(r'(?:[vV]?\d+(?:\.\d+)*|pro|max)', re.I)
_STOPWORDS = frozenset('ai agent 模型 大模型 工具 产品 发布 教程 技巧 用法'.split())
_JSON_BLOCK = re.compile(r'```json\s*(.*?)```', re.S)


class RetrievalError(Exception):
    code = 'retrieval_failed'

    def __init__(self, message='找资料失败', *, retrieval=None):
        super().__init__(message)
        self.retrieval = retrieval or {}


class RetrievalTimeout(RetrievalError):
    code = 'retrieval_timeout'

    def __init__(self, *, retrieval=None):
        super().__init__('找资料超时', retrieval=retrieval)


@dataclass(frozen=True)
class Dependencies:
    """All calls accept timeout_s; DB calls also accept v1 scope/window kwargs.

    embed(texts) -> (vectors, usage); minimax(system, user) -> (text, usage,
    model); rerank(query, docs) -> OpenRouter's {results, usage} response.
    Metadata, similarities and input_lengths return dictionaries keyed by id.
    now supplies a UTC wall clock, monotonic supplies the total deadline clock.
    """
    embed: Callable
    count_phrase: Callable
    lexical_top: Callable
    vector_top: Callable
    fetch_meta: Callable
    similarities: Callable
    input_lengths: Callable
    minimax: Callable
    rerank: Callable
    now: Callable = lambda: datetime.now(timezone.utc)
    monotonic: Callable = time.monotonic
    sleep: Callable = time.sleep


def settings() -> dict:
    """Read spec §2.8 from the process environment, with typed defaults."""
    result = {}
    for key, default in DEFAULTS.items():
        value = os.environ.get(key, default)
        try:
            if isinstance(default, (int, float)):
                value = type(default)(value)
                if not math.isfinite(value) or value <= 0:
                    raise ValueError('expected positive finite configuration')
            else:
                value = str(value).strip() or default
        except (TypeError, ValueError, OverflowError):
            value = default
        result[key] = value
    return result


def _clean(value) -> str:
    return ' '.join(str(value or '').split())


def rule_keyword(question: str) -> str:
    """Strip intent with the unchanged v1 rules, preserving the whole phrase."""
    from remote_db.ai_search import _INTENT_WORDS, _LEADING_INTENT, _TRAILING_INTENT
    original = ''.join(' ' if c == '\u3000' else chr(ord(c) - 0xFEE0)
                       if 0xFF01 <= ord(c) <= 0xFF5E else c for c in question).strip()
    text = original
    while text:
        stripped = _TRAILING_INTENT.sub('', _LEADING_INTENT.sub('', text).strip()).strip()
        if stripped == text:
            break
        text = stripped
    return ' '.join(token for token in text.split() if token not in _INTENT_WORDS) or original


def clean_keywords(keyword_queries, entities, *, max_keywords=8) -> list[str]:
    """Keep first spellings/order while rejecting fragments and broad words."""
    values = list(keyword_queries) + [entity.get('name') for entity in entities if isinstance(entity, dict)]
    seen, keywords = set(), []
    for value in values:
        if not isinstance(value, str):
            continue
        text = _clean(value)
        if len(text) > 40:
            continue
        while text:
            stripped = _SUFFIX.sub('', text).strip()
            if stripped == text:
                break
            text = stripped
        canonical = re.sub(r'[\s\-‐‑‒–—]+', '', text).casefold()
        if not text or not canonical or canonical in seen or canonical in _STOPWORDS or _FRAGMENT.fullmatch(canonical):
            continue
        seen.add(canonical)
        keywords.append(text)
        if len(keywords) >= max_keywords:
            break
    return keywords


def _strings(value) -> list[str] | None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return None
    return list(dict.fromkeys(_clean(item) for item in value if _clean(item)))


def parse_understanding(raw: str, question: str) -> tuple[dict, list[str]]:
    """Only the final json fence is executable output; repair individual fields."""
    blocks = _JSON_BLOCK.findall(raw)
    if not blocks:
        raise ValueError('missing json fence')
    obj = json.loads(blocks[-1])
    if not isinstance(obj, dict):
        raise ValueError('query understanding must be an object')
    repaired = []
    kind = obj.get('type')
    if kind not in ('entity', 'broad'):
        kind = 'entity'
        repaired.append('type')
    entities = obj.get('entities')
    if not isinstance(entities, list):
        entities = []
        repaired.append('entities')
    parsed_entities = []
    for entity in entities:
        if (not isinstance(entity, dict) or not isinstance(entity.get('name'), str)
                or not _clean(entity['name']) or len(_clean(entity['name'])) > 40):
            repaired.append('entities')
            continue
        aliases = _strings(entity.get('aliases'))
        if aliases is None:
            aliases = []
            repaired.append('entities.aliases')
        parsed_entities.append({'name': _clean(entity['name']), 'aliases': aliases})
    keywords = _strings(obj.get('keyword_queries'))
    if not keywords:
        keywords = clean_keywords([rule_keyword(question)], [])
        repaired.append('keyword_queries')
    keywords = [keyword for keyword in keywords if len(keyword) <= 40]
    semantic = _strings(obj.get('semantic_queries'))
    if not semantic:
        semantic = []
        repaired.append('semantic_queries')
    semantic = list(dict.fromkeys(query[:100] for query in semantic))[:6]
    window = obj.get('time_window_days')
    if type(window) is not int or window <= 0:
        if window is not None:
            repaired.append('time_window_days')
        window = 90
    return dict(type=kind, entities=parsed_entities, keyword_queries=keywords,
                semantic_queries=semantic, time_window_days=window), list(dict.fromkeys(repaired))


def doc_text(meta: dict, chars=700) -> str:
    return (_clean(meta.get('title'))[:150] + '\n' + _clean(meta.get('ai_summary')))[:chars]


def _runtime_env():
    from env_utils import load_project_env
    return {**load_project_env(BASE_DIR), **os.environ}


def _post_json(url, payload, headers, *, timeout_s, provider, record_429=True):
    import ai_provider_guard
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                                 headers={**headers, 'Content-Type': 'application/json'}, method='POST')
    with ai_provider_guard.guarded_urlopen(req, provider=provider, source='ai_search_retrieval',
                                          timeout=timeout_s, record_429=record_429,
                                          context=ssl.create_default_context()) as response:
        return json.loads(response.read().decode('utf-8'))


def _embed(texts, *, timeout_s):
    from clustering import embedding_provider
    name, api_key, api_base = embedding_provider.resolve_runtime_provider()
    provider = embedding_provider.get_provider(name, api_key=api_key, api_base=api_base)
    provider.timeout = min(provider.timeout, timeout_s)
    vectors = provider.embed(list(texts), mode='query')
    return vectors.tolist(), {'estimated_tokens': embedding_provider.estimate_embedding_tokens(texts),
                             'provider': provider.name}


def _minimax(system, user, *, model, temperature, max_tokens, timeout_s):
    env = _runtime_env()
    key = (env.get('MINIMAX_API_KEY') or '').strip()
    if not key:
        raise RuntimeError('missing MINIMAX_API_KEY')
    base = (env.get('MINIMAX_API_BASE') or 'https://api.minimaxi.com/anthropic/v1').rstrip('/')
    data = _post_json(base + '/messages',
                      {'model': model, 'system': system, 'messages': [{'role': 'user', 'content': user}],
                       'temperature': temperature, 'max_tokens': max_tokens},
                      {'x-api-key': key, 'anthropic-version': '2023-06-01'},
                      timeout_s=timeout_s, provider='minimax')
    text = ''.join(block.get('text', '') for block in data.get('content', []) if block.get('type') == 'text')
    return text, data.get('usage') or {}, data.get('model') or model


def _rerank(query, docs, *, model, timeout_s):
    env = _runtime_env()
    key = (env.get('OPENROUTER_RERANK_API_KEY') or env.get('OPENROUTER_API_KEY') or '').strip()
    if not key:
        raise RuntimeError('missing OPENROUTER_RERANK_API_KEY or OPENROUTER_API_KEY')
    base = (env.get('OPENROUTER_API_BASE') or 'https://openrouter.ai/api/v1').rstrip('/')
    return _post_json(base + '/rerank', {'model': model, 'query': query, 'documents': docs},
                      {'Authorization': f'Bearer {key}'}, timeout_s=timeout_s, provider='openrouter', record_429=False)


def default_dependencies() -> Dependencies:
    """Bind production implementations without opening a connection or socket."""
    from remote_db import ai_search_legs as legs
    return Dependencies(embed=_embed, count_phrase=legs.count_phrase,
                        lexical_top=legs.lexical_top, vector_top=legs.vector_top,
                        fetch_meta=legs.fetch_meta, similarities=legs.similarities,
                        input_lengths=legs.input_lengths, minimax=_minimax, rerank=_rerank)


def _published(value) -> datetime | None:
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if isinstance(value, datetime):
            return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    except ValueError:
        pass
    return None


def _finite(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _score_batch(data: dict, size: int) -> list[float]:
    rows = data.get('results')
    if not isinstance(rows, list) or len(rows) != size:
        raise ValueError('incomplete rerank results')
    scores = [None] * size
    for row in rows:
        index = row.get('index')
        score = _finite(row.get('relevance_score'))
        if type(index) is not int or not 0 <= index < size or scores[index] is not None or score is None:
            raise ValueError('invalid rerank index or score')
        scores[index] = score
    return scores


def run(question: str, *, public_only=True, user_id=None, manual_owner_user_id=None,
        min_github_stars=50, progress=None, timeout_s=None, deps: Dependencies | None = None,
        config: dict | None = None) -> dict:
    """Execute spec §2.2 without writing jobs; raise errors with partial records.

    progress receives {stage: 'searching', keywords: [...]} immediately after
    query understanding/cleanup. timeout_s overrides the configured total limit.
    """
    cfg = {**settings(), **(config or {})}
    dep = deps or default_dependencies()
    started = dep.monotonic()
    limit = float(cfg['AI_SEARCH_RETRIEVAL_TIMEOUT_S'] if timeout_s is None else timeout_s)
    scope = dict(public_only=public_only, user_id=user_id, manual_owner_user_id=manual_owner_user_id,
                 min_github_stars=min_github_stars)
    record = {'scope': scope, 'stage': 'understanding', 'preview_titles': [], 'understanding': {},
              'keywords': [], 'semantic_queries': [], 'legs': [], 'merged_count': 0,
              'window_days': cfg['AI_SEARCH_WINDOW_DAYS'], 'df_cap': cfg['AI_SEARCH_DF_CAP'],
              'rerank': {}, 'selection': {},
              'timings_s': {}, 'usage': []}
    timing_lock = threading.Lock()

    def check():
        if dep.monotonic() - started >= limit:
            record['timings_s']['total'] = round(dep.monotonic() - started, 6)
            raise RetrievalTimeout(retrieval=record)

    def call(name, fn, *args, timeout_cap=None, **kwargs):
        check()
        before = dep.monotonic()
        remaining = limit - (before - started)
        if timeout_cap is not None and timeout_cap <= 0:
            raise TimeoutError('AI search database leg has no time remaining')
        kwargs['timeout_s'] = min(remaining, timeout_cap) if timeout_cap is not None else remaining
        try:
            value = fn(*args, **kwargs)
        except Exception:
            check()
            raise
        finally:
            with timing_lock:
                record['timings_s'][name] = round(dep.monotonic() - before, 6)
        check()
        return value

    def error(exc):
        return f'{type(exc).__name__}: {exc}'[:500]

    def embed(texts, name):
        vectors, usage = call(name, dep.embed, texts, timeout_cap=30)
        if len(vectors) != len(texts) or any(not vector or any(_finite(v) is None for v in vector) for vector in vectors):
            raise ValueError('invalid query embeddings')
        record['usage'].append({'stage': name, 'usage': usage})
        return vectors

    def vector_leg(query, vector, window):
        leg = dict(type='vector', query=query, hits=0, taken=0, skipped=False, error=None, elapsed_s=0)
        before = dep.monotonic()
        rows = []
        try:
            rows = call('vector:' + query, dep.vector_top, vector, k=cfg['AI_SEARCH_LEG_LIMIT'],
                        window_days=window, **scope)
            leg.update(hits=len(rows), taken=len(rows))
        except RetrievalTimeout as exc:
            leg['error'] = error(exc)
            raise
        except Exception as exc:
            leg['error'] = error(exc)
        finally:
            leg['elapsed_s'] = round(dep.monotonic() - before, 6)
            record['legs'].append(leg)
        return rows, leg

    # ① Original vector retrieval doubles as the preview and the first vector leg.
    check()
    preview_started = dep.monotonic()
    original_vector, original_rows, original_leg = None, [], None
    embedding_ok = True
    try:
        original_vector = embed([question], 'embedding_original')[0]
    except RetrievalTimeout:
        raise
    except Exception as exc:
        embedding_ok = False
        record['embedding_error'] = error(exc)
    if original_vector is not None:
        # Understanding failure must still have a full 90-day original leg,
        # even when the configured normal window is shorter.
        preview_window = max(90, cfg['AI_SEARCH_WINDOW_DAYS'])
        original_rows, original_leg = vector_leg(question, original_vector, preview_window)
        preview_ids = [row['id'] for row in original_rows[:cfg['AI_SEARCH_PREVIEW_TITLES']]]
        if preview_ids:
            try:
                preview_meta = call('preview_metadata', dep.fetch_meta, preview_ids,
                                    summary_chars=cfg['AI_SEARCH_RERANK_DOC_CHARS'] + 300,
                                    window_days=preview_window, **scope)
                record['preview_titles'] = [_clean(preview_meta[i].get('title')) for i in preview_ids if i in preview_meta]
            except RetrievalTimeout:
                raise
            except Exception as exc:
                record['preview_error'] = error(exc)
    record['timings_s']['preview'] = round(dep.monotonic() - preview_started, 6)

    # ② Prompt header is never sent to MiniMax. Keep the input label verbatim.
    check()
    before = dep.monotonic()
    version, system = load_prompt('prompts/18_ai_search_query.md')
    listed = '\n'.join('- ' + title for title in record['preview_titles']) or '（无）'
    user = f'问题：{question}\n\n站内参考（用原问题初步检索到的前 20 条标题）：\n{listed}'
    understanding = record['understanding']
    understanding.update(prompt_version=version, input=user, output=None, usage={}, fallback=False,
                         field_fallbacks=[], elapsed_s=0, model=cfg['AI_SEARCH_QU_MODEL'])
    try:
        raw, usage, model = call('understanding', dep.minimax, system, user, model=cfg['AI_SEARCH_QU_MODEL'],
                                temperature=.01, max_tokens=4096, timeout_cap=cfg['AI_SEARCH_QU_TIMEOUT_S'])
        understanding.update(usage=usage, model=model, raw_text=raw)
        record['usage'].append({'stage': 'understanding', 'model': model, 'usage': usage})
        output, repaired = parse_understanding(raw, question)
        understanding['field_fallbacks'] = repaired
    except RetrievalTimeout:
        raise
    except Exception as exc:
        output = dict(type='entity', entities=[], keyword_queries=[rule_keyword(question)],
                      semantic_queries=[], time_window_days=90)
        understanding.update(fallback=True, error=error(exc))
    finally:
        understanding['elapsed_s'] = round(dep.monotonic() - before, 6)
    understanding['output'] = output
    check()
    cleanup_started = dep.monotonic()
    keywords = clean_keywords(output['keyword_queries'], output['entities'], max_keywords=cfg['AI_SEARCH_KEYWORDS_MAX'])
    semantic = output['semantic_queries']
    window = 90 if understanding['fallback'] else output['time_window_days'] \
        if output['time_window_days'] < 90 else cfg['AI_SEARCH_WINDOW_DAYS']
    record.update(stage='searching', keywords=keywords, semantic_queries=semantic, window_days=window)
    record['timings_s']['keyword_cleanup'] = round(dep.monotonic() - cleanup_started, 6)
    if progress is not None:
        progress({'stage': 'searching', 'keywords': list(keywords)})
    check()

    # ③ Each lexical route succeeds or fails independently; a DF skip is success.
    lexical_rows, successful_legs = [], 0
    before = dep.monotonic()
    for keyword in keywords:
        check()
        leg = dict(type='lexical', query=keyword, hits=0, taken=0, skipped=False, error=None, elapsed_s=0)
        leg_started = dep.monotonic()
        try:
            hits = call('count:' + keyword, dep.count_phrase, keyword, timeout_cap=6,
                        cap=cfg['AI_SEARCH_DF_CAP'] + 1, window_days=window, **scope)
            leg['hits'] = hits
            leg_remaining = 6 - (dep.monotonic() - leg_started)
            if leg_remaining <= 0:
                raise TimeoutError('AI search database leg has no time remaining')
            if hits > cfg['AI_SEARCH_DF_CAP']:
                leg.update(skipped=True, skip_reason='df_cap', hits_capped=True)
            else:
                rows = call('lexical:' + keyword, dep.lexical_top, keyword, k=cfg['AI_SEARCH_LEG_LIMIT'],
                            timeout_cap=leg_remaining, window_days=window, **scope)
                if dep.monotonic() - leg_started >= 6:
                    raise TimeoutError('AI search database leg has no time remaining')
                lexical_rows.append((keyword, rows))
                leg['taken'] = len(rows)
            successful_legs += 1
        except RetrievalTimeout as exc:
            leg['error'] = error(exc)
            raise
        except Exception as exc:
            leg['error'] = error(exc)
        finally:
            leg['elapsed_s'] = round(dep.monotonic() - leg_started, 6)
            record['legs'].append(leg)
    record['timings_s']['lexical'] = round(dep.monotonic() - before, 6)

    # ④ Semantic sentences are embedded together. Embedding failure discards all
    # vector candidates, including the previously retrieved original-query leg.
    check()
    vectors = [(question, original_rows, original_leg)] if original_leg is not None else []
    before = dep.monotonic()
    if embedding_ok and semantic:
        try:
            semantic_vectors = embed(semantic, 'embedding_semantic')
        except RetrievalTimeout:
            raise
        except Exception as exc:
            embedding_ok = False
            record['embedding_error'] = error(exc)
        else:
            for query, vector in zip(semantic, semantic_vectors):
                rows, leg = vector_leg(query, vector, window)
                vectors.append((query, rows, leg))
    if not embedding_ok:
        for _, _, leg in vectors:
            leg.update(discarded=True, discard_reason='embedding_failed')
        vectors = []
    vector_ok = any(leg['error'] is None for _, _, leg in vectors)
    successful_legs += sum(leg['error'] is None for _, _, leg in vectors)
    record['timings_s']['vector'] = round(dep.monotonic() - before, 6)
    record['vector_ok'] = vector_ok
    if not successful_legs:
        raise RetrievalError(retrieval=record)

    # ⑤ Merge in route order and filter the original preview's wider window too.
    check()
    merge_started = dep.monotonic()
    union = {}
    for kind, legs in [('lexical', lexical_rows), ('vector', [(q, rows) for q, rows, _ in vectors])]:
        for query, rows in legs:
            for rank, row in enumerate(rows, 1):
                entry = union.setdefault(row['id'], {'legs': []})
                entry['legs'].append(f'{kind}:{query}#{rank}')
    record['merged_count'] = len(union)
    meta = {}
    if union:
        try:
            meta = call('metadata', dep.fetch_meta, list(union),
                        summary_chars=cfg['AI_SEARCH_RERANK_DOC_CHARS'] + 300,
                        window_days=window, **scope)
        except RetrievalTimeout:
            raise
        except Exception as exc:
            raise RetrievalError('找资料失败: 无法读取候选资料', retrieval=record) from exc
    ids = [i for i in union if i in meta]
    if window < cfg['AI_SEARCH_WINDOW_DAYS'] or window < 90:
        now = dep.now()
        now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
        cutoff = now - timedelta(days=window)
        ids = [i for i in ids if (published := _published(meta[i].get('published_at'))) is not None and published >= cutoff]
    record['filtered_count'] = len(ids)
    record['timings_s']['merge_filter'] = round(dep.monotonic() - merge_started, 6)

    # Original-query similarity must include lexical-only matches as well.
    check()
    sims = {}
    if ids and original_vector is not None:
        try:
            similarities = call('similarities', dep.similarities, ids, original_vector,
                                window_days=window, **scope)
            sims = {i: sim for i, value in similarities.items() if (sim := _finite(value)) is not None}
        except RetrievalTimeout:
            raise
        except Exception as exc:
            record['similarity_error'] = error(exc)
            sims = {row['id']: value for row in original_rows
                    if (value := _finite(row.get('sim'))) is not None and row['id'] in ids}

    # ⑥ Retry whole-model attempts: every candidate has a comparable model score.
    check()
    rerank_started = dep.monotonic()
    docs = [doc_text(meta[i], cfg['AI_SEARCH_RERANK_DOC_CHARS']) for i in ids]
    used_model, scores = None, [None] * len(ids)
    attempts = []
    attempt_summaries = []
    record['rerank'] = {'query': question, 'count': len(ids), 'attempts': attempts}

    def rerank_attempt(model):
        attempt = {'model': model, 'error': None, 'waited_s': 0, 'batches': []}
        attempts.append(attempt)
        chunks = [(start, docs[start:start + 250]) for start in range(0, len(docs), 250)]
        all_scores = [None] * len(docs)

        def one(chunk):
            start, documents = chunk
            before = dep.monotonic()
            data = call(f'rerank:{model}:{len(attempts)}:{start}', dep.rerank, question, documents,
                        model=model, timeout_cap=15)
            batch = {'start': start, 'count': len(documents), 'usage': data.get('usage') or {},
                     'elapsed_s': round(dep.monotonic() - before, 6)}
            # Preserve provider usage even if its score payload is malformed.
            with timing_lock:
                attempt['batches'].append(batch)
                record['usage'].append({'stage': 'rerank', 'model': model, **batch})
            return start, _score_batch(data, len(documents))

        with ThreadPoolExecutor(max_workers=4) as pool:
            for start, batch_scores in pool.map(one, chunks):
                all_scores[start:start + len(batch_scores)] = batch_scores
        return all_scores

    if ids:
        models = [cfg['AI_SEARCH_RERANK_MODEL'], cfg['AI_SEARCH_RERANK_FALLBACK_MODEL']]
        for attempt_number, model in enumerate(models):
            check()
            before = dep.monotonic()
            try:
                scores = rerank_attempt(model)
                used_model = model
                attempt_summaries.append(f'{model}: ok')
                break
            except RetrievalTimeout:
                raise
            except Exception as exc:
                attempts[-1]['error'] = error(exc)
                # Exception messages can contain the query, request URL or credentials.
                summary = type(exc).__name__
                if isinstance(exc, urllib.error.HTTPError):
                    summary += f' HTTP {exc.code}'
                attempt_summaries.append(f'{model}: {summary}')
                wait_s = None
                if attempt_number == 0:
                    if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                        wait_s = 8
                        try:
                            retry_after = float((exc.headers or {}).get('Retry-After', ''))
                        except (TypeError, ValueError):
                            pass
                        else:
                            if math.isfinite(retry_after) and retry_after > 0:
                                wait_s = min(retry_after, 15)
                        remaining = limit - (dep.monotonic() - started)
                        if remaining < wait_s + 20:
                            wait_s = None
                    elif dep.monotonic() - before <= 5:
                        wait_s = 1
                if wait_s is not None:
                    wait_started = dep.monotonic()
                    dep.sleep(wait_s)
                    attempts[-1]['waited_s'] = round(dep.monotonic() - wait_started, 6)
                    check()
                    models.insert(1, model)
        if used_model is None:
            used_model = 'similarity'
        if used_model != cfg['AI_SEARCH_RERANK_MODEL']:
            print(f'[warn] ai_search rerank fell back to {used_model}: ' + '; '.join(attempt_summaries))
    above = sum(score is not None and score >= cfg['AI_SEARCH_INSUFFICIENT_SCORE'] for score in scores)
    valid_scores = [score for score in scores if score is not None]
    distribution = {'min': min(valid_scores) if valid_scores else None,
                    'max': max(valid_scores) if valid_scores else None,
                    'mean': sum(valid_scores) / len(valid_scores) if valid_scores else None,
                    'buckets': {f'{n / 10:.1f}-{(n + 1) / 10:.1f}':
                                sum(n / 10 <= score < (n + 1) / 10 or n == 9 and score == 1 for score in valid_scores)
                                for n in range(10)}}
    record['rerank'] = dict(model=used_model, count=len(ids), query=question, attempts=attempts,
                            elapsed_s=round(dep.monotonic() - rerank_started, 6),
                            fallback=used_model is not None and used_model != cfg['AI_SEARCH_RERANK_MODEL'],
                            above_threshold=above, score_distribution=distribution,
                            threshold=cfg['AI_SEARCH_INSUFFICIENT_SCORE'], min_count=cfg['AI_SEARCH_INSUFFICIENT_MIN'])
    record['timings_s']['rerank'] = record['rerank']['elapsed_s']

    # ⑦ Sort ties by original similarity, then remove complete tail entries.
    check()
    selection_started = dep.monotonic()
    score_of = dict(zip(ids, scores))
    def ordering(i):
        sim = sims.get(i)
        return (score_of[i] if score_of[i] is not None else -math.inf,
                sim is not None, sim if sim is not None else -math.inf)
    chosen = sorted(ids, key=ordering, reverse=True)[:cfg['AI_SEARCH_TOP_N']]
    top_n_count = len(chosen)
    lengths = {}
    if chosen:
        try:
            lengths = call('input_lengths', dep.input_lengths, chosen, item_chars=cfg['AI_SEARCH_ITEM_CHARS'],
                           window_days=window, **scope)
        except RetrievalTimeout:
            raise
        except Exception as exc:
            raise RetrievalError('找资料失败: 无法估算写作输入', retrieval=record) from exc
    missing_lengths = [i for i in chosen if i not in lengths]
    chosen = [i for i in chosen if i in lengths]
    _, digest_system = load_prompt(digest_settings()['AI_SEARCH_PROMPT_PATH'])
    today = dep.now().astimezone(timezone.utc).date().isoformat()
    def estimate():
        prefix = f'用户问题：{question}\n\n今天：{today}\n\n候选条目（共 {len(chosen)} 条）：\n\n'
        chars = sum(lengths[i] for i in chosen) + len(prefix) + 2 * max(0, len(chosen) - 1) + len(digest_system)
        return math.ceil(chars / cfg['AI_SEARCH_CHARS_PER_TOKEN'])
    dropped = 0
    while chosen and estimate() > cfg['AI_SEARCH_INPUT_MAX_TOKENS']:
        check()
        chosen.pop()
        dropped += 1
    record['selection'] = dict(top_n=cfg['AI_SEARCH_TOP_N'], top_n_count=top_n_count,
                               dropped_count=dropped, unavailable_ids=missing_lengths,
                               estimated_tokens=estimate(), system_prompt_chars=len(digest_system),
                               input_max_tokens=cfg['AI_SEARCH_INPUT_MAX_TOKENS'], retained_count=len(chosen))
    record['timings_s']['selection'] = round(dep.monotonic() - selection_started, 6)

    # ⑧ The threshold is calibrated for primary Voyage scores only.
    check()
    insufficient_started = dep.monotonic()
    primary_voyage = used_model == cfg['AI_SEARCH_RERANK_MODEL'] and str(used_model).startswith('voyageai/')
    record['rerank']['threshold_applied'] = primary_voyage
    insufficient = not chosen or primary_voyage and above < cfg['AI_SEARCH_INSUFFICIENT_MIN']
    if insufficient:
        chosen = chosen[:5]
    candidate_meta = [dict(id=i, score=score_of[i], sim=sims.get(i), legs=union[i]['legs']) for i in chosen]
    record['selection']['final_count'] = len(chosen)
    record['timings_s']['insufficient'] = round(dep.monotonic() - insufficient_started, 6)
    record['timings_s']['total'] = round(dep.monotonic() - started, 6)
    check()
    return dict(outcome='insufficient' if insufficient else 'ok', candidate_ids=chosen,
                candidate_meta=candidate_meta, retrieval=record,
                lexical_hits=sum(len(rows) for _, rows in lexical_rows),
                vector_hits=sum(len(rows) for _, rows, _ in vectors), vector_ok=vector_ok,
                window_days=window, query_lexical=' | '.join(keywords))
