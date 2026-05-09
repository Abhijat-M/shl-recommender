"""Conversational agent for SHL assessment recommendation.

Stateless: each `run` takes the full conversation history and returns the
agent's next reply + structured recommendations.

Pipeline per turn:
  1. Refuse-fast: deterministic prompt-injection precheck on the latest user
     turn. If it matches, refuse before calling the LLM.
  2. Router LLM (JSON): classifies intent, extracts slots, generates a search
     query.
  3. Branch by intent:
       - clarify  -> emit clarifying question, no recs
       - refuse   -> deterministic refusal text, no recs
       - compare  -> retrieve named assessments, generate factual prose
       - search   -> hybrid retrieve, ask generator LLM to pick a 1..10 subset
       - refine   -> same as search; the search query inherits prior slots so
                     "actually, add personality tests" lands on the right query
  4. Hard guardrails: scope-check each rec against the URL allowlist, cap at
     10, validate against the API schema (caller does this final step).

All boundaries are explicit: the orchestrator returns a typed `AgentTurnResult`
that the API layer maps to the response schema.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from src.agent.guardrails import (
    allowed_url,
    extract_compare_targets,
    is_obviously_injection,
    looks_legal,
)
from src.agent.prompts import (
    COMPARE_SYSTEM,
    RECOMMEND_SYSTEM,
    REFUSAL_REPLIES,
    ROUTER_SYSTEM,
    initial_clarifying_question,
)
from src.config import get_settings
from src.llm import LLMClient, LLMError, LLMMessage, make_llm
from src.retrieval.catalog import Assessment
from src.retrieval.retriever import HybridRetriever, RetrievalFilters

LOG = logging.getLogger(__name__)

INTENTS = {"clarify", "search", "refine", "compare", "refuse"}


@dataclass(slots=True)
class Recommendation:
    name: str
    url: str
    test_type: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "url": self.url, "test_type": self.test_type}


@dataclass(slots=True)
class AgentTurnResult:
    reply: str
    recommendations: list[Recommendation] = field(default_factory=list)
    end_of_conversation: bool = False
    # Diagnostics (not returned to user but useful for tests/eval)
    intent: str = ""
    rationale: str = ""


@dataclass(slots=True)
class _RouterDecision:
    intent: str
    slots: dict[str, Any]
    search_query: str
    compare_targets: list[str]
    refuse_reason: str | None
    clarifying_question: str
    rationale: str


class Orchestrator:
    """The agent. Holds a retriever + LLM client, runs one turn at a time."""

    def __init__(
        self,
        retriever: HybridRetriever | None = None,
        llm: LLMClient | None = None,
    ) -> None:
        self._settings = get_settings()
        self._retriever = retriever or HybridRetriever()
        self._llm = llm or make_llm()
        self._allowed_urls: set[str] | None = None
        self._allowed_hosts: set[str] | None = None

    def warm(self) -> Orchestrator:
        """Load the index. Safe to call multiple times."""
        self._retriever.load()
        if self._allowed_urls is None:
            self._allowed_urls = {a.url for a in self._retriever.catalog}
            self._allowed_hosts = set()
            for u in self._allowed_urls:
                from urllib.parse import urlparse

                host = (urlparse(u).hostname or "").lower()
                if host:
                    self._allowed_hosts.add(host)
        return self

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    async def run(self, messages: list[dict[str, str]]) -> AgentTurnResult:
        """Process one turn of conversation."""
        self.warm()

        if not messages:
            return AgentTurnResult(
                reply=initial_clarifying_question(), intent="clarify"
            )

        # Find latest user message.
        latest_user = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"),
            "",
        )

        # 1) Fast refusal (cheaper than an LLM call when the cue is obvious).
        if is_obviously_injection(latest_user):
            return AgentTurnResult(
                reply=REFUSAL_REPLIES["injection"],
                intent="refuse",
                rationale="local injection regex match",
            )
        if looks_legal(latest_user):
            return AgentTurnResult(
                reply=REFUSAL_REPLIES["legal"],
                intent="refuse",
                rationale="local legal regex match",
            )

        # 2) Route via LLM.
        try:
            decision = await self._route(messages)
        except LLMError as e:
            LOG.warning("router LLM failed: %s. Attempting retrieval-only fallback.", e)
            return self._retrieval_only_fallback(messages, reason=str(e))

        # 3) Branch by intent.
        if decision.intent == "refuse":
            reason = (decision.refuse_reason or "off_topic").lower()
            if reason not in REFUSAL_REPLIES:
                reason = "off_topic"
            return AgentTurnResult(
                reply=REFUSAL_REPLIES[reason],
                intent="refuse",
                rationale=decision.rationale,
            )

        if decision.intent == "clarify":
            # Safety net: don't loop on clarify forever. If we've already asked
            # 2+ clarifying questions and the user has answered each time,
            # force a search even if the router says "clarify". The 8-turn
            # cap is real and the SHL evaluator stops on a shortlist.
            if _prior_clarify_count(messages) >= 2 and _user_turns(messages) >= 2:
                LOG.info("clarify-loop guard: forcing search after %d clarifies",
                         _prior_clarify_count(messages))
                forced = _RouterDecision(
                    intent="search",
                    slots=decision.slots,
                    search_query=decision.search_query
                    or _last_user_message(messages),
                    compare_targets=[],
                    refuse_reason=None,
                    clarifying_question="",
                    rationale="clarify_loop_guard",
                )
                return await self._handle_search(messages, forced)
            q = (decision.clarifying_question or "").strip()
            if not q:
                q = initial_clarifying_question()
            return AgentTurnResult(reply=q, intent="clarify", rationale=decision.rationale)

        if decision.intent == "compare":
            return await self._handle_compare(messages, decision)

        # search / refine
        return await self._handle_search(messages, decision)

    # ------------------------------------------------------------------
    # Stage 1: routing + slot extraction
    # ------------------------------------------------------------------
    async def _route(self, messages: list[dict[str, str]]) -> _RouterDecision:
        history = _serialize_history(messages)
        msgs = [
            LLMMessage(role="system", content=ROUTER_SYSTEM),
            LLMMessage(
                role="user",
                content=(
                    "Conversation so far:\n" + history + "\n\nProduce the JSON now."
                ),
            ),
        ]
        raw = await self._llm.complete_json(msgs, temperature=0.0, max_tokens=1500)
        intent = (raw.get("intent") or "").strip().lower()
        if intent not in INTENTS:
            LOG.warning("router returned unknown intent %r; defaulting to clarify", intent)
            intent = "clarify"
        slots = raw.get("slots") or {}
        if not isinstance(slots, dict):
            slots = {}
        compare_targets = raw.get("compare_targets") or []
        if not isinstance(compare_targets, list):
            compare_targets = []
        return _RouterDecision(
            intent=intent,
            slots=slots,
            search_query=str(raw.get("search_query") or "").strip(),
            compare_targets=[str(x) for x in compare_targets if str(x).strip()],
            refuse_reason=(str(raw["refuse_reason"]).lower() if raw.get("refuse_reason") else None),
            clarifying_question=str(raw.get("clarifying_question") or "").strip(),
            rationale=str(raw.get("rationale") or "").strip(),
        )

    # ------------------------------------------------------------------
    # Branch: search / refine
    # ------------------------------------------------------------------
    async def _handle_search(
        self, messages: list[dict[str, str]], decision: _RouterDecision
    ) -> AgentTurnResult:
        # Build retrieval query: search_query + slot-derived hints.
        query = decision.search_query
        if not query:
            # Fallback: stitch latest user message into a query.
            query = next(
                (m["content"] for m in reversed(messages) if m.get("role") == "user"),
                "",
            )

        slots = decision.slots
        # Append slot hints to broaden the query (BM25 benefits from explicit terms).
        hint_parts: list[str] = []
        if slots.get("role"):
            hint_parts.append(f"Role: {slots['role']}")
        if slots.get("seniority"):
            hint_parts.append(f"Seniority: {slots['seniority']}")
        if slots.get("skills"):
            hint_parts.append("Skills: " + ", ".join(slots["skills"]))
        if hint_parts:
            query = query + "\n" + "\n".join(hint_parts)

        # Filters (only HARD requirements should be applied; soft prefs go to the LLM).
        filters = RetrievalFilters(
            test_types=set(slots.get("test_types") or []),
            require_remote_testing=slots.get("remote_testing"),
        )
        # Soften: if filters yield <3, retry without test_type filter to let LLM pick.
        hits = self._retriever.search(query, filters=filters, top_k=self._settings.top_k_final)
        if len(hits) < 3 and filters.test_types:
            LOG.info("Filter yielded %d hits; retrying without test_type filter", len(hits))
            hits = self._retriever.search(
                query,
                filters=RetrievalFilters(require_remote_testing=filters.require_remote_testing),
                top_k=self._settings.top_k_final,
            )
        if not hits:
            # Last resort: drop all filters.
            hits = self._retriever.search(query, top_k=self._settings.top_k_final)
        if not hits:
            return AgentTurnResult(
                reply=(
                    "I couldn't find a clean match in the SHL catalog for that. "
                    "Could you describe the role and key skills?"
                ),
                intent="clarify",
                rationale="no_hits",
            )

        # Stage 2: ask the LLM to pick a 1..10 subset by index.
        try:
            reply, picked_indices, end_conv = await self._select(
                messages, decision, hits
            )
        except LLMError as e:
            LOG.warning("generator LLM failed: %s. Falling back to top-K.", e)
            fallback_indices = list(range(min(5, len(hits))))
            recs = _hits_to_recs(
                [hits[i] for i in fallback_indices if 0 <= i < len(hits)]
            )
            recs = self._enforce_url_allowlist(recs)
            return AgentTurnResult(
                reply=self._fallback_reply(decision),
                recommendations=recs,
                end_of_conversation=False,
                intent=decision.intent or "search",
                rationale="generator_error_fallback",
            )

        chosen: list[Assessment] = []
        for i in picked_indices:
            if 0 <= i < len(hits):
                chosen.append(hits[i].assessment)
        if not chosen:
            # Generator returned nothing useful: take the top 5.
            chosen = [h.assessment for h in hits[:5]]
            if not reply:
                reply = self._fallback_reply(decision)

        # Cap at 10 (spec) and clamp to 1.
        if len(chosen) > 10:
            chosen = chosen[:10]
        recs = _assessments_to_recs(chosen)
        recs = self._enforce_url_allowlist(recs)
        if not recs:
            return AgentTurnResult(
                reply=(
                    "I couldn't find a clean match in the SHL catalog for that. "
                    "Could you describe the role and key skills?"
                ),
                intent="clarify",
                rationale="all_recs_filtered_out",
            )

        return AgentTurnResult(
            reply=reply or self._fallback_reply(decision),
            recommendations=recs,
            end_of_conversation=end_conv,
            intent=decision.intent or "search",
            rationale=decision.rationale,
        )

    async def _select(
        self,
        messages: list[dict[str, str]],
        decision: _RouterDecision,
        hits: list,  # list[Hit]
    ) -> tuple[str, list[int], bool]:
        history = _serialize_history(messages)
        candidates = _serialize_candidates(hits)
        slot_str = _format_slots(decision.slots)
        msgs = [
            LLMMessage(role="system", content=RECOMMEND_SYSTEM),
            LLMMessage(
                role="user",
                content=(
                    f"Conversation:\n{history}\n\n"
                    f"Slots:\n{slot_str}\n\n"
                    f"Candidates (index, name, test_types):\n{candidates}\n\n"
                    "Output JSON now."
                ),
            ),
        ]
        raw = await self._llm.complete_json(msgs, temperature=0.0, max_tokens=800)
        reply = str(raw.get("reply") or "").strip()
        idx_raw = raw.get("selected_indices") or []
        if not isinstance(idx_raw, list):
            idx_raw = []
        indices: list[int] = []
        for i in idx_raw:
            try:
                indices.append(int(i))
            except (TypeError, ValueError):
                continue
        end_conv = bool(raw.get("end_of_conversation"))
        return reply, indices, end_conv

    def _fallback_reply(self, decision: _RouterDecision) -> str:
        role = (decision.slots.get("role") or "the role").strip()
        return f"Here is a shortlist of SHL assessments that fit {role}."

    # ------------------------------------------------------------------
    # Branch: compare
    # ------------------------------------------------------------------
    async def _handle_compare(
        self, messages: list[dict[str, str]], decision: _RouterDecision
    ) -> AgentTurnResult:
        targets = decision.compare_targets[:3]  # cap blast radius
        if not targets:
            return AgentTurnResult(
                reply="Which two assessments would you like me to compare?",
                intent="clarify",
            )
        records = self._retriever.lookup_by_names(targets, fuzzy=True)
        if not records:
            return AgentTurnResult(
                reply=(
                    f"I couldn't find {' or '.join(targets)} in the SHL catalog. "
                    "Could you give the full names?"
                ),
                intent="clarify",
            )

        # Build context block of ONLY the catalog data.
        context_lines: list[str] = []
        for r in records:
            context_lines.append(f"- Name: {r.name}")
            context_lines.append(f"  URL: {r.url}")
            if r.test_types:
                context_lines.append(
                    f"  Test types: {', '.join(r.test_type_full_names)}"
                )
            if r.assessment_length:
                context_lines.append(f"  Length: {r.assessment_length}")
            if r.job_levels:
                context_lines.append(f"  Job levels: {', '.join(r.job_levels)}")
            if r.languages:
                context_lines.append(f"  Languages: {', '.join(r.languages)}")
            if r.description:
                context_lines.append(f"  Description: {r.description[:600]}")
            context_lines.append("")
        context = "\n".join(context_lines)

        history = _serialize_history(messages)
        msgs = [
            LLMMessage(role="system", content=COMPARE_SYSTEM),
            LLMMessage(
                role="user",
                content=(
                    f"Conversation:\n{history}\n\n"
                    f"Catalog records to compare:\n{context}\n\n"
                    "Output JSON now."
                ),
            ),
        ]
        try:
            raw = await self._llm.complete_json(msgs, temperature=0.0, max_tokens=800)
            reply = str(raw.get("reply") or "").strip()
        except LLMError as e:
            LOG.warning("compare LLM failed: %s; emitting deterministic summary", e)
            reply = self._deterministic_compare_summary(records)
        if not reply:
            reply = self._deterministic_compare_summary(records)
        return AgentTurnResult(
            reply=reply,
            intent="compare",
            rationale=decision.rationale,
        )

    # ------------------------------------------------------------------
    # Graceful degradation when the LLM is unavailable
    # ------------------------------------------------------------------
    def _retrieval_only_fallback(
        self,
        messages: list[dict[str, str]],
        reason: str,
    ) -> AgentTurnResult:
        """Best-effort response when the router LLM has failed.

        Order of attempts:
        1. Compare-intent (regex on latest user turn) -> grounded summary.
        2. Hybrid retrieve on the concatenated user history.
        3. Clarify (last resort).

        The retriever is local (FAISS + BM25) and needs no LLM. We use the
        concatenated user turns as the query so context-rich conversations
        still get relevant recs.
        """
        latest = _last_user_message(messages)
        # 1) Compare-intent fast path.
        targets = extract_compare_targets(latest)
        if targets:
            records = self._retriever.lookup_by_names(targets, fuzzy=True)
            if records:
                return AgentTurnResult(
                    reply=self._deterministic_compare_summary(records),
                    intent="compare",
                    rationale=f"local_compare_fallback:{reason[:60]}",
                )

        full_query = _all_user_text(messages).strip()
        if len(full_query.split()) < 2:
            return AgentTurnResult(
                reply=initial_clarifying_question(),
                intent="clarify",
                rationale=f"router_error_no_query:{reason[:60]}",
            )
        try:
            hits = self._retriever.search(
                full_query, top_k=self._settings.top_k_final
            )
        except Exception:
            LOG.exception("retrieval-only fallback failed")
            hits = []
        if not hits:
            return AgentTurnResult(
                reply=initial_clarifying_question(),
                intent="clarify",
                rationale=f"router_error_no_hits:{reason[:60]}",
            )
        recs = _hits_to_recs(hits[: max(3, min(5, len(hits)))])
        recs = self._enforce_url_allowlist(recs)
        if not recs:
            return AgentTurnResult(
                reply=initial_clarifying_question(),
                intent="clarify",
                rationale="router_error_no_allowed_recs",
            )
        return AgentTurnResult(
            reply=(
                "Here are SHL assessments that look most relevant to your query."
            ),
            recommendations=recs,
            intent="search",
            rationale=f"retrieval_only_fallback:{reason[:60]}",
        )

    @staticmethod
    def _deterministic_compare_summary(records: list[Assessment]) -> str:
        parts: list[str] = []
        for r in records:
            tt = ", ".join(r.test_type_full_names) or "no listed type"
            length = f", {r.assessment_length}" if r.assessment_length else ""
            parts.append(f"{r.name} covers {tt}{length}.")
        return " ".join(parts)

    # ------------------------------------------------------------------
    # URL allowlist
    # ------------------------------------------------------------------
    def _enforce_url_allowlist(self, recs: list[Recommendation]) -> list[Recommendation]:
        if self._allowed_urls is None:
            return recs
        out: list[Recommendation] = []
        for r in recs:
            if r.url in self._allowed_urls and (
                self._allowed_hosts is None
                or allowed_url(r.url, self._allowed_hosts)
            ):
                out.append(r)
        return out


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def _user_turns(messages: list[dict[str, str]]) -> int:
    return sum(1 for m in messages if m.get("role") == "user")


def _all_user_text(messages: list[dict[str, str]]) -> str:
    """Concatenate every user message so cumulative context is queryable
    even when the LLM router is unavailable."""
    parts = [
        (m.get("content") or "").strip()
        for m in messages
        if m.get("role") == "user"
    ]
    return " ".join(p for p in parts if p)


def _last_user_message(messages: list[dict[str, str]]) -> str:
    return next(
        (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
        "",
    )


def _prior_clarify_count(messages: list[dict[str, str]]) -> int:
    """Heuristic: count assistant messages that look like clarifying questions.

    A clarifying question ends with a "?" and is short (< 200 chars). This
    matches what the agent itself emits in clarify mode.
    """
    n = 0
    for m in messages:
        if m.get("role") != "assistant":
            continue
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if content.endswith("?") and len(content) < 200:
            n += 1
    return n


def _serialize_history(messages: list[dict[str, str]]) -> str:
    out: list[str] = []
    for m in messages:
        role = (m.get("role") or "user").strip()
        content = (m.get("content") or "").strip()
        if not content:
            continue
        out.append(f"{role.upper()}: {content}")
    return "\n".join(out)


def _serialize_candidates(hits: list) -> str:
    """Format candidates for the generator. Keeps it short to save tokens."""
    lines: list[str] = []
    for i, h in enumerate(hits):
        a: Assessment = h.assessment
        types = "".join(a.test_types) or "-"
        # 200-char description preview keeps the prompt small.
        desc = (a.description or "").strip().replace("\n", " ")
        if len(desc) > 200:
            desc = desc[:197] + "..."
        meta_bits: list[str] = []
        if a.assessment_length:
            meta_bits.append(a.assessment_length)
        if a.job_levels:
            meta_bits.append("levels: " + "/".join(a.job_levels[:3]))
        meta = "; ".join(meta_bits)
        lines.append(f"[{i}] {a.name} (types={types}; {meta}) :: {desc}")
    return "\n".join(lines)


def _format_slots(slots: dict[str, Any]) -> str:
    if not slots:
        return "(none)"
    lines: list[str] = []
    for k, v in slots.items():
        if v in (None, "", [], {}):
            continue
        lines.append(f"- {k}: {v}")
    return "\n".join(lines) if lines else "(none)"


def _hits_to_recs(hits: list) -> list[Recommendation]:
    out: list[Recommendation] = []
    for h in hits:
        a: Assessment = h.assessment
        out.append(
            Recommendation(name=a.name, url=a.url, test_type=a.test_type_str or "")
        )
    return out


def _assessments_to_recs(assessments: list[Assessment]) -> list[Recommendation]:
    return [
        Recommendation(name=a.name, url=a.url, test_type=a.test_type_str or "")
        for a in assessments
    ]
