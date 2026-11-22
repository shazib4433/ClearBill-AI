"""Stage 7: Governed Human-in-the-Loop state transitions, RBAC enforcement & immutable audit logger."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from backend.schemas import BillingState, FinalizeRequest, LineItemAction, TransitionRequest
from database.audit_schema import AuditLog, AuditSession, StagedLineItem
from database.his_schema import Employee


def utc_now_iso() -> str:
    """Return clean UTC ISO-8601 timestamp."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def verify_employee(his_db: Session, employee_uid: str) -> Employee:
    """Authenticate an employee against the Read-Only HIS employees table."""
    uid = (employee_uid or "").strip().upper()
    employee = his_db.query(Employee).filter(Employee.employee_uid == uid).first()
    if employee is None:
        raise HTTPException(
            status_code=401,
            detail=(
                f"Unauthorized Employee UID '{employee_uid}'. Only active staff registered in the "
                "Hospital Information System (EMP-DEL-101, 102, 201, 301, 999) can authenticate."
            ),
        )
    if not employee.is_active:
        raise HTTPException(
            status_code=403,
            detail=f"Employee UID '{uid}' ({employee.full_name}) is inactive and cannot perform billing actions.",
        )
    return employee


def validate_justification(remark: str) -> str:
    """Require a non-empty clinical/audit justification remark (>= 10 characters)."""
    cleaned = (remark or "").strip()
    if len(cleaned) < 10:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Justification remark must be at least 10 characters (received {len(cleaned)}). "
                "Provide clear clinical or payer-tariff reasoning for the audit log."
            ),
        )
    return cleaned


def _format_limit(limit: float | None) -> str:
    if limit is None:
        return "Unlimited"
    if limit <= 0:
        return "₹0 (Read-Only)"
    return f"₹{limit:,.0f}"


def _recommended_escalation(exposure: float) -> str:
    if exposure > 25000.0:
        return "Escalate to SENIOR_AUDITOR (EMP-DEL-201) or MEDICAL_SUPERINTENDENT (EMP-DEL-301)."
    if exposure > 15000.0:
        return "Escalate to TPA_COORDINATOR (EMP-DEL-102, limit ₹25,000) or SENIOR_AUDITOR (EMP-DEL-201)."
    return "Requires BILLING_EXECUTIVE (EMP-DEL-101) or higher authority."


def recompute_session_metrics(session: AuditSession, items: list[StagedLineItem]) -> None:
    """Recompute all financial KPIs, readiness score, and bill state from current line items."""
    total_billed = round(sum(float(i.total_amount) for i in items), 2)
    clean_amt = 0.0
    staging_amt = 0.0
    potential_savings = 0.0
    confirmed_savings = 0.0

    staging_count = 0
    for item in items:
        state = item.current_state
        total_item = float(item.total_amount)
        pot_sav = float(item.potential_savings or 0.0)

        if state == BillingState.FLAGGED_STAGING.value:
            staging_count += 1
            staging_amt += total_item
            potential_savings += pot_sav
        elif state == BillingState.REMOVED_CONFIRMED.value:
            removed_amt = pot_sav if pot_sav > 0 else total_item
            confirmed_savings += removed_amt
            potential_savings += removed_amt
            if item.allowed_cap_amount is not None and item.allowed_cap_amount > 0:
                clean_amt += float(item.allowed_cap_amount)
        elif state in (
            BillingState.VERIFIED_CLEAN.value,
            BillingState.RESTORED_WITH_OVERRIDE.value,
            BillingState.FINAL_APPROVED.value,
        ):
            clean_amt += total_item

    clean_amt = round(clean_amt, 2)
    staging_amt = round(staging_amt, 2)
    potential_savings = round(potential_savings, 2)
    confirmed_savings = round(confirmed_savings, 2)
    net_payable = round(max(0.0, total_billed - confirmed_savings), 2)

    if session.bill_state != BillingState.FINAL_APPROVED.value:
        session.bill_state = (
            BillingState.FLAGGED_STAGING.value if staging_count > 0 else BillingState.VERIFIED_CLEAN.value
        )

    total_count = max(len(items), 1)
    resolved_count = total_count - staging_count
    if session.bill_state == BillingState.FINAL_APPROVED.value:
        readiness = 100.0
    else:
        readiness = round((resolved_count / total_count) * 100.0, 1)

    session.total_billed_amount = total_billed
    session.clean_verified_amount = clean_amt
    session.flagged_staging_amount = staging_amt
    session.potential_savings_amount = potential_savings
    session.confirmed_savings_amount = confirmed_savings
    session.net_payable_amount = net_payable
    session.readiness_score = readiness
    session.updated_at = utc_now_iso()


