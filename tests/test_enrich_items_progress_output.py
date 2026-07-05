import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import enrich_items


def test_enrich_progress_display_platform_maps_known_sources():
    assert enrich_items._display_platform('twitter') == 'X'
    assert enrich_items._display_platform('waytoagi') == 'waytoagi'
    assert enrich_items._display_platform('lingowhale') == '公众号'


def test_enrich_progress_chunk_platform_handles_single_and_mixed_sources():
    assert enrich_items._chunk_platform([{'platform': 'twitter'}]) == 'X'
    assert enrich_items._chunk_platform([
        {'platform': 'twitter'},
        {'platform': 'waytoagi'},
    ]) == '混合平台'
    assert enrich_items._chunk_platform([]) == '全部平台'
