"""Static regression checks for TLS verification in network clients."""
from pathlib import Path


NETWORK_CLIENT_FILES = [
    'src/generate_summaries.py',
    'src/enrich_items.py',
    'src/score_items.py',
    'src/generate_actions.py',
    'src/generate_briefing.py',
    'src/interest_engine.py',
    'src/asr_worker.py',
    'src/dedup_actions.py',
    'src/fetch_url.py',
    'src/fetch_lingowhale.py',
    'src/routes/actions.py',
    'src/routes/health.py',
    'src/ingest.py',
    'scripts/probe_ai_provider.py',
]

FORBIDDEN_TLS_SNIPPETS = (
    'CERT_NONE',
    'check_hostname = False',
    '_create_unverified_context',
)


def test_network_clients_do_not_disable_tls_verification():
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for rel in NETWORK_CLIENT_FILES:
        text = (root / rel).read_text(encoding='utf-8')
        for snippet in FORBIDDEN_TLS_SNIPPETS:
            if snippet in text:
                offenders.append(f'{rel}: {snippet}')

    assert offenders == []
