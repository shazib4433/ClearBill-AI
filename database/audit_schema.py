"""SQLAlchemy ORM definitions and immutability triggers for ClearBill Audit & Staging DB."""

from __future__ import annotations

from sqlalchemy import Engine, Float, ForeignKey, Integer, String, Text, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class AuditBase(DeclarativeBase):
    """Declarative base for ClearBill Staging & Immutable Audit tables."""


class AuditSession(AuditBase):
    """Tracks each UHID or uploaded bill reconciliation run and summary metrics."""

    __tablename__ = "audit_sessions"

    session_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    uhid: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    patient_display_masked: Mapped[str] = mapped_column(String(160), nullable=False)
    payer_category: Mapped[str] = mapped_column(String(32), nullable=False)
    payer_name: Mapped[str] = mapped_column(String(120), nullable=False)
    department: Mapped[str] = mapped_column(String(80), nullable=False)
    primary_diagnosis: Mapped[str] = mapped_column(String(255), nullable=False)
    skill_loaded: Mapped[str] = mapped_column(String(80), nullable=False)
    skill_path: Mapped[str] = mapped_column(String(255), nullable=False)
    engine_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    models_used: Mapped[str] = mapped_column(String(120), nullable=False)
    fallback_notice: Mapped[str | None] = mapped_column(Text, nullable=True)
    bill_state: Mapped[str] = mapped_column(String(32), nullable=False)
    total_billed_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    clean_verified_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    flagged_staging_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    potential_savings_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    confirmed_savings_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    net_payable_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    readiness_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    raw_his_payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    redacted_payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    phi_redaction_stats_json: Mapped[str] = mapped_column(Text, nullable=False)
    pipeline_trace_json: Mapped[str] = mapped_column(Text, nullable=False)
    finalized_by_uid: Mapped[str | None] = mapped_column(String(32), nullable=True)
    finalized_by_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    finalized_by_role: Mapped[str | None] = mapped_column(String(48), nullable=True)
    finalized_at: Mapped[str | None] = mapped_column(String(48), nullable=True)
    finalization_remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(48), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(48), nullable=False)


class StagedLineItem(AuditBase):
    """Per-line-item classification, staging isolation state, and clinical evidence citations."""

    __tablename__ = "staged_line_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(48), ForeignKey("audit_sessions.session_id"), index=True, nullable=False
    )
    uhid: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    item_id: Mapped[str] = mapped_column(String(48), nullable=False)
    service_date: Mapped[str] = mapped_column(String(32), nullable=False)
    service_code: Mapped[str] = mapped_column(String(48), nullable=False)
    service_name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(48), nullable=False)
    department: Mapped[str] = mapped_column(String(80), nullable=False)
    posted_by_shift: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    unit_price: Mapped[float] = mapped_column(Float, nullable=False)
    total_amount: Mapped[float] = mapped_column(Float, nullable=False)
    allowed_cap_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    potential_savings: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    payable_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    ai_classification: Mapped[str] = mapped_column(String(48), nullable=False)
    current_state: Mapped[str] = mapped_column(String(32), nullable=False)
    bucket: Mapped[str] = mapped_column(String(24), nullable=False)
    plain_english_remark: Mapped[str] = mapped_column(Text, nullable=False)
    clinical_evidence_citations_json: Mapped[str] = mapped_column(Text, nullable=False)
    payer_rule_cited: Mapped[str] = mapped_column(String(200), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.95)
    last_modified_by_uid: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_modified_by_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    last_justification_remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[str] = mapped_column(String(48), nullable=False)


class AuditLog(AuditBase):
    """Append-only immutable audit ledger protected by SQLite UPDATE/DELETE abort triggers."""

    __tablename__ = "audit_logs"

    audit_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    timestamp: Mapped[str] = mapped_column(String(48), index=True, nullable=False)
    uhid: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    session_id: Mapped[str] = mapped_column(String(48), index=True, nullable=False)
    item_id: Mapped[str] = mapped_column(String(48), nullable=False)
    service_code: Mapped[str] = mapped_column(String(48), nullable=False)
    service_name: Mapped[str] = mapped_column(String(255), nullable=False)
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)
    previous_state: Mapped[str] = mapped_column(String(32), nullable=False)
    new_state: Mapped[str] = mapped_column(String(32), nullable=False)
    financial_impact: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    employee_uid: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    employee_name: Mapped[str] = mapped_column(String(120), nullable=False)
    employee_role: Mapped[str] = mapped_column(String(48), nullable=False)
    justification_remark: Mapped[str] = mapped_column(Text, nullable=False)


def create_audit_immutability_triggers(engine: Engine) -> None:
    """Install SQLite BEFORE UPDATE and BEFORE DELETE triggers to keep audit_logs append-only."""
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TRIGGER IF NOT EXISTS prevent_audit_log_update
                BEFORE UPDATE ON audit_logs
                BEGIN
                    SELECT RAISE(ABORT, 'IMMUTABLE_AUDIT_LOG: UPDATE operations are prohibited on audit_logs');
                END;
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TRIGGER IF NOT EXISTS prevent_audit_log_delete
                BEFORE DELETE ON audit_logs
                BEGIN
                    SELECT RAISE(ABORT, 'IMMUTABLE_AUDIT_LOG: DELETE operations are prohibited on audit_logs');
                END;
                """
            )
        )
