"""
gh0st SQLite State Ledger & SQLAlchemy Database Schema (Milestone 1)
Repository: https://github.com/Judge-M/gh0st
Persistence Layer: SQLite / SQLAlchemy ORM
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pathlib import Path

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker, Session

from .models import (
    ClaimValidity,
    ChildTicketProposal,
    MemoryClaim,
    QuotaRecord,
    Ticket,
    TicketStatus,
    ToolSchema,
    WorkerReport,
)


class Base(DeclarativeBase):
    pass


class TaskRow(Base):
    __tablename__ = "tasks"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class TicketRow(Base):
    __tablename__ = "tickets"

    ticket_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), ForeignKey("tasks.task_id"), index=True)
    parent_ticket_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    depth: Mapped[int] = mapped_column(Integer, default=0)
    capability: Mapped[str] = mapped_column(String(64), index=True)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    constraints_json: Mapped[str] = mapped_column(Text, default="[]")
    context_slice_json: Mapped[str] = mapped_column(Text, default="{}")
    attached_tool_schema_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    max_tokens_allocated: Mapped[int] = mapped_column(Integer, default=4096)
    max_cost_usd: Mapped[float] = mapped_column(Float, default=0.50)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class WorkerReportRow(Base):
    __tablename__ = "worker_reports"

    report_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ticket_id: Mapped[str] = mapped_column(String(64), ForeignKey("tickets.ticket_id"), unique=True, index=True)
    task_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    output_json: Mapped[str] = mapped_column(Text, default="{}")
    stdout: Mapped[str] = mapped_column(Text, default="")
    stderr: Mapped[str] = mapped_column(Text, default="")
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    child_proposals_json: Mapped[str] = mapped_column(Text, default="[]")
    completed_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class QuotaRecordRow(Base):
    __tablename__ = "quota_records"

    quota_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), ForeignKey("tasks.task_id"), unique=True, index=True)
    max_tokens_budget: Mapped[int] = mapped_column(Integer, nullable=False)
    tokens_consumed: Mapped[int] = mapped_column(Integer, default=0)
    max_cost_usd_budget: Mapped[float] = mapped_column(Float, nullable=False)
    cost_usd_consumed: Mapped[float] = mapped_column(Float, default=0.0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class MemoryClaimRow(Base):
    __tablename__ = "memory_claims"

    claim_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope: Mapped[str] = mapped_column(String(64), index=True)
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    is_trusted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="promoted", index=True)
    validity: Mapped[str] = mapped_column(String(32), default="valid", index=True)
    provenance_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class StateLedger:
    """State Ledger Manager for gh0st, providing SQLite persistence for tickets, reports, quotas, and claims."""

    def __init__(self, db_url: Optional[str] = None):
        if db_url is None:
            db_path = Path.home() / ".gh0st" / "state.db"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            db_url = f"sqlite:///{db_path.as_posix()}"
        self.db_url = db_url
        self.engine = create_engine(db_url, echo=False)
        self.SessionLocal = sessionmaker(bind=self.engine)

    def init_db(self):
        """Create database tables if they do not exist."""
        Base.metadata.create_all(bind=self.engine)

    def get_session(self) -> Session:
        return self.SessionLocal()

    # --- Task Operations ---
    def create_task(self, task_id: str, description: str) -> TaskRow:
        with self.get_session() as session:
            task = TaskRow(task_id=task_id, description=description)
            session.add(task)
            session.commit()
            session.refresh(task)
            return task

    def get_task(self, task_id: str) -> Optional[TaskRow]:
        with self.get_session() as session:
            return session.get(TaskRow, task_id)

    def set_task_status(self, task_id: str, status: str) -> None:
        with self.get_session() as session:
            row = session.get(TaskRow, task_id)
            if row is None:
                raise ValueError(f"Unknown task: {task_id}")
            row.status = status
            session.commit()

    # --- Quota Operations ---
    def create_quota_record(self, quota: QuotaRecord) -> QuotaRecord:
        with self.get_session() as session:
            row = QuotaRecordRow(
                quota_id=quota.quota_id,
                task_id=quota.task_id,
                max_tokens_budget=quota.max_tokens_budget,
                tokens_consumed=quota.tokens_consumed,
                max_cost_usd_budget=quota.max_cost_usd_budget,
                cost_usd_consumed=quota.cost_usd_consumed,
                is_active=quota.is_active,
            )
            session.add(row)
            session.commit()
            return quota

    def get_quota_record(self, task_id: str) -> Optional[QuotaRecord]:
        with self.get_session() as session:
            row = session.query(QuotaRecordRow).filter(QuotaRecordRow.task_id == task_id).first()
            if not row:
                return None
            return QuotaRecord(
                quota_id=row.quota_id,
                task_id=row.task_id,
                max_tokens_budget=row.max_tokens_budget,
                tokens_consumed=row.tokens_consumed,
                max_cost_usd_budget=row.max_cost_usd_budget,
                cost_usd_consumed=row.cost_usd_consumed,
                is_active=row.is_active,
            )

    def update_quota_consumption(self, task_id: str, tokens_used: int, cost_usd: float) -> Optional[QuotaRecord]:
        with self.get_session() as session:
            row = session.query(QuotaRecordRow).filter(QuotaRecordRow.task_id == task_id).first()
            if not row:
                return None
            row.tokens_consumed += tokens_used
            row.cost_usd_consumed += cost_usd
            session.commit()
            session.refresh(row)
            return QuotaRecord(
                quota_id=row.quota_id,
                task_id=row.task_id,
                max_tokens_budget=row.max_tokens_budget,
                tokens_consumed=row.tokens_consumed,
                max_cost_usd_budget=row.max_cost_usd_budget,
                cost_usd_consumed=row.cost_usd_consumed,
                is_active=row.is_active,
            )

    # --- Ticket Operations ---
    def create_ticket(self, ticket: Ticket) -> Ticket:
        with self.get_session() as session:
            attached_tool_json = (
                ticket.attached_tool_schema.model_dump_json()
                if ticket.attached_tool_schema
                else None
            )
            row = TicketRow(
                ticket_id=ticket.ticket_id,
                task_id=ticket.task_id,
                parent_ticket_id=ticket.parent_ticket_id,
                depth=ticket.depth,
                capability=ticket.capability,
                objective=ticket.objective,
                constraints_json=json.dumps(ticket.constraints),
                context_slice_json=json.dumps(ticket.context_slice),
                attached_tool_schema_json=attached_tool_json,
                max_tokens_allocated=ticket.max_tokens_allocated,
                max_cost_usd=ticket.max_cost_usd,
                status=ticket.status.value,
            )
            session.add(row)
            session.commit()
            return ticket

    def get_ticket(self, ticket_id: str) -> Optional[Ticket]:
        with self.get_session() as session:
            row = session.query(TicketRow).filter(TicketRow.ticket_id == ticket_id).first()
            if not row:
                return None

            tool_schema = (
                ToolSchema(**json.loads(row.attached_tool_schema_json))
                if row.attached_tool_schema_json
                else None
            )
            return Ticket(
                ticket_id=row.ticket_id,
                task_id=row.task_id,
                parent_ticket_id=row.parent_ticket_id,
                depth=row.depth,
                capability=row.capability,
                objective=row.objective,
                constraints=json.loads(row.constraints_json),
                context_slice=json.loads(row.context_slice_json),
                attached_tool_schema=tool_schema,
                max_tokens_allocated=row.max_tokens_allocated,
                max_cost_usd=row.max_cost_usd,
                status=TicketStatus(row.status),
                created_at=row.created_at,
            )

    def list_pending_tickets(self, limit: int = 10) -> List[Ticket]:
        with self.get_session() as session:
            rows = (
                session.query(TicketRow)
                .filter(TicketRow.status == TicketStatus.PENDING.value)
                .order_by(TicketRow.created_at.asc())
                .limit(limit)
                .all()
            )
            tickets = []
            for row in rows:
                tool_schema = (
                    ToolSchema(**json.loads(row.attached_tool_schema_json))
                    if row.attached_tool_schema_json
                    else None
                )
                tickets.append(
                    Ticket(
                        ticket_id=row.ticket_id,
                        task_id=row.task_id,
                        parent_ticket_id=row.parent_ticket_id,
                        depth=row.depth,
                        capability=row.capability,
                        objective=row.objective,
                        constraints=json.loads(row.constraints_json),
                        context_slice=json.loads(row.context_slice_json),
                        attached_tool_schema=tool_schema,
                        max_tokens_allocated=row.max_tokens_allocated,
                        max_cost_usd=row.max_cost_usd,
                        status=TicketStatus(row.status),
                        created_at=row.created_at,
                    )
                )
            return tickets

    def list_task_tickets(self, task_id: str) -> List[Ticket]:
        with self.get_session() as session:
            ids = session.query(TicketRow.ticket_id).filter(TicketRow.task_id == task_id).order_by(TicketRow.created_at).all()
        return [self.get_ticket(ticket_id) for (ticket_id,) in ids]

    def update_ticket_status(self, ticket_id: str, status: TicketStatus) -> bool:
        with self.get_session() as session:
            row = session.query(TicketRow).filter(TicketRow.ticket_id == ticket_id).first()
            if not row:
                return False
            row.status = status.value
            session.commit()
            return True

    # --- Worker Report Operations ---
    def save_worker_report(self, report: WorkerReport) -> WorkerReport:
        with self.get_session() as session:
            if session.query(WorkerReportRow).filter(WorkerReportRow.ticket_id == report.ticket_id).first():
                raise ValueError(f"Report already exists for {report.ticket_id}")
            proposals_json = json.dumps(
                [p.model_dump() for p in report.child_ticket_proposals]
            )
            row = WorkerReportRow(
                report_id=report.report_id,
                ticket_id=report.ticket_id,
                task_id=report.task_id,
                status=report.status,
                output_json=json.dumps(report.output),
                stdout=report.stdout,
                stderr=report.stderr,
                tokens_used=report.tokens_used,
                cost_usd=report.cost_usd,
                child_proposals_json=proposals_json,
            )
            session.add(row)
            session.commit()

            # Automatically update ticket status and quota consumption
            ticket_status = (
                TicketStatus.COMPLETED if report.status == "success" else TicketStatus.FAILED
            )
            row_ticket = session.query(TicketRow).filter(TicketRow.ticket_id == report.ticket_id).first()
            if row_ticket:
                row_ticket.status = ticket_status.value

            row_quota = session.query(QuotaRecordRow).filter(QuotaRecordRow.task_id == report.task_id).first()
            if row_quota:
                row_quota.tokens_consumed += max(0, report.tokens_used)
                row_quota.cost_usd_consumed += max(0.0, report.cost_usd)

            session.commit()
            return report

    def get_worker_report(self, ticket_id: str) -> Optional[WorkerReport]:
        with self.get_session() as session:
            row = session.query(WorkerReportRow).filter(WorkerReportRow.ticket_id == ticket_id).first()
            if not row:
                return None

            proposals_raw = json.loads(row.child_proposals_json)
            proposals = [ChildTicketProposal(**p) for p in proposals_raw]

            return WorkerReport(
                report_id=row.report_id,
                ticket_id=row.ticket_id,
                task_id=row.task_id,
                status=row.status,
                output=json.loads(row.output_json),
                stdout=row.stdout,
                stderr=row.stderr,
                tokens_used=row.tokens_used,
                cost_usd=row.cost_usd,
                child_ticket_proposals=proposals,
                completed_at=row.completed_at,
            )

    # --- Memory Claim Operations ---
    def save_memory_claim(self, claim: MemoryClaim) -> MemoryClaim:
        with self.get_session() as session:
            row = MemoryClaimRow(
                claim_id=claim.claim_id,
                scope=claim.scope,
                claim_text=claim.claim_text,
                is_trusted=claim.is_trusted,
                status=claim.status,
                validity=claim.validity.value,
                provenance_json=json.dumps(claim.provenance),
            )
            session.add(row)
            session.commit()
            return claim

    def get_trusted_valid_claims(self, scope: str) -> List[MemoryClaim]:
        """Fetch human-gated (is_trusted=True) and valid (validity='valid') claims for context packing."""
        with self.get_session() as session:
            rows = (
                session.query(MemoryClaimRow)
                .filter(
                    MemoryClaimRow.scope == scope,
                    MemoryClaimRow.is_trusted.is_(True),  # Human-gated authority
                    MemoryClaimRow.status == "promoted",
                    MemoryClaimRow.validity == ClaimValidity.VALID.value,  # Exclude stale/expired
                )
                .all()
            )
            claims = []
            for r in rows:
                claims.append(
                    MemoryClaim(
                        claim_id=r.claim_id,
                        scope=r.scope,
                        claim_text=r.claim_text,
                        is_trusted=r.is_trusted,
                        status=r.status,
                        validity=ClaimValidity(r.validity),
                        provenance=json.loads(r.provenance_json),
                    )
                )
            return claims
