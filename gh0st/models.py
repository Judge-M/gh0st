"""
gh0st Core Protocol Models & Data Schemas (Milestone 1)
Repository: https://github.com/Judge-M/gh0st
Architectural Foundation: Pydantic v2
"""

from enum import Enum
from typing import Any, Dict, List, Optional, Literal
from datetime import datetime, timezone
import uuid
from pydantic import BaseModel, Field, ConfigDict


class TicketStatus(str, Enum):
    PENDING = "pending"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    HALTED = "halted"
    CANCELLED = "cancelled"


class ExecutionTier(str, Enum):
    LOW_COMPLEXITY = "low_complexity"    # Formats, syntax checks, simple lookups
    HIGH_COMPLEXITY = "high_complexity"  # Multi-file refactoring, deep reasoning


class ComputePlacement(str, Enum):
    LOCAL_GPU = "local_gpu"    # $0 spend, local hardware
    RENTED_GPU = "rented_gpu"  # Flat rate rented node (e.g. RunPod)
    CLOUD_API = "cloud_api"    # Commercial API endpoints (Claude / GPT-4o / OpenRouter)


class ClaimValidity(str, Enum):
    VALID = "valid"
    STALE = "stale"
    EXPIRED = "expired"
    INVALID = "invalid"


class ToolSchema(BaseModel):
    """Represents a single, pruned MCP / OpenAPI tool definition (1-Schema Rule)."""
    name: str = Field(description="Unique tool function name")
    description: str = Field(description="Concise functional overview")
    parameters: Dict[str, Any] = Field(default_factory=dict, description="JSON Schema for function arguments")


class MemoryRoute(BaseModel):
    """System-1 OAG memory decision carried with a ticket."""
    system_rules: Literal[True] = Field(default=True, description="Always load trusted, scope-matched SQLite governance rules")
    ontology_domains: List[str] = Field(default_factory=list, description="Matched OAG memory domains")
    reference_providers: List[str] = Field(default_factory=list, description="External untrusted retrievers selected for this turn")


class ChildTicketProposal(BaseModel):
    """Proposal emitted by an ephemeral worker for downstream sub-tasks."""
    capability: str = Field(description="Capability cluster identifier")
    objective: str = Field(description="Specific sub-task goal")
    context_slice: Dict[str, Any] = Field(default_factory=dict, description="Lean context payload slice")
    estimated_tier: ExecutionTier = Field(default=ExecutionTier.LOW_COMPLEXITY)


