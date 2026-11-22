"""7-Stage UHID-Triggered Reconciliation Pipeline Orchestrator."""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.config import Settings, get_settings
from backend.file_parser import parse_uploaded_bill
from backend.gemini_pipeline import run_stage3_ingestion, run_stage5_fusion
from backend.governance import recompute_session_metrics, utc_now_iso
from backend.phi_redactor import mask_display_name, redact_his_bundle
from backend.schemas import (
    AIClassification,
    AuditSessionResponse,
    BillingState,
    EvidenceCitation,
    PipelineStageTrace,
    StagedLineItemResponse,
)
from backend.skill_loader import load_payer_skill
from database.audit_schema import AuditLog, AuditSession, StagedLineItem
from database.his_schema import (
    Admission,
    BillingLedger,
    ClinicalNote,
    DoctorOrder,
    LabReport,
    NursingLog,
    Patient,
)


_uhid_locks_guard = threading.Lock()
_uhid_locks: dict[str, threading.Lock] = {}


def _get_uhid_lock(uhid: str) -> threading.Lock:
    """Serialize reconciliation pipeline runs per UHID so concurrent requests cannot overwrite newer sessions."""
    with _uhid_locks_guard:
        lock = _uhid_locks.get(uhid)
        if lock is None:
            lock = threading.Lock()
            _uhid_locks[uhid] = lock
        return lock


def _collect_prior_decisions(
    audit_db: Session,
    clean_uhid: str,
    existing: AuditSession | None,
) -> dict[str, StagedLineItem]:
    """Collect governed line-item states from the latest session and fall back to immutable AuditLog history."""
    prior_decisions: dict[str, StagedLineItem] = {}
    if existing is not None:
        existing_items = (
            audit_db.query(StagedLineItem)
            .filter(StagedLineItem.session_id == existing.session_id)
            .all()
        )
        for it in existing_items:
            if it.last_modified_by_uid is not None:
                prior_decisions[it.item_id] = it

    latest_logs = (
        audit_db.query(AuditLog)
        .filter(AuditLog.uhid == clean_uhid, AuditLog.item_id != "BILL_HEADER")
        .order_by(AuditLog.timestamp.desc(), text("rowid DESC"))
        .all()
    )
    seen_log_items: set[str] = set()
    for log in latest_logs:
        if log.item_id in seen_log_items:
            continue
        seen_log_items.add(log.item_id)
        if log.item_id in prior_decisions:
            continue
        hist_item = (
            audit_db.query(StagedLineItem)
            .filter(
                StagedLineItem.session_id == log.session_id,
                StagedLineItem.item_id == log.item_id,
            )
            .first()
        )
        if hist_item is not None and hist_item.last_modified_by_uid is not None:
            prior_decisions[hist_item.item_id] = hist_item

    return prior_decisions


