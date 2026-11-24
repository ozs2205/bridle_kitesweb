"""Pydantic request models (input validation)."""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from config import MAX_SOURCE_BYTES, VM_ALIASES


class LoadRequest(BaseModel):
    source: str = Field(..., description="RISC-V assembly source code")
    vm: str = Field(
        default="single-cycle",
        description="single-cycle | rvss | rv5-nh-nf | rv5-h-nf | rv5-nh-f | rv5-h-f",
    )

    @field_validator("source")
    @classmethod
    def _check_source(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source must not be empty")
        if "\x00" in value:
            raise ValueError("source must not contain NUL characters")
        if len(value.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise ValueError(f"source exceeds {MAX_SOURCE_BYTES} bytes")
        return value

    @field_validator("vm")
    @classmethod
    def _check_vm(cls, value: str) -> str:
        key = value.strip().lower()
        if key not in VM_ALIASES:
            raise ValueError(f"unknown vm '{value}'; valid: {', '.join(sorted(VM_ALIASES))}")
        return VM_ALIASES[key]


class ExecuteRequest(BaseModel):
    mode: Literal["run", "step", "run_debug"]
    count: int = Field(default=1, ge=1, le=100000, description="instructions to run in 'step' mode")
    breakpoints: Optional[List[int]] = Field(
        default=None,
        max_length=256,
        description="if given, the session's breakpoints (source line numbers) are set to exactly this list first",
    )

    @field_validator("breakpoints")
    @classmethod
    def _check_breakpoints(cls, value: Optional[List[int]]) -> Optional[List[int]]:
        if value is not None and any(line < 1 for line in value):
            raise ValueError("breakpoint line numbers must be >= 1")
        return value
