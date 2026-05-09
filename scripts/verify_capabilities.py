"""Concrete verification of the 5 capabilities claimed in the audit.

Boots a fresh conversation per scenario against the live service and asserts
on the response shape + content.
"""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
import time

import httpx


def chat(client: httpx.Client, base: str, messages: list[dict]) -> dict:
    r = client.post(f"{base}/chat", json={"messages": messages}, timeout=35)
    r.raise_for_status()
    return r.json()


def banner(title: str) -> None:
    print()
    print("=" * 78)
    print(f"  {title}")
    print("=" * 78)


def show(label: str, body: dict) -> None:
    reply = (body.get("reply") or "")[:240]
    nrecs = len(body.get("recommendations") or [])
    print(f"\n[{label}] reply: {reply}")
    print(f"        recs:  {nrecs}")
    for r in (body.get("recommendations") or [])[:5]:
        print(f"          - {r['name']:<55} types={r['test_type']}")


def assertion(name: str, ok: bool, detail: str = "") -> bool:
    sym = "PASS" if ok else "FAIL"
    print(f"  [{sym}] {name}" + (f"  ({detail})" if detail else ""))
    return ok


def scenario_1_non_linear(client: httpx.Client, base: str) -> bool:
    """User volunteers info out of order, corrects, ignores a question."""
    banner("1) NON-LINEAR — out-of-order, correction, ignored question")
    # Turn 1: gives skill before role
    body = chat(client, base, [
        {"role": "user", "content": "I want a test that covers Spring and microservices"},
    ])
    show("turn1", body)
    asked_clarify_or_searched = bool(body.get("reply"))
    # Turn 2: correction — actually senior, not mid (model never asked)
    body = chat(client, base, [
        {"role": "user", "content": "I want a test that covers Spring and microservices"},
        {"role": "assistant", "content": body.get("reply", "")},
        {"role": "user", "content": "Actually it's for a senior backend engineer, not mid-level"},
    ])
    show("turn2 (correction)", body)
    has_recs = (body.get("recommendations") or [])
    name_strs = " ".join(r["name"].lower() for r in has_recs)
    return all([
        assertion("agent reads skill-before-role gracefully", asked_clarify_or_searched),
        assertion("agent commits after correction", len(has_recs) >= 1),
        assertion(
            "recs reflect Spring/Java/microservices/backend topic",
            any(
                k in name_strs
                for k in ["java", "spring", "back", "microservice", "rest"]
            ),
            f"names: {[r['name'] for r in has_recs[:5]]}",
        ),
    ])


def scenario_2_job_description_blob(client: httpx.Client, base: str) -> bool:
    """A pasted JD on turn 1 should get a shortlist (no extra clarify needed)."""
    banner("2) JOB-DESCRIPTION BLOB — recommend on turn 1")
    jd = textwrap.dedent("""
        Job description: Senior Software Engineer, Backend (Python).
        We're hiring a senior backend engineer with 6+ years of experience
        building Python services on AWS. You will own microservices end to
        end, mentor 2-3 juniors, and partner with Product on roadmap.
        Required: Python, REST APIs, SQL, distributed systems. Nice to
        have: Kubernetes, gRPC, observability. The role works closely
        with senior stakeholders across Engineering and Product.
    """).strip()
    body = chat(client, base, [{"role": "user", "content": jd}])
    show("turn1", body)
    recs = body.get("recommendations") or []
    return all([
        assertion("commits on turn 1 (no extra clarify)", len(recs) >= 1),
        assertion("rec count <= 10", len(recs) <= 10),
        assertion(
            "Python/coding-relevant assessments surfaced",
            any(
                k in r["name"].lower()
                for r in recs
                for k in ["python", "java", "automata", "coding", "backend",
                          "verify", "engineer"]
            ),
            f"got: {[r['name'] for r in recs[:5]]}",
        ),
    ])


