"""Pure parsing models for the historical CSI D/P2 research extractor.

The package deliberately has no dependency on the production application.  It
only turns cached Xueqiu payloads into normalised posts and observations; the
fetcher, validator, and report writer can build on these small primitives.
"""

from .models import (
    BasisConfidence,
    BasisEvidence,
    ParseConfidence,
    ParsedObservation,
    ParseFailure,
    PEBasis,
    RawPost,
    canonical_json,
    normalize_created_at,
    raw_post_from_payload,
    source_hash,
)
from .parser import (
    DEFAULT_BENCHMARK_CODE,
    DEFAULT_BENCHMARK_NAME,
    DEFAULT_SOURCE_PROVIDER,
    detail_payload_to_raw_post,
    extract_dividend_yield,
    extract_pe,
    extract_valuation_date,
    is_candidate_post,
    is_relevant_post,
    merge_post_detail,
    needs_detail_request,
    parse_post,
    strip_html,
    timeline_item_to_raw_post,
)

__all__ = [
    "DEFAULT_BENCHMARK_CODE",
    "DEFAULT_BENCHMARK_NAME",
    "DEFAULT_SOURCE_PROVIDER",
    "BasisConfidence",
    "BasisEvidence",
    "PEBasis",
    "ParseConfidence",
    "ParseFailure",
    "ParsedObservation",
    "RawPost",
    "canonical_json",
    "detail_payload_to_raw_post",
    "extract_dividend_yield",
    "extract_pe",
    "extract_valuation_date",
    "is_candidate_post",
    "is_relevant_post",
    "merge_post_detail",
    "needs_detail_request",
    "normalize_created_at",
    "parse_post",
    "raw_post_from_payload",
    "source_hash",
    "strip_html",
    "timeline_item_to_raw_post",
]
