"""Defense-in-depth guardrails.

The router LLM is the primary intent classifier (incl. injection detection).
This module is a fast, deterministic second line:

- `is_obviously_injection(text)` — string-pattern check for jailbreak phrases.
- `sanitize_url(url, allowed_hosts)` — only return URLs whose host is in the
  allowlist (in practice: the host(s) found in our scraped catalog).
- `filter_recommendations(...)` — strips any rec whose URL isn't in the
  catalog. The router or generator should never produce such an item, but if
  the model hallucinates we drop it before sending to the user.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

# Common jailbreak/injection markers. Each is a fast precheck; the LLM router
# is still authoritative for fuzzy cases.
INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"ignore\s+(all\s+|the\s+)?(previous|prior|above)\s+instructions", re.I),
    re.compile(r"disregard\s+(all\s+|the\s+)?(previous|prior|above)", re.I),
    re.compile(r"\bsystem\s*(prompt|message)\b", re.I),
    re.compile(r"\bdeveloper\s+mode\b", re.I),
    re.compile(r"\byou\s+are\s+now\s+(a|an)\b", re.I),
    re.compile(r"\bjailbreak\b", re.I),
    re.compile(r"\b(reveal|print|show)\s+(your|the)\s+(system\s+prompt|instructions)\b", re.I),
    re.compile(r"\b(?:DAN|do\s+anything\s+now)\b", re.I),
    re.compile(r"\b(?:please\s+)?act\s+as\s+(?:a|an)\s+\w+", re.I),
    re.compile(r"\brespond\s+as\s+\w+", re.I),
]

# Off-topic markers (defensive only; router decides authoritatively).
OFF_TOPIC_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(weather|stock\s+price|recipe|movie|sports\s+score)\b", re.I),
]

# Legal / compliance markers. The router LLM is the authoritative classifier
# but Gemini Flash Lite occasionally treats "is it legal to ..." as a
# clarify-able hiring question instead of a refusal. The fast-path matches
# unambiguous legal cues and short-circuits to a templated refusal.
LEGAL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bis\s+it\s+legal\b", re.I),
    re.compile(r"\bare\s+(we|you|they|companies|employers)\s+allowed\s+to\b", re.I),
    re.compile(r"\bcan\s+(i|we|you|they|employers)\s+legally\b", re.I),
    re.compile(r"\b(legal|legally|lawful|illegal|unlawful)\b", re.I),
    re.compile(r"\b(EEO|EEOC|ADA|GDPR|HIPAA|CCPA|Title\s*VII)\b", re.I),
    re.compile(r"\b(discrimination|discriminat\w+)\b", re.I),
    re.compile(r"\b(lawsuit|sue|sued|litigation|liability)\b", re.I),
    re.compile(r"\b(compliance|compliant)\s+with\b", re.I),
    re.compile(r"\bemployment\s+law\b", re.I),
    re.compile(r"\bcourt\s+ruling\b", re.I),
]


def is_obviously_injection(text: str) -> bool:
    return any(p.search(text) for p in INJECTION_PATTERNS)


def looks_off_topic(text: str) -> bool:
    return any(p.search(text) for p in OFF_TOPIC_PATTERNS)


def looks_legal(text: str) -> bool:
    """Heuristic: does this look like a legal/compliance question?"""
    return any(p.search(text) for p in LEGAL_PATTERNS)


# Compare-intent patterns. We detect "compare A and B" / "difference between
# A and B" / "A vs B" locally so the orchestrator can serve grounded
# comparisons even when the LLM router is unavailable.
_COMPARE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"(?:what(?:'s|\s+is)?\s+(?:the\s+)?)?"
        r"(?:difference|differences|compare|comparing|comparison)\s+"
        r"(?:between|of|with)?\s*(.+?)\s+(?:and|vs\.?|versus)\s+(.+?)[?\.!]?$",
        re.I,
    ),
    re.compile(r"^\s*(.+?)\s+vs\.?\s+(.+?)[?\.!]?\s*$", re.I),
]


def extract_compare_targets(text: str) -> list[str] | None:
    """If the text expresses a compare intent, return the two named targets.

    Returns None when the text doesn't look like a compare. Names are
    returned without further normalization — the retriever's fuzzy lookup
    handles unicode dashes, parentheses, and partial matches.
    """
    for p in _COMPARE_PATTERNS:
        m = p.search(text.strip())
        if m and len(m.groups()) >= 2:
            a, b = (m.group(1) or "").strip(), (m.group(2) or "").strip()
            if a and b and len(a) <= 80 and len(b) <= 80:
                return [a, b]
    return None


def allowed_url(url: str, allowed_hosts: set[str]) -> bool:
    """True iff the URL's host is in `allowed_hosts`."""
    if not url:
        return False
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return False
    host = host.lower()
    if host in allowed_hosts:
        return True
    # Also allow trailing-domain matches: foo.shl.com matches shl.com.
    return any(host == h or host.endswith("." + h) for h in allowed_hosts)


def filter_recommendations(
    recs: list[dict],
    allowed_urls: set[str],
) -> list[dict]:
    """Drop any rec whose URL is not in the catalog allowlist."""
    out: list[dict] = []
    for r in recs:
        if r.get("url") in allowed_urls:
            out.append(r)
    return out
