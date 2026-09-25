"""
gh0st SQLite State Ledger & SQLAlchemy Database Schema (Milestone 1)
Repository: https://github.com/Judge-M/gh0st
Persistence Layer: SQLite / SQLAlchemy ORM
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
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
    inspect,
    or_,
    update,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker, Session

from .models import (
    ClaimValidity,
    ChildTicketProposal,
    GrantRequest,
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
    workspace_path: Mapped[str] = mapped_column(Text, default="")
    source_commit: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    permitted_local_capabilities_json: Mapped[str] = mapped_column(Text, default='["local_shell"]')
    gateway_allowance_json: Mapped[str] = mapped_column(Text, default="{}")
    assigned_model: Mapped[str] = mapped_column(Text, default="")
    attached_tool_schema_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    max_tokens_allocated: Mapped[int] = mapped_column(Integer, default=4096)
    max_cost_usd: Mapped[float] = mapped_column(Float, default=0.50)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    claim_owner: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
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
    diff: Mapped[str] = mapped_column(Text, default="")
    commands_executed_json: Mapped[str] = mapped_column(Text, default="[]")
    test_results_json: Mapped[str] = mapped_column(Text, default="[]")
    unresolved_failures_json: Mapped[str] = mapped_column(Text, default="[]")
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
    tokens_reserved: Mapped[int] = mapped_column(Integer, default=0)
    tokens_uncertain: Mapped[int] = mapped_column(Integer, default=0)
    max_cost_usd_budget: Mapped[float] = mapped_column(Float, nullable=False)
    cost_usd_consumed: Mapped[float] = mapped_column(Float, default=0.0)
    cost_usd_reserved: Mapped[float] = mapped_column(Float, default=0.0)
    cost_usd_uncertain: Mapped[float] = mapped_column(Float, default=0.0)
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


class GrantRequestRow(Base):
    __tablename__ = "grant_requests"

    grant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), ForeignKey("tasks.task_id"), index=True)
    ticket_id: Mapped[str] = mapped_column(String(64), ForeignKey("tickets.ticket_id"), index=True)
    action_type: Mapped[str] = mapped_column(String(64))
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(32), default="pending")
    decided_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class RelayPairRow(Base):
    __tablename__ = "relay_pairs"

    pair_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    secret: Mapped[str] = mapped_column(Text, nullable=False)
    relay_url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class StateLedger:
    """State Ledger Manager for gh0st, providing SQLite persistence for tickets, reports, quotas, and claims."""

    CLAIM_LEASE_SECONDS = 300

    @staticmethod
    def _utc_now() -> datetime:
        # SQLite stores these timestamps without timezone metadata.
        return datetime.now(timezone.utc).replace(tzinfo=None)

    def __init__(self, db_url: Optional[str] = None):
        if db_url is None:
            db_path = Path.home() / ".gh0st" / "state.db"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            db_url = f"sqlite:///{db_path.as_posix()}"
        self.db_url = db_url
        self.engine = create_engine(db_url, echo=False)
        self.SessionLocal = sessionmaker(bind=self.engine)

    def init_db(self):
        """Create tables and add the Phase 1 columns to existing SQLite ledgers."""
        Base.metadata.create_all(bind=self.engine)
        if self.engine.dialect.name == "sqlite":
            additions = {
                "tickets": {
                    "workspace_path": "TEXT NOT NULL DEFAULT ''",
                    "source_commit": "VARCHAR(64)",
                    "permitted_local_capabilities_json": "TEXT NOT NULL DEFAULT '[\"local_shell\"]'",
                    "gateway_allowance_json": "TEXT NOT NULL DEFAULT '{}'",
                    "assigned_model": "TEXT NOT NULL DEFAULT ''",
                    "claimed_at": "DATETIME",
                    "claim_owner": "VARCHAR(64)",
                    "lease_expires_at": "DATETIME",
                },
                "worker_reports": {
                    "diff": "TEXT NOT NULL DEFAULT ''",
                    "commands_executed_json": "TEXT NOT NULL DEFAULT '[]'",
                    "test_results_json": "TEXT NOT NULL DEFAULT '[]'",
                    "unresolved_failures_json": "TEXT NOT NULL DEFAULT '[]'",
                },
                "quota_records": {
                    "tokens_reserved": "INTEGER NOT NULL DEFAULT 0",
                    "cost_usd_reserved": "FLOAT NOT NULL DEFAULT 0",
                    "tokens_uncertain": "INTEGER NOT NULL DEFAULT 0",
                    "cost_usd_uncertain": "FLOAT NOT NULL DEFAULT 0",
                },
                "grant_requests": {
                    "decided_by": "VARCHAR(128)",
                    "decided_at": "DATETIME",
                },
            }
            with self.engine.begin() as connection:
                for table, columns in additions.items():
                    existing = {column["name"] for column in inspect(connection).get_columns(table)}
                    for column, definition in columns.items():
                        if column not in existing:
                            connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

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

    def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("Task list limit must be between 1 and 100")
        with self.get_session() as session:
            rows = (session.query(TaskRow)
                    .order_by(TaskRow.created_at.desc(), TaskRow.task_id.desc())
                    .limit(limit).all())
            return [{"task_id": row.task_id, "description": row.description,
                     "status": row.status, "created_at": row.created_at.isoformat(),
                     "updated_at": row.updated_at.isoformat()}
                    for row in rows]

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
                tokens_reserved=quota.tokens_reserved,
                tokens_uncertain=quota.tokens_uncertain,
                max_cost_usd_budget=quota.max_cost_usd_budget,
                cost_usd_consumed=quota.cost_usd_consumed,
                cost_usd_reserved=quota.cost_usd_reserved,
                cost_usd_uncertain=quota.cost_usd_uncertain,
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
                tokens_reserved=row.tokens_reserved,
                tokens_uncertain=row.tokens_uncertain,
                max_cost_usd_budget=row.max_cost_usd_budget,
                cost_usd_consumed=row.cost_usd_consumed,
                cost_usd_reserved=row.cost_usd_reserved,
                cost_usd_uncertain=row.cost_usd_uncertain,
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
                tokens_reserved=row.tokens_reserved,
                tokens_uncertain=row.tokens_uncertain,
                max_cost_usd_budget=row.max_cost_usd_budget,
                cost_usd_consumed=row.cost_usd_consumed,
                cost_usd_reserved=row.cost_usd_reserved,
                cost_usd_uncertain=row.cost_usd_uncertain,
                is_active=row.is_active,
            )

    # --- Ticket Operations ---
    def create_ticket(self, ticket: Ticket) -> Ticket:
        with self.get_session() as session:
            with session.begin():
                reservation = session.execute(
                    update(QuotaRecordRow)
                    .where(
                        QuotaRecordRow.task_id == ticket.task_id,
                        QuotaRecordRow.is_active.is_(True),
                        QuotaRecordRow.max_tokens_budget - QuotaRecordRow.tokens_consumed - QuotaRecordRow.tokens_reserved - QuotaRecordRow.tokens_uncertain >= ticket.max_tokens_allocated,
                        QuotaRecordRow.max_cost_usd_budget - QuotaRecordRow.cost_usd_consumed - QuotaRecordRow.cost_usd_reserved - QuotaRecordRow.cost_usd_uncertain >= ticket.max_cost_usd,
                    )
                    .values(
                        tokens_reserved=QuotaRecordRow.tokens_reserved + ticket.max_tokens_allocated,
                        cost_usd_reserved=QuotaRecordRow.cost_usd_reserved + ticket.max_cost_usd,
                    )
                )
                if reservation.rowcount != 1:
                    raise ValueError("Insufficient unreserved task budget")
                session.add(TicketRow(
                    ticket_id=ticket.ticket_id, task_id=ticket.task_id,
                    parent_ticket_id=ticket.parent_ticket_id, depth=ticket.depth,
                    capability=ticket.capability, objective=ticket.objective,
                    constraints_json=json.dumps(ticket.constraints),
                    context_slice_json=json.dumps(ticket.context_slice),
                    workspace_path=ticket.workspace_path,
                    source_commit=ticket.source_commit,
                    permitted_local_capabilities_json=json.dumps(ticket.permitted_local_capabilities),
                    gateway_allowance_json=json.dumps(ticket.gateway_allowance),
                    assigned_model=ticket.assigned_model,
                    attached_tool_schema_json=(ticket.attached_tool_schema.model_dump_json() if ticket.attached_tool_schema else None),
                    max_tokens_allocated=ticket.max_tokens_allocated,
                    max_cost_usd=ticket.max_cost_usd, status=ticket.status.value,
                ))
        return ticket

    @staticmethod
    def _ticket_from_row(row: TicketRow) -> Ticket:
        return Ticket(
            ticket_id=row.ticket_id, task_id=row.task_id,
            parent_ticket_id=row.parent_ticket_id, depth=row.depth,
            capability=row.capability, objective=row.objective,
            constraints=json.loads(row.constraints_json),
            context_slice=json.loads(row.context_slice_json),
            workspace_path=row.workspace_path,
            source_commit=row.source_commit,
            permitted_local_capabilities=json.loads(row.permitted_local_capabilities_json),
            gateway_allowance=json.loads(row.gateway_allowance_json),
            assigned_model=row.assigned_model,
            attached_tool_schema=(ToolSchema.model_validate_json(row.attached_tool_schema_json) if row.attached_tool_schema_json else None),
            max_tokens_allocated=row.max_tokens_allocated,
            max_cost_usd=row.max_cost_usd,
            status=TicketStatus(row.status), created_at=row.created_at,
        )

    def get_ticket(self, ticket_id: str) -> Optional[Ticket]:
        with self.get_session() as session:
            row = session.query(TicketRow).filter(TicketRow.ticket_id == ticket_id).first()
            if not row:
                return None

            return self._ticket_from_row(row)

    def list_pending_tickets(self, limit: int = 10) -> List[Ticket]:
        with self.get_session() as session:
            rows = (
                session.query(TicketRow)
                .filter(TicketRow.status == TicketStatus.PENDING.value)
                .order_by(TicketRow.created_at.asc())
                .limit(limit)
                .all()
            )
            return [self._ticket_from_row(row) for row in rows]

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

    def claim_ticket(self, ticket_id: str, owner: Optional[str] = None) -> bool:
        now = self._utc_now()
        owner = owner or uuid.uuid4().hex
        with self.get_session() as session:
            with session.begin():
                result = session.execute(
                    update(TicketRow)
                    .where(TicketRow.ticket_id == ticket_id, TicketRow.status == TicketStatus.PENDING.value)
                    .values(status=TicketStatus.RUNNING.value, claimed_at=now,
                            claim_owner=owner,
                            lease_expires_at=now + timedelta(seconds=self.CLAIM_LEASE_SECONDS))
                )
                return result.rowcount == 1

    def owns_claim(self, ticket_id: str, owner: str) -> bool:
        with self.get_session() as session:
            return session.query(TicketRow.ticket_id).filter(
                TicketRow.ticket_id == ticket_id,
                TicketRow.status == TicketStatus.RUNNING.value,
                TicketRow.claim_owner == owner,
            ).first() is not None

    def renew_claim(self, ticket_id: str, owner: str) -> bool:
        with self.get_session() as session:
            with session.begin():
                result = session.execute(
                    update(TicketRow)
                    .where(TicketRow.ticket_id == ticket_id, TicketRow.status == TicketStatus.RUNNING.value,
                           TicketRow.claim_owner == owner)
                    .values(lease_expires_at=self._utc_now() + timedelta(seconds=self.CLAIM_LEASE_SECONDS))
                )
                return result.rowcount == 1

    def recover_abandoned_ticket(self, ticket_id: str, reason: str,
                                 *, worker_exited: bool = False,
                                 expected_owner: Optional[str] = None,
                                 now: Optional[datetime] = None) -> bool:
        """Halt an expired claim, or a child known to have exited without a report.

        Unknown gateway usage is held as uncertain budget so it cannot be spent
        again. The ticket is never automatically replayed against its clone.
        """
        cutoff = (now or self._utc_now()).replace(tzinfo=None)
        if worker_exited and not expected_owner:
            raise ValueError("An exited worker's claim owner is required for recovery")
        with self.get_session() as session:
            with session.begin():
                conditions = [TicketRow.ticket_id == ticket_id,
                              TicketRow.status == TicketStatus.RUNNING.value]
                if not worker_exited:
                    conditions.append(or_(TicketRow.lease_expires_at.is_(None),
                                          TicketRow.lease_expires_at <= cutoff))
                else:
                    conditions.append(TicketRow.claim_owner == expected_owner)
                result = session.execute(
                    update(TicketRow).where(*conditions)
                    .values(status=TicketStatus.HALTED.value, lease_expires_at=None)
                )
                if result.rowcount != 1:
                    return False
                ticket = session.get(TicketRow, ticket_id)
                session.add(WorkerReportRow(
                    report_id=WorkerReport(ticket_id=ticket_id, task_id=ticket.task_id,
                                           status="halted").report_id,
                    ticket_id=ticket_id, task_id=ticket.task_id, status="halted",
                    output_json=json.dumps({"error": reason, "usage": "unverified"}),
                    stderr=reason, unresolved_failures_json=json.dumps([reason]),
                ))
                reserved = session.execute(
                    update(QuotaRecordRow)
                    .where(QuotaRecordRow.task_id == ticket.task_id,
                           QuotaRecordRow.tokens_reserved >= ticket.max_tokens_allocated,
                           QuotaRecordRow.cost_usd_reserved >= ticket.max_cost_usd)
                    .values(
                        tokens_reserved=QuotaRecordRow.tokens_reserved - ticket.max_tokens_allocated,
                        cost_usd_reserved=QuotaRecordRow.cost_usd_reserved - ticket.max_cost_usd,
                        tokens_uncertain=QuotaRecordRow.tokens_uncertain + ticket.max_tokens_allocated,
                        cost_usd_uncertain=QuotaRecordRow.cost_usd_uncertain + ticket.max_cost_usd,
                    )
                )
                if reserved.rowcount != 1:
                    raise ValueError("Ticket reservation is missing during recovery")
                session.execute(update(TaskRow).where(TaskRow.task_id == ticket.task_id)
                                .values(status="halted"))
                return True

    def recover_expired_claims(self, task_id: str) -> list[str]:
        now = self._utc_now()
        with self.get_session() as session:
            ticket_ids = [ticket_id for (ticket_id,) in session.query(TicketRow.ticket_id).filter(
                TicketRow.task_id == task_id,
                TicketRow.status == TicketStatus.RUNNING.value,
                or_(TicketRow.lease_expires_at.is_(None), TicketRow.lease_expires_at <= now),
            ).all()]
        return [ticket_id for ticket_id in ticket_ids
                if self.recover_abandoned_ticket(ticket_id, "Worker claim expired before a report", now=now)]

    # --- Worker Report Operations ---
    def save_worker_report(self, report: WorkerReport) -> WorkerReport:
        with self.get_session() as session:
            with session.begin():
                ticket = session.get(TicketRow, report.ticket_id)
                if ticket is None or ticket.task_id != report.task_id or ticket.status != TicketStatus.RUNNING.value:
                    raise ValueError("Ticket is not claimed by this task")
                final_status = (
                    TicketStatus.COMPLETED.value if report.status == "success" else
                    TicketStatus.HALTED.value if report.status == "halted" else
                    TicketStatus.FAILED.value
                )
                claimed = session.execute(update(TicketRow).where(
                    TicketRow.ticket_id == report.ticket_id,
                    TicketRow.status == TicketStatus.RUNNING.value,
                ).values(status=final_status, lease_expires_at=None))
                if claimed.rowcount != 1:
                    raise ValueError("Ticket claim was already finalized")
                session.add(WorkerReportRow(
                    report_id=report.report_id, ticket_id=report.ticket_id,
                    task_id=report.task_id, status=report.status,
                    output_json=json.dumps(report.output), stdout=report.stdout,
                    stderr=report.stderr, diff=report.diff,
                    commands_executed_json=json.dumps(report.commands_executed),
                    test_results_json=json.dumps(report.test_results),
                    unresolved_failures_json=json.dumps(report.unresolved_failures),
                    tokens_used=report.tokens_used, cost_usd=report.cost_usd,
                    child_proposals_json=json.dumps([p.model_dump() for p in report.child_ticket_proposals]),
                ))
                charged = session.execute(
                    update(QuotaRecordRow)
                    .where(QuotaRecordRow.task_id == report.task_id,
                           QuotaRecordRow.tokens_reserved >= ticket.max_tokens_allocated,
                           QuotaRecordRow.cost_usd_reserved >= ticket.max_cost_usd)
                    .values(
                        tokens_consumed=QuotaRecordRow.tokens_consumed + report.tokens_used,
                        cost_usd_consumed=QuotaRecordRow.cost_usd_consumed + report.cost_usd,
                        tokens_reserved=QuotaRecordRow.tokens_reserved - ticket.max_tokens_allocated,
                        cost_usd_reserved=QuotaRecordRow.cost_usd_reserved - ticket.max_cost_usd,
                    )
                )
                if charged.rowcount != 1:
                    raise ValueError("Ticket reservation is missing during reporting")
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
                diff=row.diff,
                commands_executed=json.loads(row.commands_executed_json),
                test_results=json.loads(row.test_results_json),
                unresolved_failures=json.loads(row.unresolved_failures_json),
                tokens_used=row.tokens_used,
                cost_usd=row.cost_usd,
                child_ticket_proposals=proposals,
                completed_at=row.completed_at,
            )

    # --- One-time Relay Pairing ---
    def create_relay_pair(self, pair_id: str, device_id: str, secret: str,
                          relay_url: str, ttl_seconds: int = 300) -> None:
        if ttl_seconds < 1 or ttl_seconds > 300:
            raise ValueError("Pair offer TTL must be between 1 and 300 seconds")
        with self.get_session() as session:
            session.add(RelayPairRow(
                pair_id=pair_id, device_id=device_id, secret=secret,
                relay_url=relay_url,
                expires_at=self._utc_now() + timedelta(seconds=ttl_seconds),
            ))
            session.commit()

    def get_relay_pair(self, pair_id: str) -> Optional[dict[str, Any]]:
        with self.get_session() as session:
            row = session.get(RelayPairRow, pair_id)
            if not row:
                return None
            return {"pair_id": row.pair_id, "device_id": row.device_id,
                    "secret": row.secret, "relay_url": row.relay_url,
                    "status": row.status, "expires_at": row.expires_at}

    def list_live_relay_pairs(self, device_id: str) -> list[dict[str, Any]]:
        with self.get_session() as session:
            rows = session.query(RelayPairRow).filter(
                RelayPairRow.device_id == device_id,
                RelayPairRow.status.in_(["pending", "offered"]),
                RelayPairRow.expires_at > self._utc_now(),
            ).all()
            return [{"pair_id": row.pair_id, "device_id": row.device_id,
                     "secret": row.secret, "relay_url": row.relay_url,
                     "status": row.status, "expires_at": row.expires_at}
                    for row in rows]

    def set_relay_pair_status(self, pair_id: str, expected: tuple[str, ...], status: str) -> bool:
        with self.get_session() as session:
            with session.begin():
                result = session.execute(update(RelayPairRow).where(
                    RelayPairRow.pair_id == pair_id,
                    RelayPairRow.status.in_(expected),
                    RelayPairRow.expires_at > self._utc_now(),
                ).values(status=status))
                return result.rowcount == 1

    def consume_relay_pair(self, pair_id: str, device_id: str) -> Optional[str]:
        """Consume a one-time secret and erase its persisted copy."""
        with self.get_session() as session:
            with session.begin():
                row = session.get(RelayPairRow, pair_id)
                if (not row or row.device_id != device_id or
                        row.status not in {"pending", "offered"} or
                        row.expires_at <= self._utc_now()):
                    return None
                secret = row.secret
                row.secret = ""
                row.status = "paired"
                return secret

    def expire_relay_pairs(self) -> None:
        with self.get_session() as session:
            with session.begin():
                session.execute(update(RelayPairRow).where(
                    RelayPairRow.status.in_(["pending", "offered"]),
                    RelayPairRow.expires_at <= self._utc_now(),
                ).values(status="expired", secret=""))

    def save_grant_request(self, grant: GrantRequest) -> GrantRequest:
        with self.get_session() as session:
            with session.begin():
                session.add(GrantRequestRow(
                    grant_id=grant.grant_id, task_id=grant.task_id,
                    ticket_id=grant.ticket_id, action_type=grant.action_type,
                    details_json=json.dumps(grant.details), status=grant.status,
                ))
        return grant

    def list_grant_requests(self, task_id: str) -> List[GrantRequest]:
        with self.get_session() as session:
            rows = session.query(GrantRequestRow).filter(GrantRequestRow.task_id == task_id).order_by(GrantRequestRow.created_at).all()
            return [GrantRequest(
                grant_id=row.grant_id, task_id=row.task_id,
                ticket_id=row.ticket_id, action_type=row.action_type,
                details=json.loads(row.details_json), status=row.status,
                decided_by=row.decided_by, decided_at=row.decided_at,
                created_at=row.created_at,
            ) for row in rows]

    def decide_grant_request(self, grant_id: str, approved: bool, operator_id: str) -> GrantRequest:
        if not operator_id.strip():
            raise ValueError("An operator identity is required")
        decided_at = datetime.now(timezone.utc)
        with self.get_session() as session:
            with session.begin():
                result = session.execute(
                    update(GrantRequestRow)
                    .where(GrantRequestRow.grant_id == grant_id, GrantRequestRow.status == "pending")
                    .values(status="approved" if approved else "rejected",
                            decided_by=operator_id, decided_at=decided_at)
                )
                if result.rowcount != 1:
                    raise ValueError("Grant request does not exist or has already been decided")
        with self.get_session() as session:
            row = session.get(GrantRequestRow, grant_id)
            return GrantRequest(
                grant_id=row.grant_id, task_id=row.task_id, ticket_id=row.ticket_id,
                action_type=row.action_type, details=json.loads(row.details_json),
                status=row.status, decided_by=row.decided_by, decided_at=row.decided_at,
                created_at=row.created_at,
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