def _fetch_his_bundle(his_db: Session, uhid: str) -> dict[str, Any]:
    """Stage 1A: Fetch read-only inpatient clinical & billing records for a Patient UHID."""
    clean_uhid = uhid.strip().upper()
    patient = his_db.query(Patient).filter(Patient.uhid == clean_uhid).first()
    if patient is None:
        raise HTTPException(
            status_code=404,
            detail=f"Patient UHID '{uhid}' not found in Hospital Information System (HIS).",
        )
    admission = his_db.query(Admission).filter(Admission.uhid == clean_uhid).first()
    if admission is None:
        raise HTTPException(
            status_code=404,
            detail=f"No inpatient admission record found for UHID '{clean_uhid}'.",
        )

    ledger = (
        his_db.query(BillingLedger)
        .filter(BillingLedger.uhid == clean_uhid)
        .order_by(BillingLedger.item_id.asc())
        .all()
    )
    orders = (
        his_db.query(DoctorOrder)
        .filter(DoctorOrder.uhid == clean_uhid)
        .order_by(DoctorOrder.order_id.asc())
        .all()
    )
    notes = (
        his_db.query(ClinicalNote)
        .filter(ClinicalNote.uhid == clean_uhid)
        .order_by(ClinicalNote.note_id.asc())
        .all()
    )
    nursing = (
        his_db.query(NursingLog)
        .filter(NursingLog.uhid == clean_uhid)
        .order_by(NursingLog.log_id.asc())
        .all()
    )
    labs = (
        his_db.query(LabReport)
        .filter(LabReport.uhid == clean_uhid)
        .order_by(LabReport.report_id.asc())
        .all()
    )

    return {
        "patient": {
            "uhid": patient.uhid,
            "full_name": patient.full_name,
            "age": patient.age,
            "gender": patient.gender,
            "phone": patient.phone,
            "email": patient.email,
            "address": patient.address,
            "gov_id_type": patient.gov_id_type,
            "gov_id_number": patient.gov_id_number,
            "payer_category": patient.payer_category,
            "payer_name": patient.payer_name,
            "policy_or_card_no": patient.policy_or_card_no,
            "room_entitlement": patient.room_entitlement,
            "room_entitlement_cap_per_day": patient.room_entitlement_cap_per_day,
        },
        "admission": {
            "admission_id": admission.admission_id,
            "uhid": admission.uhid,
            "admission_date": admission.admission_date,
            "discharge_date": admission.discharge_date,
            "department": admission.department,
            "attending_doctor": admission.attending_doctor,
            "primary_diagnosis": admission.primary_diagnosis,
            "icd10_code": admission.icd10_code,
            "package_code": admission.package_code,
            "package_name": admission.package_name,
            "package_rate": admission.package_rate,
            "room_category_billed": admission.room_category_billed,
            "patient_upgrade_consent_signed": admission.patient_upgrade_consent_signed,
            "discharge_summary_brief": admission.discharge_summary_brief,
        },
        "billing_ledger": [
            {
                "item_id": r.item_id,
                "service_date": r.service_date,
                "service_code": r.service_code,
                "service_name": r.service_name,
                "category": r.category,
                "department": r.department,
                "quantity": r.quantity,
                "unit_price": r.unit_price,
                "total_amount": r.total_amount,
                "posted_by_shift": r.posted_by_shift,
                "tariff_notes": r.tariff_notes,
            }
            for r in ledger
        ],
        "doctor_orders": [
            {
                "order_id": o.order_id,
                "order_timestamp": o.order_timestamp,
                "ordered_by": o.ordered_by,
                "order_type": o.order_type,
                "order_details": o.order_details,
                "frequency": o.frequency,
                "status": o.status,
            }
            for o in orders
        ],
        "clinical_notes": [
            {
                "note_id": n.note_id,
                "note_timestamp": n.note_timestamp,
                "author_name": n.author_name,
                "author_role": n.author_role,
                "note_type": n.note_type,
                "note_text": n.note_text,
            }
            for n in notes
        ],
        "nursing_logs": [
            {
                "log_id": l.log_id,
                "log_timestamp": l.log_timestamp,
                "shift": l.shift,
                "nurse_name": l.nurse_name,
                "ward_location": l.ward_location,
                "entry_text": l.entry_text,
            }
            for l in nursing
        ],
        "lab_reports": [
            {
                "report_id": rep.report_id,
                "sample_collected_at": rep.sample_collected_at,
                "reported_at": rep.reported_at,
                "test_name": rep.test_name,
                "result_summary": rep.result_summary,
                "verified_by": rep.verified_by,
            }
            for rep in labs
        ],
    }


