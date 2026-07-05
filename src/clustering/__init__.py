"""Event aggregation clustering module (v15.0).

Subpackage layout:
- vector_utils: cosine / weighted mean / BLOB codec
- embedding_provider: ABC + OpenRouter/Doubao/OpenAI/Fake implementations
- pipeline: Stage 0-4 two-stage incremental clustering (Wave 3)
- summary_writer: draft -> live atomic swap (Wave 3)
- merge_detector: passive merge LLM judge (Wave 5+)
"""