class Ticket(BaseModel):
    """Primary unit of work in gh0st. Task-scoped, lean, and immutable during turn."""
    ticket_id: str = Field(default_factory=lambda: f"tkt_{uuid.uuid4().hex[:12]}")
    task_id: str = Field(description="Root task context identifier")
    parent_ticket_id: Optional[str] = Field(default=None, description="Parent ticket ID for child task lineage")
    depth: int = Field(default=0, ge=0, description="Recursion depth counter (0 = root ticket)")
    capability: str = Field(description="Target capability cluster")
    objective: str = Field(description="Specific bounded objective for this worker turn")
    constraints: List[str] = Field(default_factory=list, description="Execution boundaries or rules")
    context_slice: Dict[str, Any] = Field(default_factory=dict, description="Minimal context payload")
    workspace_path: str = Field(description="Absolute path to this task's isolated workspace")
    source_commit: Optional[str] = Field(default=None, description="Commit copied into the task workspace")
    permitted_local_capabilities: List[str] = Field(default_factory=lambda: ["local_shell"])
    gateway_allowance: Dict[str, Any] = Field(default_factory=dict)
    assigned_model: str = Field(default="")
    routing_strategy: str = Field(default="smart")

    # 1-Schema Rule
    attached_tool_schema: Optional[ToolSchema] = Field(default=None, description="Strictly 1 tool schema attached by System-1")
    memory_route: MemoryRoute = Field(default_factory=MemoryRoute, description="OAG-selected built-in and external memory paths")

    # Budget Caps
    max_tokens_allocated: int = Field(default=4096, ge=1, description="Max prompt + output tokens allowed")
    max_cost_usd: float = Field(default=0.50, ge=0, description="Max USD budget cap for this ticket")

    status: TicketStatus = Field(default=TicketStatus.PENDING)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class WorkerReport(BaseModel):
    """Terminal output emitted by an ephemeral worker before sys.exit(0)."""
    report_id: str = Field(default_factory=lambda: f"rpt_{uuid.uuid4().hex[:12]}")
    ticket_id: str = Field(description="Associated ticket identifier")
    task_id: str = Field(description="Root task identifier")
    status: str = Field(description="'success', 'failure', or 'halted'")
    output: Dict[str, Any] = Field(default_factory=dict, description="Structured execution result")
    stdout: str = Field(default="", description="Captured standard output")
    stderr: str = Field(default="", description="Captured error log")
    diff: str = Field(default="", description="Workspace diff against the source commit")
    commands_executed: List[Dict[str, Any]] = Field(default_factory=list)
    test_results: List[Dict[str, Any]] = Field(default_factory=list)
    unresolved_failures: List[str] = Field(default_factory=list)
    tokens_used: int = Field(default=0, ge=0, description="Actual tokens consumed")
    cost_usd: float = Field(default=0.0, ge=0, description="Actual cost incurred")

    # Fan-Out Emission
    child_ticket_proposals: List[ChildTicketProposal] = Field(
        default_factory=list,
        description="Child tickets proposed for queue re-entry"
    )
    completed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class QuotaRecord(BaseModel):
    """Financial and token budget tracking per root task context."""
    quota_id: str = Field(default_factory=lambda: f"qta_{uuid.uuid4().hex[:12]}")
    task_id: str = Field(description="Root task identifier")
    max_tokens_budget: int = Field(ge=1, description="Total token ceiling")
    tokens_consumed: int = Field(default=0, ge=0, description="Tokens used so far")
    tokens_reserved: int = Field(default=0, ge=0)
    tokens_uncertain: int = Field(default=0, ge=0)
    max_cost_usd_budget: float = Field(ge=0, description="Total cost cap in USD")
    cost_usd_consumed: float = Field(default=0.0, ge=0, description="USD spent so far")
    cost_usd_reserved: float = Field(default=0.0, ge=0)
    cost_usd_uncertain: float = Field(default=0.0, ge=0)
    is_active: bool = Field(default=True)

    @property
    def remaining_tokens(self) -> int:
        return max(0, self.max_tokens_budget - self.tokens_consumed - self.tokens_reserved - self.tokens_uncertain)

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.max_cost_usd_budget - self.cost_usd_consumed - self.cost_usd_reserved - self.cost_usd_uncertain)


class GrantRequest(BaseModel):
    grant_id: str = Field(default_factory=lambda: f"grt_{uuid.uuid4().hex[:12]}")
    task_id: str
    ticket_id: str
    action_type: str
    details: Dict[str, Any] = Field(default_factory=dict)
    status: str = Field(default="pending")
    decided_by: Optional[str] = None
    decided_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class MemoryClaim(BaseModel):
    """Human-gated trusted memory item (BrainConnect alignment)."""
    claim_id: str = Field(default_factory=lambda: f"clm_{uuid.uuid4().hex[:12]}")
    scope: str = Field(description="Project or task scope identifier")
    claim_text: str = Field(description="Fact assertion")
    is_trusted: bool = Field(default=False, description="Human authority approval flag")
    status: str = Field(default="promoted", description="'promoted', 'proposed', or 'rejected'")
    validity: ClaimValidity = Field(default=ClaimValidity.VALID, description="Strictly 'valid' for worker context")
    provenance: Dict[str, Any] = Field(default_factory=dict)


class System1Decision(BaseModel):
    """Sub-100ms routing decision emitted by Jev / Layla gateway router."""
    prompt: str
    confidence_score: float = Field(ge=0.0, le=1.0)
    selected_capability: str
    selected_model: Optional[str] = None
    selected_routing_strategy: str = "smart"
    execution_tier: ExecutionTier
    compute_placement: ComputePlacement
    attached_tool_schema: Optional[ToolSchema] = None
    memory_route: MemoryRoute = Field(default_factory=MemoryRoute)
    requires_planner: bool = Field(default=False, description="True if confidence < 0.90")
    reasoning_summary: str = Field(default="")
