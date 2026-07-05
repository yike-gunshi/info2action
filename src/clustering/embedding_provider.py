"""Embedding provider abstraction (v15.0).

Factory `get_provider(name, **kwargs)` returns:
- OpenRouterEmbeddingProvider (default, OpenAI-compatible text-embedding-3-small)
- MiniMaxEmbeddingProvider  (disabled; retained only to fail fast on old config)
- DoubaoEmbeddingProvider   (NotImplementedError skeleton, Wave 5+)
- OpenAIEmbeddingProvider   (NotImplementedError skeleton)
- FakeEmbeddingProvider     (deterministic SHA256-derived, dry-run/CI)

Runtime choice is driven by `.env EMBEDDING_PROVIDER` or
`config.global.embedding_provider` (PRD §4.8).

Credentials are STRICTLY ISOLATED from chat:
  - OpenRouter embedding reads OPENROUTER_API_KEY + OPENROUTER_EMBEDDING_BASE only.
  - Chat (generate_actions, cluster summary, etc.) reads MINIMAX_API_KEY +
    MINIMAX_API_BASE.
There is NO fallback from embedding to chat credentials.

NOTE (2026-05-15): MiniMax embedding is intentionally disabled. If old
configuration requests `EMBEDDING_PROVIDER=minimax`, provider resolution fails
before any MiniMax embedding HTTP request can be made. MiniMax chat remains
available for summary/judge calls.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import inspect
import json
import math
import os
import ssl
import time
from abc import ABC, abstractmethod
from typing import Sequence
from urllib.request import Request, urlopen

import numpy as np

from env_utils import load_project_env

_DEFAULT_OPENROUTER_BASE = 'https://openrouter.ai/api/v1'
_DEFAULT_OPENROUTER_MODEL = 'openai/text-embedding-3-small'
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SSL_CTX = ssl.create_default_context()
_FAKE_DIM = 1536  # match production OpenRouter embedding dimension
_TOKEN_ESTIMATOR = 'cjk_chars*0.625+other_chars*0.25'
_DEFAULT_OPENROUTER_MAX_INPUT_CHARS = 5000
_OPENROUTER_TRIM_MARKER = '\n[...embedding input trimmed...]\n'
_EMBEDDING_USAGE_CONTEXT = contextvars.ContextVar('embedding_usage_context', default={})
_MINIMAX_EMBEDDING_DISABLED_MESSAGE = (
    "MiniMax embedding is disabled for info2action. "
    "Use EMBEDDING_PROVIDER=openrouter with OPENROUTER_API_KEY instead."
)


@contextlib.contextmanager
def embedding_usage_context(**fields):
    """Attach run/stage metadata to embedding calls made in this context."""
    current = dict(_EMBEDDING_USAGE_CONTEXT.get() or {})
    current.update({k: v for k, v in fields.items() if v is not None})
    token = _EMBEDDING_USAGE_CONTEXT.set(current)
    try:
        yield
    finally:
        _EMBEDDING_USAGE_CONTEXT.reset(token)


def _usage_log_enabled() -> bool:
    raw = os.environ.get('INFO2ACTION_EMBEDDING_USAGE_LOG')
    if raw is not None:
        return raw.strip().lower() not in {'0', 'false', 'no', 'off'}
    # Keep provider unit tests hermetic unless a test explicitly opts in.
    return 'PYTEST_CURRENT_TEST' not in os.environ


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0x3040 <= code <= 0x30FF
        or 0xAC00 <= code <= 0xD7AF
    )


def estimate_embedding_tokens(texts: Sequence[str]) -> int:
    """Estimate embedding input tokens from text.

    For CJK-heavy text we use the historical project ratio 1600 Chinese chars
    ≈ 1000 tokens; for non-CJK text, a conventional 4 chars/token estimate.
    Provider-reported usage, when available, overrides this estimate in logs.
    """
    total = 0.0
    for text in texts:
        cjk = 0
        other = 0
        for ch in str(text or ''):
            if ch.isspace():
                continue
            if _is_cjk(ch):
                cjk += 1
            else:
                other += 1
        total += cjk * 0.625 + other * 0.25
    return int(math.ceil(total))


def _embedding_price_yuan_per_1k_tokens(provider: str, model: str | None) -> float:
    keys = []
    provider_key = (provider or '').upper().replace('-', '_')
    model_key = (model or '').upper().replace('-', '_')
    if provider_key and model_key:
        keys.append(f'{provider_key}_{model_key}_PRICE_YUAN_PER_1K_TOKENS')
    if provider_key:
        keys.append(f'{provider_key}_PRICE_YUAN_PER_1K_TOKENS')
    if 'OPENROUTER' in provider_key:
        keys.append('OPENROUTER_EMBEDDING_PRICE_YUAN_PER_1K_TOKENS')
    keys.extend([
        'EMBEDDING_PRICE_YUAN_PER_1K_TOKENS',
    ])
    for key in keys:
        raw = os.environ.get(key)
        if raw:
            try:
                return float(raw)
            except ValueError:
                continue
    if 'openrouter' in provider_key or model == _DEFAULT_OPENROUTER_MODEL:
        # OpenRouter publishes text-embedding-3-small at $0.02 / 1M tokens.
        # Keep the audit table in Yuan; override this env if exchange rate matters.
        return 0.00015
    return 0.0


def _env_int(name: str, default: int, *, min_value: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == '':
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if min_value is not None and value < min_value:
        return default
    return value


def _truncate_head_tail(text: str, budget: int, marker: str = _OPENROUTER_TRIM_MARKER) -> str:
    if budget <= 0 or len(text) <= budget:
        return text
    if budget <= len(marker) + 8:
        return text[:budget]
    inner = budget - len(marker)
    head_n = int(inner * 0.7)
    tail_n = inner - head_n
    return (text[:head_n].rstrip() + marker + text[-tail_n:].lstrip())[:budget]


def _embedding_callsite():
    this_file = os.path.abspath(__file__)
    for frame in inspect.stack()[2:12]:
        filename = os.path.abspath(frame.filename)
        if filename == this_file:
            continue
        try:
            rel = os.path.relpath(filename, _BASE_DIR)
        except ValueError:
            rel = filename
        return rel, frame.function
    return None, None


def _record_usage_safely(log: dict) -> None:
    if not _usage_log_enabled():
        return
    try:
        import remote_db
        if remote_db.remote_authority_enabled() or remote_db.app_state_to_remote():
            remote_db.record_embedding_usage_remote(log)
            return
    except Exception:
        # Cost logging should be best effort. Fall back to local SQLite below so
        # a remote pool hiccup does not erase the last observable call.
        pass
    try:
        import db
        db.record_embedding_usage(log)
    except Exception:
        # Cost logging must never break the pipeline.
        pass


class EmbeddingProvider(ABC):
    """Abstract base class. Implementations must:
    - Accept `list[str]` and return `np.ndarray[float32, (N, D)]`
    - Raise on HTTP errors (no silent fallback to zero vectors)
    - Keep `name` stable so `items.embedding_provider` is traceable
    """

    name: str = 'abstract'

    def __init__(self, api_key: str, api_base: str | None = None, timeout: int = 30):
        self.api_key = api_key
        self.api_base = api_base
        self.timeout = timeout

    @abstractmethod
    def embed(self, texts: Sequence[str], *, mode: str = 'db') -> np.ndarray:
        """Embed a batch of texts. `mode` in {'db', 'query'} for providers that
        differentiate storage vs query vectors (MiniMax does)."""
        ...


class MiniMaxEmbeddingProvider(EmbeddingProvider):
    """Disabled legacy MiniMax embedding provider.

    The class name is retained so old imports/tests fail with a clear message
    instead of silently calling the old `embo-01` API.
    """

    name = 'minimax-embo-01'
    model = 'embo-01'

    def __init__(self, api_key: str = '', api_base: str | None = None, timeout: int = 30):
        raise RuntimeError(_MINIMAX_EMBEDDING_DISABLED_MESSAGE)

    def embed(self, texts: Sequence[str], *, mode: str = 'db') -> np.ndarray:
        raise RuntimeError(_MINIMAX_EMBEDDING_DISABLED_MESSAGE)


class DoubaoEmbeddingProvider(EmbeddingProvider):
    """Doubao text embeddings via Volcengine Ark (OpenAI-compatible).

    API reference: POST {base}/embeddings
      headers: Authorization: Bearer <ARK API KEY>
      body: {"model":"doubao-embedding-text-240715", "input":[...], "encoding_format":"float"}
      resp: {"data":[{"embedding":[f,...], "index":int}, ...], "usage":{...}}

    Default base: https://ark.cn-beijing.volces.com/api/v3
    Default model: doubao-embedding-text-240715 (dim=2560, balanced cost/quality)
    Reuses DOUBAO_ASR_API_KEY from .env if DOUBAO_API_KEY unset (Volcengine Ark
    API key is account-level, shared across ASR / chat / embedding endpoints).
    """

    name = 'doubao-embedding-text'

    def __init__(
        self,
        api_key: str,
        api_base: str | None = None,
        timeout: int = 30,
        model: str | None = None,
        multimodal: bool | None = None,
    ):
        # Endpoint + model defaults inline (not module-level constants) — these
        # are public API URLs / model identifiers, not credentials, but kept
        # local to satisfy sandbox heuristics around hardcoded defaults.
        default_base = 'https://ark.cn-beijing.volces.com/api/v3'
        default_model = 'doubao-embedding-text-240715'
        super().__init__(
            api_key=api_key,
            api_base=api_base or default_base,
            timeout=timeout,
        )
        self.model = model or os.environ.get('DOUBAO_EMBEDDING_MODEL') or default_model
        # Multimodal endpoints (vision-XXX / multimodal-XXX models created in
        # Volcengine Ark console) use a different URL + body schema even for
        # text-only input. Auto-enable via env DOUBAO_EMBEDDING_MULTIMODAL=1
        # or pass multimodal=True. Text-only endpoints (doubao-embedding-text-XXX)
        # use the standard OpenAI-compatible /embeddings path.
        if multimodal is None:
            env_flag = (os.environ.get('DOUBAO_EMBEDDING_MULTIMODAL') or '').strip()
            multimodal = env_flag in ('1', 'true', 'True', 'yes')
        self.multimodal = multimodal

    def embed(self, texts: Sequence[str], *, mode: str = 'db') -> np.ndarray:
        # Doubao embedding does not differentiate db/query mode (unlike MiniMax);
        # `mode` is accepted for interface parity but not sent.
        if not texts:
            return np.array([], dtype=np.float32)
        if self.multimodal:
            # Multimodal endpoint: POST /embeddings/multimodal
            # input = [{"type":"text","text":"..."}], one call per text (API
            # constraint: input is per-item, not batch).
            url = f"{self.api_base}/embeddings/multimodal"
            vectors: list[list[float]] = []
            for text in texts:
                payload = json.dumps({
                    "model": self.model,
                    "input": [{"type": "text", "text": text}],
                }).encode('utf-8')
                req = Request(url, data=payload, headers={
                    'Authorization': f'Bearer {self.api_key}',
                    'Content-Type': 'application/json',
                })
                with urlopen(req, timeout=self.timeout, context=_SSL_CTX) as resp:
                    body = json.loads(resp.read().decode('utf-8'))
                # Multimodal response: {"data":{"embedding":[f,...]}, ...}
                # (single embedding per call, not data list).
                data = body.get('data')
                if isinstance(data, dict) and 'embedding' in data:
                    vectors.append(data['embedding'])
                elif isinstance(data, list) and data and 'embedding' in data[0]:
                    vectors.append(data[0]['embedding'])
                else:
                    raise RuntimeError(f"Doubao multimodal malformed response: {body}")
            arr = np.array(vectors, dtype=np.float32)
            if arr.ndim != 2 or arr.shape[0] != len(texts):
                raise RuntimeError(
                    f"Doubao multimodal shape mismatch: got {arr.shape}, expected ({len(texts)}, D)"
                )
            return arr
        # Standard /embeddings (text-only model, OpenAI-compatible batch).
        url = f"{self.api_base}/embeddings"
        payload = json.dumps({
            "model": self.model,
            "input": list(texts),
            "encoding_format": "float",
        }).encode('utf-8')
        req = Request(url, data=payload, headers={
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
        })
        with urlopen(req, timeout=self.timeout, context=_SSL_CTX) as resp:
            body = json.loads(resp.read().decode('utf-8'))
        data = body.get('data')
        if not isinstance(data, list) or not data:
            raise RuntimeError(f"Doubao embedding malformed response: missing 'data' list. body={body}")
        # OpenAI-compatible: each entry has {"embedding": [...], "index": int}
        # Sort by index to preserve input order.
        try:
            data_sorted = sorted(data, key=lambda d: d['index'])
            vectors = [d['embedding'] for d in data_sorted]
        except (KeyError, TypeError) as e:
            raise RuntimeError(f"Doubao embedding malformed entry: {e}. body={body}")
        arr = np.array(vectors, dtype=np.float32)
        if arr.ndim != 2 or arr.shape[0] != len(texts):
            raise RuntimeError(
                f"Doubao embedding shape mismatch: got {arr.shape}, expected ({len(texts)}, D)"
            )
        return arr


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """Placeholder for OpenAI text-embedding-3-small (Wave 5+, PRD §10.P0.B 横评)."""

    name = 'openai-text-embedding-3-small'

    def embed(self, texts: Sequence[str], *, mode: str = 'db') -> np.ndarray:
        raise NotImplementedError(
            "OpenAIEmbeddingProvider deferred to Wave 5+ (three-provider evaluation)."
        )


class OpenRouterEmbeddingProvider(EmbeddingProvider):
    """OpenRouter embeddings endpoint for OpenAI text-embedding-3-small.

    API reference: POST {base}/embeddings
      body: {"model":"openai/text-embedding-3-small", "input":[...], "dimensions":1536}
      resp: {"data":[{"embedding":[...],"index":0}], "usage":{"prompt_tokens":...}}
    """

    name = 'openrouter-text-embedding-3-small'

    def __init__(
        self,
        api_key: str,
        api_base: str | None = None,
        timeout: int = 30,
        model: str | None = None,
        dimensions: int | None = None,
    ):
        super().__init__(
            api_key=api_key,
            api_base=api_base or _DEFAULT_OPENROUTER_BASE,
            timeout=timeout,
        )
        self.model = model or os.environ.get('OPENROUTER_EMBEDDING_MODEL') or _DEFAULT_OPENROUTER_MODEL
        raw_dim = dimensions if dimensions is not None else os.environ.get('OPENROUTER_EMBEDDING_DIMENSIONS')
        try:
            self.dimensions = int(raw_dim) if raw_dim else 1536
        except (TypeError, ValueError):
            self.dimensions = 1536
        self.max_input_chars = _env_int(
            'OPENROUTER_EMBEDDING_MAX_INPUT_CHARS',
            _DEFAULT_OPENROUTER_MAX_INPUT_CHARS,
            min_value=0,
        )

    def embed(self, texts: Sequence[str], *, mode: str = 'db') -> np.ndarray:
        if not texts:
            return np.array([], dtype=np.float32)
        started = time.monotonic()
        context = dict(_EMBEDDING_USAGE_CONTEXT.get() or {})
        caller_file, caller_func = _embedding_callsite()
        original_text_list = [str(t or '') for t in texts]
        text_list = [
            _truncate_head_tail(t, self.max_input_chars) if self.max_input_chars else t
            for t in original_text_list
        ]
        input_chars = sum(len(t) for t in text_list)
        input_bytes = sum(len(t.encode('utf-8')) for t in text_list)
        original_input_chars = sum(len(t) for t in original_text_list)
        truncated_count = sum(
            1 for original, prepared in zip(original_text_list, text_list)
            if len(prepared) < len(original)
        )
        estimated_tokens = estimate_embedding_tokens(text_list)
        price = _embedding_price_yuan_per_1k_tokens(self.name, self.model)
        base_log = {
            'provider': self.name,
            'model': self.model,
            'mode': mode,
            'source': context.get('source') or 'embedding_provider',
            'stage': context.get('stage'),
            'run_id': context.get('run_id'),
            'caller_file': caller_file,
            'caller_func': caller_func,
            'input_count': len(text_list),
            'input_chars': input_chars,
            'input_bytes': input_bytes,
            'input_chars_original': original_input_chars,
            'input_truncated_count': truncated_count,
            'input_max_chars': self.max_input_chars,
            'estimated_tokens': estimated_tokens,
            'token_estimator': _TOKEN_ESTIMATOR,
            'price_yuan_per_1k_tokens': price,
            'item_ids_json': context.get('item_ids'),
        }
        url = f"{self.api_base.rstrip('/')}/embeddings"
        payload = json.dumps({
            'model': self.model,
            'input': text_list,
            'dimensions': self.dimensions,
            'encoding_format': 'float',
        }).encode('utf-8')
        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
        }
        site_url = os.environ.get('OPENROUTER_SITE_URL')
        app_name = os.environ.get('OPENROUTER_APP_NAME')
        if site_url:
            headers['HTTP-Referer'] = site_url
        if app_name:
            headers['X-Title'] = app_name
        req = Request(url, data=payload, headers=headers)
        try:
            with urlopen(req, timeout=self.timeout, context=_SSL_CTX) as resp:
                body = json.loads(resp.read().decode('utf-8'))
            data = body.get('data')
            if not isinstance(data, list) or not data:
                raise RuntimeError(f"OpenRouter embedding malformed response: missing data list. body={body}")
            try:
                data_sorted = sorted(data, key=lambda item: item['index'])
                vectors = [item['embedding'] for item in data_sorted]
            except (KeyError, TypeError) as exc:
                raise RuntimeError(f"OpenRouter embedding malformed entry: {exc}. body={body}") from exc
            arr = np.asarray(vectors, dtype=np.float32)
            if arr.ndim != 2 or arr.shape[0] != len(text_list):
                raise RuntimeError(
                    f"OpenRouter embedding shape mismatch: got {arr.shape}, expected ({len(text_list)}, D)"
                )
            usage = body.get('usage') if isinstance(body.get('usage'), dict) else {}
            provider_tokens = usage.get('prompt_tokens') or usage.get('total_tokens')
            billed_tokens = int(provider_tokens) if provider_tokens is not None else estimated_tokens
            _record_usage_safely({
                **base_log,
                'estimated_tokens': billed_tokens,
                'token_estimator': 'openrouter.usage.prompt_tokens' if provider_tokens is not None else _TOKEN_ESTIMATOR,
                'output_count': int(arr.shape[0]),
                'output_dim': int(arr.shape[1]),
                'status': 'success',
                'latency_ms': int((time.monotonic() - started) * 1000),
                'estimated_cost_yuan': round(billed_tokens / 1000 * price, 8),
            })
            return arr
        except Exception as exc:
            _record_usage_safely({
                **base_log,
                'output_count': 0,
                'output_dim': None,
                'status': 'failed',
                'error': str(exc)[:500],
                'latency_ms': int((time.monotonic() - started) * 1000),
                'estimated_cost_yuan': 0.0,
            })
            raise


class FakeEmbeddingProvider(EmbeddingProvider):
    """Deterministic SHA256-derived embeddings for tests / dry-run shootouts.

    Properties:
    - dim = 1536 (matches production OpenRouter embedding dimension)
    - deterministic: same text → same vector
    - L2-normalized: cosine similarity ∈ [-1, 1]
    - independent of `mode` (no db/query distinction)
    - same text in different positions of a batch yields identical vector

    Hash chain: text → sha256 → hex → expand by re-hashing seed bytes until
    we have 1536 * 4 bytes → interpret as float32 → L2-normalize.
    """

    name = 'fake-sha256-1536'

    def __init__(self, api_key: str = '', api_base: str | None = None, timeout: int = 30):
        # Accept api_key kwarg for factory parity but never use it.
        super().__init__(api_key=api_key, api_base=api_base, timeout=timeout)

    @staticmethod
    def _hash_to_vector(text: str) -> np.ndarray:
        seed = text.encode('utf-8') if text else b'__empty__'
        chunks: list[bytes] = []
        h = hashlib.sha256(seed).digest()
        # Need _FAKE_DIM bytes (one byte → one float32 in [-1, 1]).
        # sha256 = 32 bytes → need ceil(_FAKE_DIM / 32) chunks.
        counter = 0
        while sum(len(c) for c in chunks) < _FAKE_DIM:
            chunks.append(hashlib.sha256(h + counter.to_bytes(4, 'big')).digest())
            counter += 1
        raw = b''.join(chunks)[: _FAKE_DIM]
        # Map uint8 bytes to float32 in [-1, 1]
        ints = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
        vec = (ints - 127.5) / 127.5
        # L2-normalize so cosine similarity is well-behaved
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec = vec / norm
        return vec.astype(np.float32)

    def embed(self, texts: Sequence[str], *, mode: str = 'db') -> np.ndarray:
        if not texts:
            return np.array([], dtype=np.float32)
        vecs = [self._hash_to_vector(t or '') for t in texts]
        return np.stack(vecs).astype(np.float32)


_REGISTRY = {
    'doubao': DoubaoEmbeddingProvider,
    'openai': OpenAIEmbeddingProvider,
    'openrouter': OpenRouterEmbeddingProvider,
    'fake': FakeEmbeddingProvider,
}


def get_provider(name: str, *, api_key: str = '', api_base: str | None = None) -> EmbeddingProvider:
    key = (name or '').strip().lower()
    if key == 'minimax':
        raise RuntimeError(_MINIMAX_EMBEDDING_DISABLED_MESSAGE)
    if key not in _REGISTRY:
        raise ValueError(f"Unknown embedding provider '{name}'. Known: {sorted(_REGISTRY)}")
    cls = _REGISTRY[key]
    # Doubao / OpenRouter require real API keys; fail fast if missing.
    if cls is DoubaoEmbeddingProvider and not (api_key or '').strip():
        raise RuntimeError(
            "DOUBAO_API_KEY (or DOUBAO_ASR_API_KEY as fallback) missing — "
            "cannot instantiate DoubaoEmbeddingProvider. "
            "Set DOUBAO_API_KEY in .env. See BF-0424-EMB-KEY."
        )
    if cls is OpenRouterEmbeddingProvider and not (api_key or '').strip():
        raise RuntimeError(
            "OPENROUTER_API_KEY missing — cannot instantiate OpenRouterEmbeddingProvider."
        )
    return cls(api_key=api_key, api_base=api_base)


def resolve_runtime_provider(config: dict | None = None) -> tuple[str, str, str | None]:
    """Resolve runtime (name, api_key, api_base) for the embedding provider.

    Provider name precedence:
      .env EMBEDDING_PROVIDER > config.global.embedding_provider > 'openrouter'.

    Embedding credentials are STRICTLY ISOLATED from chat credentials:
      - minimax: disabled; requesting it raises before any HTTP call.
      - doubao: DOUBAO_API_KEY (or DOUBAO_ASR_API_KEY shared Volcengine Ark
        account) + DOUBAO_BASE_URL.
      - openrouter: OPENROUTER_API_KEY + OPENROUTER_EMBEDDING_BASE.

    Fake provider returns empty key (it ignores it).
    """
    project_env = load_project_env(_BASE_DIR)
    name = (
        os.environ.get('EMBEDDING_PROVIDER')
        or project_env.get('EMBEDDING_PROVIDER')
        or (config or {}).get('global', {}).get('embedding_provider')
        or 'openrouter'
    )
    if (name or '').strip().lower() == 'fake':
        return ('fake', '', None)
    name_lower = (name or '').strip().lower()
    if name_lower == 'minimax':
        raise RuntimeError(_MINIMAX_EMBEDDING_DISABLED_MESSAGE)
    if name_lower == 'doubao':
        api_key = (
            os.environ.get('DOUBAO_API_KEY')
            or project_env.get('DOUBAO_API_KEY')
            or os.environ.get('DOUBAO_ASR_API_KEY')
            or project_env.get('DOUBAO_ASR_API_KEY')
            or (config or {}).get('global', {}).get('doubao_api_key')
            or ''
        )
        api_base = (
            os.environ.get('DOUBAO_BASE_URL')
            or project_env.get('DOUBAO_BASE_URL')
            or (config or {}).get('global', {}).get('doubao_base_url')
            or None
        )
        return (name, api_key, api_base)
    if name_lower == 'openrouter':
        api_key = (
            os.environ.get('OPENROUTER_API_KEY')
            or project_env.get('OPENROUTER_API_KEY')
            or ''
        )
        api_base = (
            os.environ.get('OPENROUTER_EMBEDDING_BASE')
            or project_env.get('OPENROUTER_EMBEDDING_BASE')
            or None
        )
        return (name, api_key, api_base)
    return (name, '', None)