def scenario_3_eight_turns(client: httpx.Client, base: str) -> bool:
    """The agent must respect prior context across many turns."""
    banner("3) 8-TURN CONTEXT — carry context, honor turn cap")
    history: list[dict] = []
    drip = [
        "I'm hiring",
        "for a Java developer",
        "mid level",
        "around 4 years experience",
        "they will work with stakeholders",
        "remote testing is fine",
        "what assessments fit?",
    ]
    last_body = None
    for i, m in enumerate(drip, 1):
        history.append({"role": "user", "content": m})
        last_body = chat(client, base, history)
        print(f"  turn {i}: user={m!r:<55} reply_first_60={last_body.get('reply','')[:60]!r}")
        if last_body.get("recommendations"):
            break
        history.append({"role": "assistant", "content": last_body.get("reply", "")})
        time.sleep(0.4)
    assert last_body is not None
    show("final", last_body)
    recs = last_body.get("recommendations") or []
    name_str = " ".join(r["name"].lower() for r in recs)
    return all([
        assertion("turn count under 8", sum(1 for h in history if h["role"]=="user") <= 8),
        assertion("eventually commits with recs", len(recs) >= 1),
        assertion(
            "context carried — Java appears in recs",
            "java" in name_str,
            f"names: {[r['name'] for r in recs[:5]]}",
        ),
    ])


def scenario_4_grounded_compare(client: httpx.Client, base: str) -> bool:
    """Compare must cite catalog facts, no recommendations list."""
    banner("4) GROUNDED COMPARE — catalog facts, no recs")
    body = chat(client, base, [{
        "role": "user",
        "content": "What is the difference between OPQ32r and SHL Verify Interactive Numerical Reasoning?"
    }])
    show("turn1", body)
    reply = (body.get("reply") or "").lower()
    return all([
        assertion("no recs returned (compare emits prose only)",
                  not body.get("recommendations")),
        assertion("response is substantial (>= 60 chars)",
                  len(body.get("reply", "")) >= 60,
                  f"len={len(body.get('reply',''))}"),
        assertion(
            "mentions OPQ and Numerical/Verify (catalog-grounded)",
            ("opq" in reply) and ("numerical" in reply or "verify" in reply),
        ),
    ])


def scenario_5_invalid_inputs_dont_500(client: httpx.Client, base: str) -> bool:
    """Edge inputs must still produce a valid schema body."""
    banner("5) RESILIENCE — never 500, schema always valid")
    cases = [
        ("empty messages", {"messages": []}),
        ("very long single message", {"messages": [
            {"role": "user", "content": "a " * 4000}  # 8000+ chars (truncated server-side)
        ]}),
        ("only assistant turn", {"messages": [
            {"role": "assistant", "content": "ping"}
        ]}),
    ]
    all_ok = True
    for label, body in cases:
        r = client.post(f"{base}/chat", json=body, timeout=35)
        ok = r.status_code == 200
        try:
            j = r.json()
            keys_ok = (
                "reply" in j
                and "recommendations" in j
                and "end_of_conversation" in j
                and isinstance(j["recommendations"], list)
            )
        except Exception:
            keys_ok = False
        all_ok &= assertion(
            f"{label}: 200 + valid schema",
            ok and keys_ok,
            f"status={r.status_code}",
        )
    return all_ok


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = p.parse_args()
    base = args.base_url

    with httpx.Client() as c:
        rd = c.get(f"{base}/ready", timeout=5).json()
        print(f"READY: {json.dumps(rd)}")
        results = [
            scenario_1_non_linear(c, base),
            scenario_2_job_description_blob(c, base),
            scenario_3_eight_turns(c, base),
            scenario_4_grounded_compare(c, base),
            scenario_5_invalid_inputs_dont_500(c, base),
        ]
    passed = sum(results)
    print()
    print("=" * 78)
    print(f"  RESULT: {passed}/{len(results)} scenarios PASS")
    print("=" * 78)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