def apply_line_item_transition(
    his_db: Session,
    audit_db: Session,
    session_id: str,
    item_id: str,
    req: TransitionRequest,
) -> tuple[AuditSession, list[StagedLineItem], AuditLog]:
    """Authenticate reviewer, enforce RBAC & state machine, update item, and append immutable log."""
    employee = verify_employee(his_db, req.employee_uid)
    remark = validate_justification(req.justification_remark)

    session = audit_db.query(AuditSession).filter(AuditSession.session_id == session_id).first()
    if session is None:
        raise HTTPException(status_code=404, detail=f"Audit session '{session_id}' not found.")

    if session.bill_state == BillingState.FINAL_APPROVED.value:
        raise HTTPException(
            status_code=409,
            detail=f"Discharge bill for {session.uhid} is FINAL_APPROVED and permanently locked.",
        )

    items = (
        audit_db.query(StagedLineItem)
        .filter(StagedLineItem.session_id == session_id)
        .order_by(StagedLineItem.id.asc())
        .all()
    )
    target = next((it for it in items if it.item_id == item_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"Line item '{item_id}' not found in session '{session_id}'.")

    # Role permission gate: TRAINEE_CLERK (limit 0) is strictly Read-Only
    if employee.role == "TRAINEE_CLERK" or (employee.max_override_limit is not None and employee.max_override_limit <= 0):
        raise HTTPException(
            status_code=403,
            detail=(
                f"Permission denied for {employee.employee_uid} ({employee.full_name}, {employee.role}): "
                "Read-Only Observer accounts cannot modify billing states or override flagged items."
            ),
        )

    prev_state = target.current_state
    now = utc_now_iso()
    financial_impact = 0.0

    if req.action == LineItemAction.CONFIRM_REMOVAL:
        if prev_state not in (BillingState.FLAGGED_STAGING.value, BillingState.RESTORED_WITH_OVERRIDE.value):
            raise HTTPException(
                status_code=409,
                detail=f"Cannot confirm removal from state '{prev_state}'. Re-flag clean items first.",
            )
        new_state = BillingState.REMOVED_CONFIRMED.value
        target.bucket = "REMOVED"
        deduction = float(target.potential_savings if target.potential_savings > 0 else target.total_amount)
        if target.potential_savings <= 0:
            target.potential_savings = float(target.total_amount)
        target.payable_amount = (
            float(target.allowed_cap_amount) if target.allowed_cap_amount is not None else 0.0
        )
        financial_impact = -round(deduction, 2)

    elif req.action == LineItemAction.RESTORE_WITH_OVERRIDE:
        if prev_state not in (BillingState.FLAGGED_STAGING.value, BillingState.REMOVED_CONFIRMED.value):
            raise HTTPException(
                status_code=409,
                detail=f"Item '{item_id}' is in state '{prev_state}' and does not require restoration override.",
            )
        override_exposure = float(target.potential_savings if target.potential_savings > 0 else target.total_amount)
        if employee.max_override_limit is not None and override_exposure > employee.max_override_limit:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Override authority exceeded for {employee.employee_uid} ({employee.full_name}, {employee.role}). "
                    f"Max override limit is {_format_limit(employee.max_override_limit)}, but disputed exposure is "
                    f"₹{override_exposure:,.0f}. {_recommended_escalation(override_exposure)}"
                ),
            )
        new_state = BillingState.RESTORED_WITH_OVERRIDE.value
        target.bucket = "CLEAN"
        target.payable_amount = float(target.total_amount)
        financial_impact = round(override_exposure, 2)

    elif req.action == LineItemAction.REFLAG_ITEM:
        if prev_state not in (BillingState.VERIFIED_CLEAN.value, BillingState.RESTORED_WITH_OVERRIDE.value):
            raise HTTPException(
                status_code=409,
                detail=f"Cannot re-flag item '{item_id}' from state '{prev_state}'.",
            )
        new_state = BillingState.FLAGGED_STAGING.value
        target.bucket = "STAGING"
        if target.potential_savings <= 0:
            target.potential_savings = float(target.total_amount)
        target.payable_amount = float(target.total_amount)
        financial_impact = round(float(target.potential_savings), 2)

    else:
        raise HTTPException(status_code=400, detail=f"Unsupported action: {req.action}")

    target.current_state = new_state
    target.last_modified_by_uid = employee.employee_uid
    target.last_modified_by_name = employee.full_name
    target.last_justification_remark = remark
    target.updated_at = now

    recompute_session_metrics(session, items)

    audit_entry = AuditLog(
        audit_id=f"AUD-{uuid.uuid4().hex[:10].upper()}",
        timestamp=now,
        uhid=session.uhid,
        session_id=session.session_id,
        item_id=target.item_id,
        service_code=target.service_code,
        service_name=target.service_name,
        action_type=req.action.value,
        previous_state=prev_state,
        new_state=new_state,
        financial_impact=financial_impact,
        employee_uid=employee.employee_uid,
        employee_name=employee.full_name,
        employee_role=employee.role,
        justification_remark=remark,
    )
    audit_db.add(audit_entry)
    audit_db.commit()
    audit_db.refresh(session)
    return session, items, audit_entry


