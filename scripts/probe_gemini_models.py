"""List models the key can access; test the most promising free ones."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from src.config import get_settings  # noqa: E402
from src.llm import GeminiClient, LLMMessage  # noqa: E402


async def list_models(key: str) -> None:
    url = "https://generativelanguage.googleapis.com/v1beta/models"
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(url, params={"key": key})
    print(f"\n--- /models {r.status_code} ---")
    if r.status_code != 200:
        print(f"BODY: {r.text[:500]}")
        return
    data = r.json()
    models = data.get("models", [])
    print(f"got {len(models)} models")
    for m in models:
        name = m.get("name", "?")
        methods = m.get("supportedGenerationMethods", [])
        if "generateContent" in methods:
            print(f"  - {name}")


async def try_model(model: str) -> bool:
    print(f"\n--- testing {model} ---")
    c = GeminiClient(model=model)
    try:
        text = await c.complete_text(
            [LLMMessage(role="user", content="Say 'ok'.")],
            max_tokens=10,
        )
        print(f"  OK: {text!r}")
        return True
    except Exception as e:
        msg = str(e)[:200]
        print(f"  FAIL: {type(e).__name__}: {msg}")
        return False


async def main() -> int:
    settings = get_settings()
    key = settings.gemini_api_key
    if not key:
        print("GEMINI_API_KEY is empty.")
        return 1
    await list_models(key)
    candidates = [
        "gemini-2.0-flash",
        "gemini-2.0-flash-001",
        "gemini-1.5-flash",
        "gemini-1.5-flash-latest",
        "gemini-1.5-flash-8b",
        "gemini-2.5-flash",
        "gemini-flash-latest",
    ]
    for m in candidates:
        if await try_model(m):
            print(f"\n>>> WORKING MODEL: {m}")
            return 0
    print("\nNo model worked.")
    return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
