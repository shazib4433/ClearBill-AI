"""Pydantic v2 schemas for Gemini Structured Outputs and ClearBill API contracts."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class AIClassification(str, Enum):
    VERIFIED_CLEAN = "VERIFIED_CLEAN"
    FLAGGED_DUPLICATE = "FLAGGED_DUPLICATE"
    FLAGGED_UNBUNDLED = "FLAGGED_UNBUNDLED"
    FLAGGED_NO_MEDICAL_NECESSITY = "FLAGGED_NO_MEDICAL_NECESSITY"
    FLAGGED_TARIFF_CAP_EXCEEDED = "FLAGGED_TARIFF_CAP_EXCEEDED"


class BillingState(str, Enum):
    PENDING_AUDIT = "PENDING_AUDIT"
    VERIFIED_CLEAN = "VERIFIED_CLEAN"
    FLAGGED_STAGING = "FLAGGED_STAGING"
    REMOVED_CONFIRMED = "REMOVED_CONFIRMED"
    RESTORED_WITH_OVERRIDE = "RESTORED_WITH_OVERRIDE"
    FINAL_APPROVED = "FINAL_APPROVED"


class LineItemAction(str, Enum):
    CONFIRM_REMOVAL = "CONFIRM_REMOVAL"
    RESTORE_WITH_OVERRIDE = "RESTORE_WITH_OVERRIDE"
    REFLAG_ITEM = "REFLAG_ITEM"


# ---------------------------------------------------------------------------
# Stage 3 Structured Output Schemas (gemini-3.5-flash-lite)
# ---------------------------------------------------------------------------


class NormalizedLineItem(BaseModel):
    item_id: str = Field(description="Original billing ledger item_id.")
    canonical_service: str = Field(description="Normalized canonical clinical service name.")
    service_category: str = Field(description="Normalized category (PACKAGE, IMPLANT, DIAGNOSTICS, etc.).")
    service_date: str = Field(description="ISO service date YYYY-MM-DD.")
    shift: str = Field(description="Nursing/billing shift (Morning, Evening, Night).")
    total_amount: float = Field(description="Billed total line-item amount in INR.")


class ClinicalEventSummary(BaseModel):
    ordered_services: list[str] = Field(default_factory=list, description="Key services explicitly ordered by clinicians.")
    ward_transfer_events: list[str] = Field(
        default_factory=list, description="Documented ICU/ward step-down or transfer chronology."
    )
    package_inclusions: list[str] = Field(
        default_factory=list, description="Active payer surgical/medical package inclusions."
    )
    clinical_justifications: list[str] = Field(
        default_factory=list, description="Special clinical deterioration or serial monitoring notes."
    )


class NormalizedClinicalLedger(BaseModel):
    uhid: str = Field(description="Patient UHID.")
    payer_category: str = Field(description="Detected payer category.")
    normalized_items: list[NormalizedLineItem] = Field(default_factory=list)
    clinical_summary: ClinicalEventSummary = Field(default_factory=ClinicalEventSummary)


# ---------------------------------------------------------------------------
# Stage 5 Structured Output Schemas (gemini-3.8-flash)
# ---------------------------------------------------------------------------


class EvidenceCitation(BaseModel):
    source_type: str = Field(description="DOCTOR_ORDER | CLINICAL_NOTE | NURSING_LOG | LAB_REPORT | PAYER_RULE")
    source_id: str = Field(description="Record ID (e.g., ORD-1001-01, NOTE-1006-01, REP-1004-03).")
    excerpt: str = Field(description="Concise clinical or tariff excerpt supporting the decision.")


class LineItemAuditDecision(BaseModel):
    item_id: str = Field(description="Billing ledger item_id being evaluated.")
    classification: AIClassification = Field(description="Audit classification.")
    allowed_cap_amount: float | None = Field(
        default=None,
        description="Maximum allowed payable INR for FLAGGED_TARIFF_CAP_EXCEEDED items; null otherwise.",
    )
    potential_savings: float = Field(
        default=0.0,
        ge=0.0,
        description="Exact INR overbilling / leakage detected on this line item (0.0 if VERIFIED_CLEAN).",
    )
    plain_english_remark: str = Field(
        description="Clear, plain-English clinical and tariff explanation for billing reviewers."
    )
    payer_rule_cited: str = Field(description="Specific payer rulebook clause applied.")
    clinical_evidence_citations: list[EvidenceCitation] = Field(default_factory=list)
    confidence: float = Field(default=0.95, ge=0.0, le=1.0)


class BillingAuditResult(BaseModel):
    uhid: str
    payer_category: str
    audit_summary: str = Field(description="Executive summary of billing audit findings.")
    decisions: list[LineItemAuditDecision] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# API Request / Response Contracts
# ---------------------------------------------------------------------------


class TransitionRequest(BaseModel):
    action: LineItemAction
    employee_uid: str = Field(..., min_length=3, max_length=32)
    justification_remark: str = Field(..., min_length=1)


class FinalizeRequest(BaseModel):
    employee_uid: str = Field(..., min_length=3, max_length=32)
    justification_remark: str = Field(..., min_length=1)


class EmployeeResponse(BaseModel):
    employee_uid: str
    full_name: str
    role: str
    department: str
    max_override_limit: float | None
    override_limit_display: str
    can_finalize_bill: bool
    is_active: bool


class PatientListItem(BaseModel):
    uhid: str
    masked_name: str
    age: int
    gender: str
    payer_category: str
    payer_name: str
    department: str
    primary_diagnosis: str
    admission_date: str
    discharge_date: str
    gross_billed_amount: float
    latest_session_id: str | None = None
    bill_state: str = "PENDING_AUDIT"
    flagged_count: int = 0
    potential_savings: float = 0.0


class StagedLineItemResponse(BaseModel):
    id: int
    item_id: str
    service_date: str
    service_code: str
    service_name: str
    category: str
    department: str
    posted_by_shift: str
    quantity: float
    unit_price: float
    total_amount: float
    allowed_cap_amount: float | None
    potential_savings: float
    payable_amount: float
    ai_classification: str
    current_state: str
    bucket: str
    plain_english_remark: str
    clinical_evidence_citations: list[EvidenceCitation]
    payer_rule_cited: str
    confidence: float
    last_modified_by_uid: str | None = None
    last_modified_by_name: str | None = None
    last_justification_remark: str | None = None
    updated_at: str


class PipelineStageTrace(BaseModel):
    stage_number: int
    name: str
    status: str
    duration_ms: float
    detail: str


class AuditSessionResponse(BaseModel):
    session_id: str
    uhid: str
    source_type: str
    patient_display_masked: str
    payer_category: str
    payer_name: str
    department: str
    primary_diagnosis: str
    skill_loaded: str
    skill_path: str
    engine_mode: str
    models_used: str
    fallback_notice: str | None = None
    bill_state: str
    total_billed_amount: float
    clean_verified_amount: float
    flagged_staging_amount: float
    potential_savings_amount: float
    confirmed_savings_amount: float
    net_payable_amount: float
    readiness_score: float
    clean_count: int
    staging_count: int
    removed_count: int
    line_items: list[StagedLineItemResponse]
    phi_redaction_stats: dict[str, Any]
    raw_his_payload: dict[str, Any]
    redacted_payload: dict[str, Any]
    pipeline_trace: list[PipelineStageTrace]
    finalized_by_uid: str | None = None
    finalized_by_name: str | None = None
    finalized_by_role: str | None = None
    finalized_at: str | None = None
    finalization_remark: str | None = None
    created_at: str
    updated_at: str


class AuditLogResponse(BaseModel):
    audit_id: str
    timestamp: str
    uhid: str
    session_id: str
    item_id: str
    service_code: str
    service_name: str
    action_type: str
    previous_state: str
    new_state: str
    financial_impact: float
    employee_uid: str
    employee_name: str
    employee_role: str
    justification_remark: str


class SkillInfoResponse(BaseModel):
    slug: str
    name: str
    description: str
    path: str
    content: str


class HealthResponse(BaseModel):
    status: str
    engine_mode: str
    auth_mode: str = "DEMO_FALLBACK"
    ingest_model: str
    fusion_model: str
    his_db_readonly: bool
    his_db_exists: bool
    audit_db_exists: bool
    skills_loaded: int