def serialize_session(session: AuditSession, items: list[StagedLineItem]) -> AuditSessionResponse:
    """Convert ORM AuditSession + StagedLineItems into API AuditSessionResponse."""
    line_responses: list[StagedLineItemResponse] = []
    clean_count = 0
    staging_count = 0
    removed_count = 0

    for it in items:
        if it.bucket == "STAGING":
            staging_count += 1
        elif it.bucket == "REMOVED":
            removed_count += 1
        else:
            clean_count += 1

        raw_citations = json.loads(it.clinical_evidence_citations_json or "[]")
        citations = [EvidenceCitation(**c) for c in raw_citations]
        line_responses.append(
            StagedLineItemResponse(
                id=it.id,
                item_id=it.item_id,
                service_date=it.service_date,
                service_code=it.service_code,
                service_name=it.service_name,
                category=it.category,
                department=it.department,
                posted_by_shift=it.posted_by_shift,
                quantity=it.quantity,
                unit_price=it.unit_price,
                total_amount=it.total_amount,
                allowed_cap_amount=it.allowed_cap_amount,
                potential_savings=it.potential_savings,
                payable_amount=it.payable_amount,
                ai_classification=it.ai_classification,
                current_state=it.current_state,
                bucket=it.bucket,
                plain_english_remark=it.plain_english_remark,
                clinical_evidence_citations=citations,
                payer_rule_cited=it.payer_rule_cited,
                confidence=it.confidence,
                last_modified_by_uid=it.last_modified_by_uid,
                last_modified_by_name=it.last_modified_by_name,
                last_justification_remark=it.last_justification_remark,
                updated_at=it.updated_at,
            )
        )

    raw_trace = json.loads(session.pipeline_trace_json or "[]")
    trace = [PipelineStageTrace(**t) for t in raw_trace]

    return AuditSessionResponse(
        session_id=session.session_id,
        uhid=session.uhid,
        source_type=session.source_type,
        patient_display_masked=session.patient_display_masked,
        payer_category=session.payer_category,
        payer_name=session.payer_name,
        department=session.department,
        primary_diagnosis=session.primary_diagnosis,
        skill_loaded=session.skill_loaded,
        skill_path=session.skill_path,
        engine_mode=session.engine_mode,
        models_used=session.models_used,
        fallback_notice=session.fallback_notice,
        bill_state=session.bill_state,
        total_billed_amount=session.total_billed_amount,
        clean_verified_amount=session.clean_verified_amount,
        flagged_staging_amount=session.flagged_staging_amount,
        potential_savings_amount=session.potential_savings_amount,
        confirmed_savings_amount=session.confirmed_savings_amount,
        net_payable_amount=session.net_payable_amount,
        readiness_score=session.readiness_score,
        clean_count=clean_count,
        staging_count=staging_count,
        removed_count=removed_count,
        line_items=line_responses,
        phi_redaction_stats=json.loads(session.phi_redaction_stats_json or "{}"),
        raw_his_payload=json.loads(session.raw_his_payload_json or "{}"),
        redacted_payload=json.loads(session.redacted_payload_json or "{}"),
        pipeline_trace=trace,
        finalized_by_uid=session.finalized_by_uid,
        finalized_by_name=session.finalized_by_name,
        finalized_by_role=session.finalized_by_role,
        finalized_at=session.finalized_at,
        finalization_remark=session.finalization_remark,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


def _execute_stages_2_to_6(
    audit_db: Session,
    raw_bundle: dict[str, Any],
    source_type: str,
    stage1_ms: float,
    prior_decisions: dict[str, StagedLineItem] | None = None,
) -> AuditSessionResponse:
    """Execute Stages 2 through 6 and persist AuditSession + StagedLineItems."""
    settings = get_settings()
    patient = raw_bundle["patient"]
    admission = raw_bundle["admission"]
    ledger = raw_bundle["billing_ledger"]

    trace: list[dict[str, Any]] = [
        {
            "stage_number": 1,
            "name": "Read-Only HIS Fetch" if source_type == "HIS_UHID" else f"Ledger Intake ({source_type})",
            "status": "COMPLETED",
            "duration_ms": round(stage1_ms, 1),
            "detail": (
                f"Fetched {len(ledger)} ledger items, {len(raw_bundle['doctor_orders'])} orders, "
                f"{len(raw_bundle['clinical_notes'])} notes, {len(raw_bundle['nursing_logs'])} nursing logs, "
                f"{len(raw_bundle['lab_reports'])} lab reports."
            ),
        }
    ]

    # Stage 2: Local PHI De-Identification
    t2 = time.perf_counter()
    redacted_bundle, phi_stats = redact_his_bundle(raw_bundle)
    trace.append(
        {
            "stage_number": 2,
            "name": "Local PHI De-Identification",
            "status": "COMPLETED",
            "duration_ms": round((time.perf_counter() - t2) * 1000, 1),
            "detail": f"Masked {phi_stats['total_redactions']} sensitive PHI entities locally prior to cloud reasoning.",
        }
    )

    # Stage 3: Fast Ingestion & Normalization (gemini-3.5-flash-lite)
    t3 = time.perf_counter()
    normalized, mode3, notice3 = run_stage3_ingestion(redacted_bundle)
    trace.append(
        {
            "stage_number": 3,
            "name": f"Fast Ingestion ({settings.gemini_ingest_model})",
            "status": "COMPLETED",
            "duration_ms": round((time.perf_counter() - t3) * 1000, 1),
            "detail": f"Normalized {len(normalized.normalized_items)} billing lines via {mode3}.",
        }
    )

    # Stage 4: Dynamic Payer Skill Loading
    t4 = time.perf_counter()
    skill = load_payer_skill(patient["payer_category"])
    trace.append(
        {
            "stage_number": 4,
            "name": "Dynamic Payer Skill Load",
            "status": "COMPLETED",
            "duration_ms": round((time.perf_counter() - t4) * 1000, 1),
            "detail": f"Injected {skill.path} ({skill.name}).",
        }
    )

    # Stage 5: Clinical + Billing Context Fusion (gemini-3.8-flash)
    t5 = time.perf_counter()
    audit_result, mode5, notice5 = run_stage5_fusion(redacted_bundle, normalized, skill)
    flagged_n = sum(1 for d in audit_result.decisions if d.classification != AIClassification.VERIFIED_CLEAN)
    trace.append(
        {
            "stage_number": 5,
            "name": f"Clinical-Billing Fusion ({settings.gemini_fusion_model})",
            "status": "COMPLETED",
            "duration_ms": round((time.perf_counter() - t5) * 1000, 1),
            "detail": f"Cross-checked {len(audit_result.decisions)} items; isolated {flagged_n} anomalies via {mode5}.",
        }
    )

    # Stage 6: Staging Isolation & Plain-English Remarks
    t6 = time.perf_counter()
    merged_decisions = dict(prior_decisions or {})
    if source_type == "HIS_UHID":
        audit_db.expire_all()
        latest_existing = (
            audit_db.query(AuditSession)
            .filter(AuditSession.uhid == patient["uhid"])
            .order_by(AuditSession.created_at.desc(), text("rowid DESC"))
            .first()
        )
        if latest_existing is not None and latest_existing.bill_state == BillingState.FINAL_APPROVED.value:
            raise HTTPException(
                status_code=409,
                detail=f"Discharge bill for {patient['uhid']} is FINAL_APPROVED and locked; cannot overwrite with re-audit.",
            )
        merged_decisions.update(
            _collect_prior_decisions(audit_db, patient["uhid"], latest_existing)
        )

    now = utc_now_iso()
    session_id = f"SES-{uuid.uuid4().hex[:10].upper()}"
    engine_mode = "GEMINI_LIVE" if (mode3 == "GEMINI_LIVE" and mode5 == "GEMINI_LIVE") else "LOCAL_RULE_ENGINE"
    models_used = (
        f"{settings.gemini_ingest_model} + {settings.gemini_fusion_model}"
        if engine_mode == "GEMINI_LIVE"
        else f"Demo Clinical Rule Engine (Parity w/ {settings.gemini_ingest_model} + {settings.gemini_fusion_model})"
    )
    fallback_notice = notice5 or notice3

    decision_map = {d.item_id: d for d in audit_result.decisions}
    staged_items: list[StagedLineItem] = []

    for row in ledger:
        dec = decision_map[row["item_id"]]
        prev = merged_decisions.get(row["item_id"]) if merged_decisions else None
        is_clean = dec.classification == AIClassification.VERIFIED_CLEAN
        current_state = BillingState.VERIFIED_CLEAN.value if is_clean else BillingState.FLAGGED_STAGING.value
        bucket = "CLEAN" if is_clean else "STAGING"
        allowed_cap = dec.allowed_cap_amount
        potential_savings = float(dec.potential_savings)
        payable_amount = float(row["total_amount"])
        last_uid = None
        last_name = None
        last_remark = None

        if prev is not None:
            current_state = prev.current_state
            bucket = prev.bucket
            payable_amount = float(prev.payable_amount)
            allowed_cap = prev.allowed_cap_amount if prev.allowed_cap_amount is not None else dec.allowed_cap_amount
            potential_savings = float(prev.potential_savings) if prev.potential_savings > 0 else float(dec.potential_savings)
            last_uid = prev.last_modified_by_uid
            last_name = prev.last_modified_by_name
            last_remark = prev.last_justification_remark

        staged_items.append(
            StagedLineItem(
                session_id=session_id,
                uhid=patient["uhid"],
                item_id=row["item_id"],
                service_date=row["service_date"],
                service_code=row["service_code"],
                service_name=row["service_name"],
                category=row["category"],
                department=row["department"],
                posted_by_shift=row["posted_by_shift"],
                quantity=float(row["quantity"]),
                unit_price=float(row["unit_price"]),
                total_amount=float(row["total_amount"]),
                allowed_cap_amount=allowed_cap,
                potential_savings=potential_savings,
                payable_amount=payable_amount,
                ai_classification=dec.classification.value,
                current_state=current_state,
                bucket=bucket,
                plain_english_remark=dec.plain_english_remark,
                clinical_evidence_citations_json=json.dumps(
                    [c.model_dump() for c in dec.clinical_evidence_citations]
                ),
                payer_rule_cited=dec.payer_rule_cited,
                confidence=float(dec.confidence),
                last_modified_by_uid=last_uid,
                last_modified_by_name=last_name,
                last_justification_remark=last_remark,
                updated_at=now,
            )
        )

    remaining_staging_n = sum(1 for it in staged_items if it.bucket == "STAGING")
    trace.append(
        {
            "stage_number": 6,
            "name": "Staging Bucket Isolation",
            "status": "COMPLETED",
            "duration_ms": round((time.perf_counter() - t6) * 1000, 1),
            "detail": f"Routed {len(ledger) - flagged_n} clean items & {flagged_n} flagged items to staging.",
        }
    )
    trace.append(
        {
            "stage_number": 7,
            "name": "Governed Review & Audit Lock",
            "status": "READY" if remaining_staging_n == 0 else "AWAITING_REVIEW",
            "duration_ms": 0.0,
            "detail": (
                "All items resolved — ready for authorized final bill sign-off."
                if remaining_staging_n == 0
                else f"Awaiting authorized Employee UID review of {remaining_staging_n} staged item(s)."
            ),
        }
    )

    session = AuditSession(
        session_id=session_id,
        uhid=patient["uhid"],
        source_type=source_type,
        patient_display_masked=mask_display_name(patient["full_name"]),
        payer_category=patient["payer_category"],
        payer_name=patient["payer_name"],
        department=admission["department"],
        primary_diagnosis=admission["primary_diagnosis"],
        skill_loaded=skill.name,
        skill_path=skill.path,
        engine_mode=engine_mode,
        models_used=models_used,
        fallback_notice=fallback_notice,
        bill_state=BillingState.PENDING_AUDIT.value,
        raw_his_payload_json=json.dumps(raw_bundle),
        redacted_payload_json=json.dumps(redacted_bundle),
        phi_redaction_stats_json=json.dumps(phi_stats),
        pipeline_trace_json=json.dumps(trace),
        created_at=now,
        updated_at=now,
    )
    recompute_session_metrics(session, staged_items)

    audit_db.add(session)
    audit_db.flush()
    audit_db.add_all(staged_items)
    audit_db.commit()
    audit_db.refresh(session)
    saved_items = (
        audit_db.query(StagedLineItem)
        .filter(StagedLineItem.session_id == session_id)
        .order_by(StagedLineItem.id.asc())
        .all()
    )
    return serialize_session(session, saved_items)


def _should_auto_retry_stale_fallback(existing: AuditSession, settings: Settings) -> bool:
    """Return True only for legacy pre-fix fallback errors that should self-heal once config is healthy."""
    if existing.bill_state == BillingState.FINAL_APPROVED.value:
        return False
    if not settings.has_live_gemini_key:
        return False
    if existing.engine_mode != "LOCAL_RULE_ENGINE" or not existing.fallback_notice:
        return False

    lowered = existing.fallback_notice.lower()
    if not settings.use_vertex_ai and ("agent platform api" in lowered or "aiplatform" in lowered):
        return True
    if "(apiconnectionerror)" in lowered:
        return True
    if "apitimeout" in lowered and settings.gemini_fusion_model.lower() not in (existing.models_used or "").lower():
        return True
    return False


def run_uhid_reconciliation(
    his_db: Session,
    audit_db: Session,
    uhid: str,
    force_rerun: bool = False,
) -> AuditSessionResponse:
    """Trigger the 7-Stage pipeline for a Patient UHID (or return existing active/approved session)."""
    clean_uhid = uhid.strip().upper()
    with _get_uhid_lock(clean_uhid):
        audit_db.expire_all()
        existing = (
            audit_db.query(AuditSession)
            .filter(AuditSession.uhid == clean_uhid)
            .order_by(AuditSession.created_at.desc(), text("rowid DESC"))
            .first()
        )
        auto_retry_stale = (
            existing is not None
            and not force_rerun
            and _should_auto_retry_stale_fallback(existing, get_settings())
        )
        if existing is not None and not force_rerun and not auto_retry_stale:
            items = (
                audit_db.query(StagedLineItem)
                .filter(StagedLineItem.session_id == existing.session_id)
                .order_by(StagedLineItem.id.asc())
                .all()
            )
            return serialize_session(existing, items)

        if existing is not None and existing.bill_state == BillingState.FINAL_APPROVED.value and force_rerun:
            raise HTTPException(
                status_code=409,
                detail=f"Discharge bill for {clean_uhid} is FINAL_APPROVED and locked; cannot overwrite with re-audit.",
            )

        prior_decisions: dict[str, StagedLineItem] = {}
        if existing is not None and (force_rerun or auto_retry_stale):
            prior_decisions = _collect_prior_decisions(audit_db, clean_uhid, existing)

        t1 = time.perf_counter()
        raw_bundle = _fetch_his_bundle(his_db, clean_uhid)
        stage1_ms = (time.perf_counter() - t1) * 1000
        return _execute_stages_2_to_6(
            audit_db,
            raw_bundle,
            "HIS_UHID",
            stage1_ms,
            prior_decisions=prior_decisions,
        )


def run_upload_reconciliation(
    audit_db: Session,
    file_bytes: bytes,
    filename: str,
    payer_category: str,
    clinical_notes_text: str = "",
    room_entitlement_cap_per_day: float = 4500.0,
) -> AuditSessionResponse:
    """Parse an uploaded CSV/Excel/PDF ledger and execute Stages 2–6."""
    t1 = time.perf_counter()
    try:
        raw_bundle, source_type = parse_uploaded_bill(
            file_bytes=file_bytes,
            filename=filename,
            payer_category=payer_category,
            clinical_notes_text=clinical_notes_text,
            room_entitlement_cap_per_day=room_entitlement_cap_per_day,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    stage1_ms = (time.perf_counter() - t1) * 1000
    return _execute_stages_2_to_6(audit_db, raw_bundle, source_type, stage1_ms)
