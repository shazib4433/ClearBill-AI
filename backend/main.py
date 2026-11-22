"""FastAPI application entrypoint for ClearBill AI."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from backend.bill_generator import build_consolidated_bill_pdf, build_printable_bill_html
from backend.config import get_settings
from backend.db import get_audit_db, get_his_db, init_audit_db
from backend.file_parser import SAMPLE_CSV_CONTENT
from backend.gemini_pipeline import test_gemini_connection
from backend.governance import apply_line_item_transition, finalize_discharge_bill, verify_employee
from backend.phi_redactor import mask_display_name
from backend.reconciler import run_uhid_reconciliation, run_upload_reconciliation, serialize_session
from backend.schemas import (
    AuditLogResponse,
    AuditSessionResponse,
    BillingState,
    EmployeeResponse,
    FinalizeRequest,
    HealthResponse,
    PatientListItem,
    SkillInfoResponse,
    TransitionRequest,
)
from backend.skill_loader import list_all_skills
from database.audit_schema import AuditLog, AuditSession, StagedLineItem
from database.his_schema import Admission, BillingLedger, Employee, Patient

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_audit_db()
    yield


app = FastAPI(
    title="ClearBill AI — Autonomous Hospital Discharge Billing Reconciliation Agent",
    version="1.0.0",
    lifespan=lifespan,
)

if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


def _to_employee_response(emp: Employee) -> EmployeeResponse:
    if emp.max_override_limit is None:
        limit_disp = "Unlimited Authority"
    elif emp.max_override_limit <= 0:
        limit_disp = "₹0 (Read-Only)"
    else:
        limit_disp = f"₹{emp.max_override_limit:,.0f}"
    return EmployeeResponse(
        employee_uid=emp.employee_uid,
        full_name=emp.full_name,
        role=emp.role,
        department=emp.department,
        max_override_limit=emp.max_override_limit,
        override_limit_display=limit_disp,
        can_finalize_bill=emp.can_finalize_bill,
        is_active=emp.is_active,
    )


@app.get("/", include_in_schema=False)
def serve_dashboard() -> FileResponse:
    index_path = FRONTEND_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Frontend dashboard not found.")
    return FileResponse(index_path)


@app.get("/api/health", response_model=HealthResponse)
def health_check() -> HealthResponse:
    settings = get_settings()
    skills = list_all_skills()
    return HealthResponse(
        status="ok",
        engine_mode="GEMINI_LIVE" if settings.has_live_gemini_key else "LOCAL_RULE_ENGINE",
        auth_mode=settings.auth_mode,
        ingest_model=settings.gemini_ingest_model,
        fusion_model=settings.gemini_fusion_model,
        his_db_readonly=True,
        his_db_exists=settings.his_db_path.exists(),
        audit_db_exists=settings.audit_db_path.exists(),
        skills_loaded=len(skills),
    )


@app.get("/api/gemini/test")
def gemini_diagnostic() -> dict:
    return test_gemini_connection()


@app.get("/api/employees", response_model=list[EmployeeResponse])
def list_employees(his_db: Session = Depends(get_his_db)) -> list[EmployeeResponse]:
    rows = his_db.query(Employee).order_by(Employee.employee_uid.asc()).all()
    return [_to_employee_response(r) for r in rows]


@app.get("/api/employees/{employee_uid}", response_model=EmployeeResponse)
def get_employee(employee_uid: str, his_db: Session = Depends(get_his_db)) -> EmployeeResponse:
    emp = verify_employee(his_db, employee_uid)
    return _to_employee_response(emp)


@app.get("/api/skills", response_model=list[SkillInfoResponse])
def get_skills() -> list[SkillInfoResponse]:
    return [
        SkillInfoResponse(
            slug=s.slug,
            name=s.name,
            description=s.description,
            path=s.path,
            content=s.content,
        )
        for s in list_all_skills()
    ]


@app.get("/api/sample-bill.csv", response_class=PlainTextResponse)
def download_sample_csv() -> PlainTextResponse:
    return PlainTextResponse(
        content=SAMPLE_CSV_CONTENT,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="clearbill_sample_echs_ptca.csv"'},
    )


@app.get("/api/patients", response_model=list[PatientListItem])
def list_patients(
    his_db: Session = Depends(get_his_db),
    audit_db: Session = Depends(get_audit_db),
) -> list[PatientListItem]:
    patients = his_db.query(Patient).order_by(Patient.uhid.asc()).all()
    admissions = {a.uhid: a for a in his_db.query(Admission).all()}
    totals_q = (
        his_db.query(BillingLedger.uhid, func.sum(BillingLedger.total_amount))
        .group_by(BillingLedger.uhid)
        .all()
    )
    gross_map = {uhid: float(total or 0.0) for uhid, total in totals_q}

    session_rows = (
        audit_db.query(
            AuditSession.uhid,
            AuditSession.session_id,
            AuditSession.bill_state,
            AuditSession.potential_savings_amount,
        )
        .order_by(AuditSession.created_at.desc(), text("rowid DESC"))
        .all()
    )
    latest_sessions: dict[str, tuple[str, str, float]] = {}
    for row_uhid, row_sid, row_state, row_savings in session_rows:
        if row_uhid not in latest_sessions:
            latest_sessions[row_uhid] = (row_sid, row_state, float(row_savings or 0.0))

    flagged_counts: dict[str, int] = {}
    if latest_sessions:
        latest_sids = [v[0] for v in latest_sessions.values()]
        counts_q = (
            audit_db.query(StagedLineItem.session_id, func.count(StagedLineItem.id))
            .filter(
                StagedLineItem.session_id.in_(latest_sids),
                StagedLineItem.bucket == "STAGING",
            )
            .group_by(StagedLineItem.session_id)
            .all()
        )
        flagged_counts = {sid: int(cnt) for sid, cnt in counts_q}

    result: list[PatientListItem] = []
    for p in patients:
        adm = admissions.get(p.uhid)
        ses_info = latest_sessions.get(p.uhid)
        sid = ses_info[0] if ses_info else None
        result.append(
            PatientListItem(
                uhid=p.uhid,
                masked_name=mask_display_name(p.full_name),
                age=p.age,
                gender=p.gender,
                payer_category=p.payer_category,
                payer_name=p.payer_name,
                department=adm.department if adm else "General",
                primary_diagnosis=adm.primary_diagnosis if adm else "",
                admission_date=adm.admission_date if adm else "",
                discharge_date=adm.discharge_date if adm else "",
                gross_billed_amount=round(gross_map.get(p.uhid, 0.0), 2),
                latest_session_id=sid,
                bill_state=ses_info[1] if ses_info else "PENDING_AUDIT",
                flagged_count=flagged_counts.get(sid, 0) if sid else 0,
                potential_savings=ses_info[2] if ses_info else 0.0,
            )
        )
    return result


@app.post("/api/reconcile/uhid/{uhid}", response_model=AuditSessionResponse)
def reconcile_uhid(
    uhid: str,
    force_rerun: bool = Query(default=False),
    his_db: Session = Depends(get_his_db),
    audit_db: Session = Depends(get_audit_db),
) -> AuditSessionResponse:
    return run_uhid_reconciliation(his_db, audit_db, uhid, force_rerun=force_rerun)


@app.post("/api/reconcile/upload", response_model=AuditSessionResponse)
async def reconcile_upload(
    file: UploadFile = File(...),
    payer_category: str = Form("ECHS"),
    clinical_notes_text: str = Form(""),
    room_entitlement_cap_per_day: float = Form(4500.0),
    audit_db: Session = Depends(get_audit_db),
) -> AuditSessionResponse:
    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=422, detail="Uploaded file is empty.")
    return run_upload_reconciliation(
        audit_db=audit_db,
        file_bytes=file_bytes,
        filename=file.filename or "uploaded_bill.csv",
        payer_category=payer_category,
        clinical_notes_text=clinical_notes_text,
        room_entitlement_cap_per_day=room_entitlement_cap_per_day,
    )


@app.get("/api/sessions/{session_id}", response_model=AuditSessionResponse)
def get_session(session_id: str, audit_db: Session = Depends(get_audit_db)) -> AuditSessionResponse:
    session = audit_db.query(AuditSession).filter(AuditSession.session_id == session_id).first()
    if session is None:
        raise HTTPException(status_code=404, detail=f"Audit session '{session_id}' not found.")
    items = (
        audit_db.query(StagedLineItem)
        .filter(StagedLineItem.session_id == session_id)
        .order_by(StagedLineItem.id.asc())
        .all()
    )
    return serialize_session(session, items)


@app.post("/api/sessions/{session_id}/items/{item_id}/transition", response_model=AuditSessionResponse)
def transition_item(
    session_id: str,
    item_id: str,
    req: TransitionRequest,
    his_db: Session = Depends(get_his_db),
    audit_db: Session = Depends(get_audit_db),
) -> AuditSessionResponse:
    session, items, _log = apply_line_item_transition(his_db, audit_db, session_id, item_id, req)
    return serialize_session(session, items)


@app.post("/api/sessions/{session_id}/finalize", response_model=AuditSessionResponse)
def finalize_session(
    session_id: str,
    req: FinalizeRequest,
    his_db: Session = Depends(get_his_db),
    audit_db: Session = Depends(get_audit_db),
) -> AuditSessionResponse:
    session, items, _log = finalize_discharge_bill(his_db, audit_db, session_id, req)
    return serialize_session(session, items)


def _get_finalized_session_and_items(
    session_id: str, audit_db: Session
) -> tuple[AuditSession, list[StagedLineItem]]:
    session = audit_db.query(AuditSession).filter(AuditSession.session_id == session_id).first()
    if session is None:
        raise HTTPException(status_code=404, detail=f"Audit session '{session_id}' not found.")
    if session.bill_state != BillingState.FINAL_APPROVED.value:
        raise HTTPException(
            status_code=409,
            detail="Consolidated final bill PDF is only available after the discharge bill is FINAL_APPROVED.",
        )
    items = (
        audit_db.query(StagedLineItem)
        .filter(StagedLineItem.session_id == session_id)
        .order_by(StagedLineItem.id.asc())
        .all()
    )
    return session, items


@app.get("/api/sessions/{session_id}/final-bill.pdf")
def download_final_bill_pdf(
    session_id: str,
    audit_db: Session = Depends(get_audit_db),
) -> Response:
    session, items = _get_finalized_session_and_items(session_id, audit_db)
    pdf_bytes = build_consolidated_bill_pdf(session, items)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="ClearBill_Final_{session.uhid}.pdf"'
        },
    )


@app.get("/api/sessions/{session_id}/final-bill/print", response_class=HTMLResponse)
def print_final_bill_html(
    session_id: str,
    audit_db: Session = Depends(get_audit_db),
) -> HTMLResponse:
    session, items = _get_finalized_session_and_items(session_id, audit_db)
    return HTMLResponse(content=build_printable_bill_html(session, items))


@app.get("/api/audit-logs", response_model=list[AuditLogResponse])
def list_audit_logs(
    uhid: str | None = Query(default=None),
    session_id: str | None = Query(default=None),
    employee_uid: str | None = Query(default=None),
    action_type: str | None = Query(default=None),
    audit_db: Session = Depends(get_audit_db),
) -> list[AuditLogResponse]:
    q = audit_db.query(AuditLog)
    if uhid:
        q = q.filter(AuditLog.uhid == uhid.strip().upper())
    if session_id:
        q = q.filter(AuditLog.session_id == session_id.strip().upper())
    if employee_uid:
        q = q.filter(AuditLog.employee_uid == employee_uid.strip().upper())
    if action_type:
        q = q.filter(AuditLog.action_type == action_type.strip().upper())
    rows = q.order_by(AuditLog.timestamp.desc(), text("rowid DESC")).limit(250).all()
    return [
        AuditLogResponse(
            audit_id=r.audit_id,
            timestamp=r.timestamp,
            uhid=r.uhid,
            session_id=r.session_id,
            item_id=r.item_id,
            service_code=r.service_code,
            service_name=r.service_name,
            action_type=r.action_type,
            previous_state=r.previous_state,
            new_state=r.new_state,
            financial_impact=r.financial_impact,
            employee_uid=r.employee_uid,
            employee_name=r.employee_name,
            employee_role=r.employee_role,
            justification_remark=r.justification_remark,
        )
        for r in rows
    ]
