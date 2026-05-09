"""Pydantic schemas for the public API.

These mirror the SHL spec exactly. The schema is non-negotiable (deviating
breaks the automated evaluator), so we keep it tight and explicit here.
Every field has a description + example for OpenAPI consumers.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Role = Literal["user", "assistant", "system"]


class ChatMessage(BaseModel):
    """One turn of conversation history."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"role": "user", "content": "Hiring a Java developer who works with stakeholders"},
                {"role": "assistant", "content": "Sure. What is seniority level?"},
            ]
        }
    )

    role: Role = Field(description="The author of the message.")
    content: str = Field(description="The message body. Max 8000 chars (truncated).")

    @field_validator("content")
    @classmethod
    def _trim(cls, v: str) -> str:
        if len(v) > 8000:
            return v[:8000]
        return v


class ChatRequest(BaseModel):
    """Stateless chat request: full history, every turn."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "messages": [
                        {"role": "user", "content": "Hiring a Java developer who works with stakeholders"},
                        {"role": "assistant", "content": "Sure. What is seniority level?"},
                        {"role": "user", "content": "Mid-level, around 4 years"},
                    ]
                }
            ]
        }
    )

    messages: list[ChatMessage] = Field(
        default_factory=list,
        description="Full conversation history. Service holds no per-conversation state.",
    )


class Recommendation(BaseModel):
    """A single SHL Individual Test Solution returned to the caller."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "name": "Java 8 (New)",
                    "url": "https://www.shl.com/products/product-catalog/view/java-8-new/",
                    "test_type": "K",
                }
            ]
        }
    )

    name: str = Field(description="Assessment name as it appears in the SHL catalog.")
    url: str = Field(description="Full catalog detail URL on shl.com.")
    test_type: str = Field(
        description=(
            "SHL test-type letter codes (joined when multiple). "
            "A=Ability/Aptitude, B=Biodata/SJT, C=Competencies, D=Development/360, "
            "E=Exercises, K=Knowledge/Skills, P=Personality/Behavior, S=Simulations."
        ),
    )


class ChatResponse(BaseModel):
    """Agent reply + (optional) committed shortlist + end-of-conversation flag.

    `recommendations` is empty when the agent is clarifying or refusing.
    It is 1-10 items when the agent has committed to a shortlist.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "reply": "Got it. Here are 5 assessments that fit a mid-level Java dev with stakeholder needs.",
                    "recommendations": [
                        {
                            "name": "Java 8 (New)",
                            "url": "https://www.shl.com/products/product-catalog/view/java-8-new/",
                            "test_type": "K",
                        },
                        {
                            "name": "Occupational Personality Questionnaire OPQ32r",
                            "url": "https://www.shl.com/products/product-catalog/view/occupational-personality-questionnaire-opq32r/",
                            "test_type": "P",
                        },
                    ],
                    "end_of_conversation": False,
                },
                {
                    "reply": "What role are you hiring for, and at what seniority level?",
                    "recommendations": [],
                    "end_of_conversation": False,
                },
            ]
        }
    )

    reply: str = Field(description="The agent's natural-language reply for this turn.")
    recommendations: list[Recommendation] = Field(
        default_factory=list,
        description=(
            "Empty when clarifying or refusing. 1-10 items when committed."
        ),
    )
    end_of_conversation: bool = Field(
        default=False,
        description="True only when the agent considers the task complete.",
    )

    @field_validator("recommendations")
    @classmethod
    def _cap(cls, v: list[Recommendation]) -> list[Recommendation]:
        if len(v) > 10:
            return v[:10]
        return v


class HealthResponse(BaseModel):
    """Liveness probe response."""

    status: Literal["ok"] = "ok"


class ReadyResponse(BaseModel):
    """Readiness probe response."""

    status: Literal["ready", "not_ready"] = Field(
        description="`ready` only after the index is loaded and the LLM client is configured."
    )
    catalog_size: int = Field(description="Number of indexed assessments.")
    llm_configured: bool = Field(description="True if a Groq API key is present.")


class VersionResponse(BaseModel):
    """Build/version metadata for diagnostics."""

    name: str
    version: str
    model: str = Field(description="LLM model identifier in use.")
    embedding_model: str