def finalize_discharge_bill(
    his_db: Session,
    audit_db: Session,
    session_id: str,
    req: FinalizeRequest,
) -> tuple[AuditSession, list[StagedLineItem], AuditLog]:
    """Transition an audit session to FINAL_APPROVED once all flagged staging items are resolved."""
    employee = verify_employee(his_db, req.employee_uid)
    remark = validate_justification(req.justification_remark)

    if not employee.can_finalize_bill:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Permission denied: {employee.employee_uid} ({employee.full_name}, {employee.role}) "
                "does not have final discharge bill sign-off authority."
            ),
        )

    session = audit_db.query(AuditSession).filter(AuditSession.session_id == session_id).first()
    if session is None:
        raise HTTPException(status_code=404, detail=f"Audit session '{session_id}' not found.")

    if session.bill_state == BillingState.FINAL_APPROVED.value:
        raise HTTPException(
            status_code=409,
            detail=f"Discharge bill for {session.uhid} is already FINAL_APPROVED.",
        )

    items = (
        audit_db.query(StagedLineItem)
        .filter(StagedLineItem.session_id == session_id)
        .order_by(StagedLineItem.id.asc())
        .all()
    )
    unresolved = [
        it.item_id
        for it in items
        if it.current_state in (BillingState.FLAGGED_STAGING.value, BillingState.PENDING_AUDIT.value)
    ]
    if unresolved:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot finalize bill while {len(unresolved)} item(s) remain in FLAGGED_STAGING: "
                f"{', '.join(unresolved)}. Confirm removal or restore with override first."
            ),
        )

    now = utc_now_iso()
    prev_bill_state = session.bill_state

    for it in items:
        if it.current_state in (BillingState.VERIFIED_CLEAN.value, BillingState.RESTORED_WITH_OVERRIDE.value):
            it.current_state = BillingState.FINAL_APPROVED.value
            it.updated_at = now

    session.bill_state = BillingState.FINAL_APPROVED.value
    session.finalized_by_uid = employee.employee_uid
    session.finalized_by_name = employee.full_name
    session.finalized_by_role = employee.role
    session.finalized_at = now
    session.finalization_remark = remark
    recompute_session_metrics(session, items)

    audit_entry = AuditLog(
        audit_id=f"AUD-{uuid.uuid4().hex[:10].upper()}",
        timestamp=now,
        uhid=session.uhid,
        session_id=session.session_id,
        item_id="BILL_HEADER",
        service_code="DISCHARGE_FINAL",
        service_name=f"Final Discharge Bill Sign-Off ({session.uhid})",
        action_type="FINALIZE_BILL",
        previous_state=prev_bill_state,
        new_state=BillingState.FINAL_APPROVED.value,
        financial_impact=float(session.net_payable_amount),
        employee_uid=employee.employee_uid,
        employee_name=employee.full_name,
        employee_role=employee.role,
        justification_remark=remark,
    )
    audit_db.add(audit_entry)
    audit_db.commit()
    audit_db.refresh(session)
    return session, items, audit_entry
