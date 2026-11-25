from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.manager import AuditLogEntryResponse

DemoScenarioName = Literal[
    "normal_pickup",
    "qr_replay",
    "pre_prep_cancel",
    "serial_abuse",
]


class SimulateScenarioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    surge_override: bool | None = Field(
        default=None,
        description="Optional override to force surge or non-surge evaluation time",
        examples=[True],
    )


class DemoStepResult(BaseModel):
    step_name: str = Field(
        description="Name of the executed pipeline step",
        examples=["verify_qr_first_scan"],
    )
    status_code: int = Field(
        description="HTTP-equivalent status code of the step",
        examples=[200],
    )
    latency_ms: float = Field(
        description="Step execution latency in milliseconds",
        examples=[12.4],
    )
    outcome: dict[str, Any] = Field(
        description="Step outcome payload",
        examples=[{"flash": "GREEN", "reason_code": "PICKUP_VERIFIED"}],
    )


class DemoSimulateResponse(BaseModel):
    scenario: DemoScenarioName = Field(
        description="Executed simulation scenario identifier",
        examples=["serial_abuse"],
    )
    total_latency_ms: float = Field(
        description="Total end-to-end simulation latency in milliseconds",
        examples=[84.2],
    )
    steps: list[DemoStepResult] = Field(
        description="Ordered timeline of executed steps with per-step latencies"
    )
    final_result: dict[str, Any] = Field(
        description="Final outcome summary of the scenario",
        examples=[{"decision": "BLOCK", "risk_score": 0.75}],
    )
    audit_entries: list[AuditLogEntryResponse] = Field(
        description="Audit log entries created during this simulation"
    )
