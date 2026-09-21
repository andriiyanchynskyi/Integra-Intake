"""Versioned skill contracts kept separate from tools and policy."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from app.agent import AgentMessage, MessageRole


_SKILL_NAME = re.compile(r"^[a-z][a-z0-9_]*\.v[1-9][0-9]*$")
ModelT = TypeVar("ModelT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class SkillDefinition:
    """Static skill metadata and schemas; it performs no model invocation."""

    name: str
    input_model: type[ModelT]
    output_model: type[OutputT]
    system_prompt: str
    eval_fixture: Path

    def __post_init__(self) -> None:
        if _SKILL_NAME.fullmatch(self.name) is None:
            raise ValueError("skill name must use <name>.v<positive integer>")
        if not self.system_prompt.strip():
            raise ValueError("skill system_prompt must be non-empty")

    def build_system_message(self) -> AgentMessage:
        return AgentMessage(
            role=MessageRole.SYSTEM,
            content=self.system_prompt,
        )
