"""Comprehensive end-to-end verification suite for ClearBill AI."""

from __future__ import annotations

import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from pypdf import PdfReader
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.db import get_his_engine, init_audit_db, reset_db_caches
from backend.main import app
from backend.skill_loader import list_all_skills
from database.seed_his_db import seed_databases


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Create isolated temporary HIS and Audit SQLite databases for deterministic tests."""
    his_path = tmp_path / "test_his.db"
    audit_path = tmp_path / "test_audit.db"

    monkeypatch.setenv("CLEARBILL_SKIP_DOTENV", "1")
    monkeypatch.setenv("HIS_DB_PATH", str(his_path))
    monkeypatch.setenv("AUDIT_DB_PATH", str(audit_path))
    monkeypatch.setenv("GEMINI_API_KEY", "")
    reset_db_caches()

    seed_databases(his_db_path=his_path, audit_db_path=audit_path, reset_audit=True)
    init_audit_db()

    with TestClient(app) as client:
        yield client, his_path, audit_path

    reset_db_caches()


def _make_minimal_pdf_bytes(lines: list[str]) -> bytes:
    """Construct a valid single-page PDF containing plain ASCII text lines parseable by pypdf."""
    text_cmds = ["BT", "/F1 11 Tf", "50 760 Td"]
    for idx, line in enumerate(lines):
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        if idx > 0:
            text_cmds.append("0 -18 Td")
        text_cmds.append(f"({escaped}) Tj")
    text_cmds.append("ET")
    stream_data = "\n".join(text_cmds).encode("latin-1")

    objects = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        (
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n"
        ),
        f"4 0 obj\n<< /Length {len(stream_data)} >>\nstream\n".encode("latin-1")
        + stream_data
        + b"\nendstream\nendobj\n",
        b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
    ]

    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(buf.tell())
        buf.write(obj)
    xref_pos = buf.tell()
    buf.write(f"xref\n0 {len(objects) + 1}\n".encode("latin-1"))
    buf.write(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        buf.write(f"{off:010d} 00000 n \n".encode("latin-1"))
    buf.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode(
            "latin-1"
        )
    )
    return buf.getvalue()


def test_readonly_his_enforcement(isolated_env):
    """Ensure backend HIS engine rejects any write attempt at the SQLite layer."""
    _client, _his_path, _audit_path = isolated_env
    engine = get_his_engine()
    with pytest.raises(OperationalError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE patients SET full_name = 'TAMPERED' WHERE uhid = 'UHID-2026-1001'"
                )
            )


def test_dynamic_payer_skills_loaded():
    """Verify all 4 Antigravity payer rulebook skills exist and parse cleanly."""
    skills = list_all_skills()
    slugs = {s.slug for s in skills}
    assert {
        "echs-tariff-audit",
        "cghs-tariff-audit",
        "tpa-cashless-audit",
        "pmjay-package-audit",
    }.issubset(slugs)


def test_dashboard_and_metadata_endpoints(isolated_env):
    """Verify frontend serving, health, employees, patients, and sample CSV endpoints."""
    client, _his, _audit = isolated_env

    home = client.get("/")
    assert home.status_code == 200
    assert "ClearBill AI" in home.text
    assert "cb-toolbar-grid" in home.text
    assert "audit-glass-overlay" in home.text
    assert "7-Stage Agentic Pipeline Trace" not in home.text
    assert "LOCAL PHI PRIVACY SHIELD ACTIVE" not in home.text

    css = client.get("/static/styles.css")
    assert css.status_code == 200
    assert "cb-glass-card" in css.text

    health = client.get("/api/health").json()
    assert health["status"] == "ok"
    assert health["his_db_readonly"] is True
    assert health["skills_loaded"] >= 4

    employees = client.get("/api/employees").json()
    assert len(employees) == 5
    assert {e["employee_uid"] for e in employees} == {
        "EMP-DEL-101",
        "EMP-DEL-102",
        "EMP-DEL-201",
        "EMP-DEL-301",
        "EMP-DEL-999",
    }

    patients = client.get("/api/patients").json()
    assert len(patients) == 10

    sample_csv = client.get("/api/sample-bill.csv")
    assert sample_csv.status_code == 200
    assert "PKG-PTCA-01" in sample_csv.text


@pytest.mark.parametrize(
    ("uhid", "expected_flagged_codes", "expected_savings", "expected_clean_codes"),
    [
        (
            "UHID-2026-1001",
            {"CARD-TROP-STAT", "EMG-ECG-12", "CON-PTCA-GW", "CON-CATH-SH"},
            22850.0,
            {"PKG-PTCA-01", "IMP-DES-NPPA", "LAB-TROP-I", "DIAG-ECG-12", "PHM-DAPT-01"},
        ),
        (
            "UHID-2026-1002",
            {"IMP-TKR-BIL-IMP", "CON-POSTOP-DRS"},
            59800.0,
            {"PKG-CGHS-TKR", "DIAG-ECHO-PRE", "PHYSIO-TKR-4D"},
        ),
        (
            "UHID-2026-1003",
            {"BED-DLX-01", "PHM-CEFT-DUP"},
            15300.0,
            {"SURG-LAP-CHOLE", "DIAG-USG-LFT", "PHM-CEFT-6D"},
        ),
        (
            "UHID-2026-1004",
            {"EQP-INFPUMP-DUP"},
            1500.0,
            {
                "BED-SP-3D",
                "LAB-CBC-PLT-1",
                "LAB-CBC-PLT-2",
                "LAB-CBC-PLT-3",
                "LAB-CBC-PLT-4",
                "LAB-CBC-PLT-5",
                "EQP-INFPUMP-3D",
            },
        ),
        (
            "UHID-2026-1005",
            {"LAB-PREOP-CBC", "LAB-BLD-GRP", "CON-OT-GLVSUT"},
            4200.0,
            {"PKG-PMJAY-APP"},
        ),
        (
            "UHID-2026-1006",
            {"BED-ICU-D34"},
            13000.0,
            {"BED-ICU-D12", "PROC-HD-3", "BED-GEN-D34", "LAB-KFT-2"},
        ),
        (
            "UHID-2026-1007",
            {"RAD-MRA-SYNC"},
            8500.0,
            {"BED-HDU-4D", "RAD-MRI-MRA-01", "PHM-STROKE-01", "DIAG-CAROTID"},
        ),
        (
            "UHID-2026-1008",
            {"NICU-PHOTO-2D"},
            16000.0,
            {"PKG-LSCS-SP", "PROC-ANESTH-OB", "CONS-PED-NB", "PHM-POSTLSCS"},
        ),
        (
            "UHID-2026-1009",
            set(),
            0.0,
            {"BED-GEN-4D", "LAB-ABG-3", "PROC-NEB-12", "EQP-BIPAP-2D", "DIAG-CXR-SPUT", "PHM-COPD-01"},
        ),
        (
            "UHID-2026-1010",
            {"BB-XMATCH-DUP", "CON-OT-DISP"},
            11600.0,
            {"BED-SICU-SP", "SURG-ORIF-FEM", "IMP-FEM-NAIL", "RAD-NCCT-01", "RAD-NCCT-02", "BB-PRBC-2U"},
        ),
    ],
)
def test_all_10_uhids_reconciliation_and_phi_redaction(
    isolated_env,
    uhid: str,
    expected_flagged_codes: set[str],
    expected_savings: float,
    expected_clean_codes: set[str],
):
    """Verify exact anomaly isolation, zero false positives, ₹ savings, and 100% PHI redaction."""
    client, _his, _audit = isolated_env
    res = client.post(f"/api/reconcile/uhid/{uhid}")
    assert res.status_code == 200, res.text
    ses = res.json()

    flagged_codes = {i["service_code"] for i in ses["line_items"] if i["bucket"] == "STAGING"}
    clean_codes = {i["service_code"] for i in ses["line_items"] if i["bucket"] == "CLEAN"}

    assert flagged_codes == expected_flagged_codes
    assert clean_codes == expected_clean_codes
    assert ses["potential_savings_amount"] == pytest.approx(expected_savings)

    # Zero PHI leakage check: raw sensitive fields must not appear in redacted_payload
    raw_patient = ses["raw_his_payload"]["patient"]
    redacted_str = json.dumps(ses["redacted_payload"])
    for sensitive in (
        raw_patient["phone"],
        raw_patient["email"],
        raw_patient["address"],
        raw_patient["gov_id_number"],
        raw_patient["policy_or_card_no"],
    ):
        assert sensitive not in redacted_str

    # Ensure full name (minus honorific) is completely absent from redacted payload
    assert raw_patient["full_name"] not in redacted_str
    assert ses["phi_redaction_stats"]["total_redactions"] >= 6


def test_file_uploads_csv_excel_pdf(isolated_env):
    """Verify CSV, Excel (.xlsx), and PDF bill upload pipelines."""
    client, _his, _audit = isolated_env

    # 1. CSV upload (sample ECHS PTCA bill)
    csv_bytes = client.get("/api/sample-bill.csv").content
    res_csv = client.post(
        "/api/reconcile/upload",
        files={"file": ("echs_ptca.csv", csv_bytes, "text/csv")},
        data={"payer_category": "ECHS", "clinical_notes_text": "PTCA ECHS-CARD-PTCA-01; no repeat Troponin-I."},
    )
    assert res_csv.status_code == 200, res_csv.text
    ses_csv = res_csv.json()
    assert ses_csv["source_type"] == "UPLOAD_CSV"
    assert ses_csv["potential_savings_amount"] == pytest.approx(15700.0)

    # 2. Excel (.xlsx) upload (PM-JAY Appendectomy with unbundled CBC & Gloves)
    wb = Workbook()
    ws = wb.active
    ws.append(["item_id", "service_date", "service_code", "service_name", "category", "quantity", "unit_price", "total_amount"])
    ws.append(["X-01", "2026-11-16", "PKG-PMJAY-APP", "PM-JAY Fixed Package Appendectomy", "PACKAGE", 1, 18000, 18000])
    ws.append(["X-02", "2026-11-16", "LAB-PREOP-CBC", "Pre-Op CBC", "DIAGNOSTICS", 1, 550, 550])
    ws.append(["X-03", "2026-11-16", "CON-OT-GLVSUT", "Sterile Surgical Gloves & Sutures", "CONSUMABLES", 1, 3200, 3200])
    xlsx_buf = io.BytesIO()
    wb.save(xlsx_buf)

    res_xlsx = client.post(
        "/api/reconcile/upload",
        files={"file": ("pmjay_bill.xlsx", xlsx_buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"payer_category": "PM-JAY"},
    )
    assert res_xlsx.status_code == 200, res_xlsx.text
    ses_xlsx = res_xlsx.json()
    assert ses_xlsx["source_type"] == "UPLOAD_EXCEL"
    assert ses_xlsx["potential_savings_amount"] == pytest.approx(3750.0)

    # 3. PDF upload
    pdf_bytes = _make_minimal_pdf_bytes(
        [
            "PKG-PTCA-01 | ECHS PTCA Coronary Package | 1 | 95000",
            "CON-PTCA-GW | Hydrophilic Coronary Guidewire Kit | 1 | 14500",
        ]
    )
    res_pdf = client.post(
        "/api/reconcile/upload",
        files={"file": ("hospital_bill.pdf", pdf_bytes, "application/pdf")},
        data={"payer_category": "ECHS"},
    )
    assert res_pdf.status_code == 200, res_pdf.text
    ses_pdf = res_pdf.json()
    assert ses_pdf["source_type"] == "UPLOAD_PDF"
    assert ses_pdf["potential_savings_amount"] == pytest.approx(14500.0)


def test_governance_rbac_finalization_and_immutability(isolated_env):
    """Test UID validation, RBAC override caps, state transitions, bill locking, and immutable audit triggers."""
    client, _his, audit_path = isolated_env

    # Reconcile UHID-2026-1002 (CGHS TKR: BL-1002-02 excess ₹55,000; BL-1002-04 ₹4,800)
    ses_1002 = client.post("/api/reconcile/uhid/UHID-2026-1002").json()
    sid_1002 = ses_1002["session_id"]

    # 1. Unknown Employee UID -> 401
    r_unknown = client.post(
        f"/api/sessions/{sid_1002}/items/BL-1002-04/transition",
        json={
            "action": "CONFIRM_REMOVAL",
            "employee_uid": "EMP-FAKE-000",
            "justification_remark": "Testing unauthorized employee rejection",
        },
    )
    assert r_unknown.status_code == 401

    # 2. Short remark (< 10 chars) -> 422
    r_short = client.post(
        f"/api/sessions/{sid_1002}/items/BL-1002-04/transition",
        json={
            "action": "CONFIRM_REMOVAL",
            "employee_uid": "EMP-DEL-101",
            "justification_remark": "Too short",
        },
    )
    assert r_short.status_code == 422

    # 3. Trainee Clerk (EMP-DEL-999) -> 403
    r_trainee = client.post(
        f"/api/sessions/{sid_1002}/items/BL-1002-04/transition",
        json={
            "action": "CONFIRM_REMOVAL",
            "employee_uid": "EMP-DEL-999",
            "justification_remark": "Trainee attempting unauthorized removal",
        },
    )
    assert r_trainee.status_code == 403

    # 4. EMP-DEL-101 (₹15k cap) CANNOT override ₹55,000 knee implant excess -> 403
    r_cap_101 = client.post(
        f"/api/sessions/{sid_1002}/items/BL-1002-02/transition",
        json={
            "action": "RESTORE_WITH_OVERRIDE",
            "employee_uid": "EMP-DEL-101",
            "justification_remark": "Attempting high-value override above 15k cap",
        },
    )
    assert r_cap_101.status_code == 403

    # 5. EMP-DEL-102 (₹25k cap) CANNOT override ₹55,000 -> 403, CAN override ₹16,000 on UHID-2026-1008
    r_cap_102 = client.post(
        f"/api/sessions/{sid_1002}/items/BL-1002-02/transition",
        json={
            "action": "RESTORE_WITH_OVERRIDE",
            "employee_uid": "EMP-DEL-102",
            "justification_remark": "Attempting 55k override above 25k TPA coordinator cap",
        },
    )
    assert r_cap_102.status_code == 403

    ses_1008 = client.post("/api/reconcile/uhid/UHID-2026-1008").json()
    sid_1008 = ses_1008["session_id"]
    assert (
        client.post(
            f"/api/sessions/{sid_1008}/items/BL-1008-05/transition",
            json={
                "action": "RESTORE_WITH_OVERRIDE",
                "employee_uid": "EMP-DEL-101",
                "justification_remark": "16k exceeds 15k billing executive cap",
            },
        ).status_code
        == 403
    )
    r_ok_102 = client.post(
        f"/api/sessions/{sid_1008}/items/BL-1008-05/transition",
        json={
            "action": "RESTORE_WITH_OVERRIDE",
            "employee_uid": "EMP-DEL-102",
            "justification_remark": "Late pediatric jaundice addendum verified with TPA",
        },
    )
    assert r_ok_102.status_code == 200

    # 6. EMP-DEL-201 (SENIOR_AUDITOR) CAN override ₹55,000 on UHID-2026-1002
    r_ok_201 = client.post(
        f"/api/sessions/{sid_1002}/items/BL-1002-02/transition",
        json={
            "action": "RESTORE_WITH_OVERRIDE",
            "employee_uid": "EMP-DEL-201",
            "justification_remark": "CGHS Special Sanction letter attached by Senior Auditor",
        },
    )
    assert r_ok_201.status_code == 200
    assert r_ok_201.json()["staging_count"] == 1

    # 7. Finalize fails (409) while BL-1002-04 remains in FLAGGED_STAGING
    r_fin_early = client.post(
        f"/api/sessions/{sid_1002}/finalize",
        json={
            "employee_uid": "EMP-DEL-301",
            "justification_remark": "Premature finalization before resolving staging items",
        },
    )
    assert r_fin_early.status_code == 409

    # 8. Confirm removal of BL-1002-04 by EMP-DEL-101 (allowed), then re-flag & confirm again
    r_rem = client.post(
        f"/api/sessions/{sid_1002}/items/BL-1002-04/transition",
        json={
            "action": "CONFIRM_REMOVAL",
            "employee_uid": "EMP-DEL-101",
            "justification_remark": "Removed post-op dressings bundled under CGHS package",
        },
    )
    assert r_rem.status_code == 200
    assert r_rem.json()["confirmed_savings_amount"] == pytest.approx(4800.0)
    assert r_rem.json()["staging_count"] == 0

    # Test Re-Flagging a clean item (BL-1002-03) and then restoring/finalizing
    r_reflag = client.post(
        f"/api/sessions/{sid_1002}/items/BL-1002-03/transition",
        json={
            "action": "REFLAG_ITEM",
            "employee_uid": "EMP-DEL-201",
            "justification_remark": "Re-flagging Echo for tariff code verification",
        },
    )
    assert r_reflag.status_code == 200
    assert r_reflag.json()["staging_count"] == 1

    r_restore_clean = client.post(
        f"/api/sessions/{sid_1002}/items/BL-1002-03/transition",
        json={
            "action": "RESTORE_WITH_OVERRIDE",
            "employee_uid": "EMP-DEL-201",
            "justification_remark": "Tariff code verified against CGHS Echo schedule",
        },
    )
    assert r_restore_clean.status_code == 200
    assert r_restore_clean.json()["staging_count"] == 0

    # Consolidated bill PDF/print rejected (409) before finalization
    assert client.get(f"/api/sessions/{sid_1002}/final-bill.pdf").status_code == 409
    assert client.get(f"/api/sessions/{sid_1002}/final-bill/print").status_code == 409

    # 9. Trainee clerk cannot finalize (403); Medical Superintendent can finalize (200)
    assert (
        client.post(
            f"/api/sessions/{sid_1002}/finalize",
            json={
                "employee_uid": "EMP-DEL-999",
                "justification_remark": "Trainee attempting final discharge lock",
            },
        ).status_code
        == 403
    )

    r_final = client.post(
        f"/api/sessions/{sid_1002}/finalize",
        json={
            "employee_uid": "EMP-DEL-301",
            "justification_remark": "All CGHS staging items resolved; approved for claim submission",
        },
    )
    assert r_final.status_code == 200
    final_ses = r_final.json()
    assert final_ses["bill_state"] == "FINAL_APPROVED"
    assert final_ses["readiness_score"] == 100.0

    # Consolidated Final Bill PDF & Printable HTML available after finalization
    pdf_resp = client.get(f"/api/sessions/{sid_1002}/final-bill.pdf")
    assert pdf_resp.status_code == 200
    assert pdf_resp.headers["content-type"].startswith("application/pdf")
    pdf_reader = PdfReader(io.BytesIO(pdf_resp.content))
    pdf_text = "\n".join(page.extract_text() or "" for page in pdf_reader.pages)
    assert "UHID-2026-1002" in pdf_text
    assert "FINAL APPROVED" in pdf_text
    assert "EMP-DEL-301" in pdf_text
    assert "PKG-CGHS-TKR" in pdf_text
    assert "380,600.00" in pdf_text

    print_resp = client.get(f"/api/sessions/{sid_1002}/final-bill/print")
    assert print_resp.status_code == 200
    assert "text/html" in print_resp.headers["content-type"]
    assert "UHID-2026-1002" in print_resp.text
    assert "window.print()" in print_resp.text
    assert f"/api/sessions/{sid_1002}/final-bill.pdf" in print_resp.text

    # Locked bill rejects further edits (409)
    assert (
        client.post(
            f"/api/sessions/{sid_1002}/items/BL-1002-03/transition",
            json={
                "action": "REFLAG_ITEM",
                "employee_uid": "EMP-DEL-301",
                "justification_remark": "Attempting edit after final sign-off lock",
            },
        ).status_code
        == 409
    )

    # 10. Audit Log API contains all transitions
    logs = client.get("/api/audit-logs?uhid=UHID-2026-1002").json()
    assert len(logs) == 5

    # 11. SQLite Triggers prohibit UPDATE and DELETE on audit_logs
    raw_conn = sqlite3.connect(str(audit_path))
    try:
        with pytest.raises((sqlite3.IntegrityError, sqlite3.OperationalError, IntegrityError)) as exc_upd:
            raw_conn.execute("UPDATE audit_logs SET justification_remark = 'TAMPERED'")
        assert "IMMUTABLE_AUDIT_LOG" in str(exc_upd.value)

        with pytest.raises((sqlite3.IntegrityError, sqlite3.OperationalError, IntegrityError)) as exc_del:
            raw_conn.execute("DELETE FROM audit_logs")
        assert "IMMUTABLE_AUDIT_LOG" in str(exc_del.value)
    finally:
        raw_conn.close()


def test_pdf_upload_extracts_descriptions_metadata_and_excludes_totals(isolated_env):
    """Regression test: multi-column PDF upload extracts full wrapped items & header metadata without duplicating totals."""
    client, _his, _audit = isolated_env

    # Case A: UHID-2026-1008 finalized with all 5 items (₹1,04,600.00)
    ses_1008 = client.post("/api/reconcile/uhid/UHID-2026-1008").json()
    sid_1008 = ses_1008["session_id"]
    assert (
        client.post(
            f"/api/sessions/{sid_1008}/items/BL-1008-05/transition",
            json={
                "action": "RESTORE_WITH_OVERRIDE",
                "employee_uid": "EMP-DEL-102",
                "justification_remark": "Pediatrician addendum confirms phototherapy monitoring",
            },
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/sessions/{sid_1008}/finalize",
            json={
                "employee_uid": "EMP-DEL-301",
                "justification_remark": "Approved maternity LSCS claim for consolidated PDF export",
            },
        ).status_code
        == 200
    )

    pdf_1008 = client.get(f"/api/sessions/{sid_1008}/final-bill.pdf")
    assert pdf_1008.status_code == 200

    reup_1008 = client.post(
        "/api/reconcile/upload",
        files={"file": ("ClearBill_Final_UHID-2026-1008.pdf", pdf_1008.content, "application/pdf")},
        data={"payer_category": "TPA"},
    )
    assert reup_1008.status_code == 200, reup_1008.text
    s_1008 = reup_1008.json()
    assert len(s_1008["line_items"]) == 5
    assert s_1008["total_billed_amount"] == pytest.approx(104600.0)
    assert s_1008["department"] == "Obstetrics & Gynecology"
    assert "Term Pregnancy" in s_1008["primary_diagnosis"]
    assert s_1008["patient_display_masked"] == "S*** K***"

    by_code_1008 = {i["service_code"]: i for i in s_1008["line_items"]}
    assert set(by_code_1008.keys()) == {
        "PKG-LSCS-SP",
        "PROC-ANESTH-OB",
        "CONS-PED-NB",
        "PHM-POSTLSCS",
        "NICU-PHOTO-2D",
    }
    assert "Spinal Anesthesia & Attending Obstetrician Team Charges" in by_code_1008["PROC-ANESTH-OB"]["service_name"]
    assert "NICU Level-II Phototherapy & Incubator Care (2 Days)" in by_code_1008["NICU-PHOTO-2D"]["service_name"]
    assert by_code_1008["NICU-PHOTO-2D"]["quantity"] == pytest.approx(2.0)

    # Case B: UHID-2026-1001 finalized WITH Section 2 Audited Deductions (Net Payable ₹1,38,333.00)
    ses_1001 = client.post("/api/reconcile/uhid/UHID-2026-1001").json()
    sid_1001 = ses_1001["session_id"]
    for flagged_id in ("BL-1001-05", "BL-1001-06", "BL-1001-07", "BL-1001-08"):
        assert (
            client.post(
                f"/api/sessions/{sid_1001}/items/{flagged_id}/transition",
                json={
                    "action": "CONFIRM_REMOVAL",
                    "employee_uid": "EMP-DEL-101",
                    "justification_remark": "Removed duplicate or bundled ECHS cath-lab charge",
                },
            ).status_code
            == 200
        )
    assert (
        client.post(
            f"/api/sessions/{sid_1001}/finalize",
            json={
                "employee_uid": "EMP-DEL-301",
                "justification_remark": "Final ECHS PTCA approval after removing bundled consumables",
            },
        ).status_code
        == 200
    )

    pdf_1001 = client.get(f"/api/sessions/{sid_1001}/final-bill.pdf")
    assert pdf_1001.status_code == 200

    reup_1001 = client.post(
        "/api/reconcile/upload",
        files={"file": ("ClearBill_Final_UHID-2026-1001.pdf", pdf_1001.content, "application/pdf")},
        data={"payer_category": "ECHS"},
    )
    assert reup_1001.status_code == 200, reup_1001.text
    s_1001 = reup_1001.json()
    assert len(s_1001["line_items"]) == 5
    assert s_1001["total_billed_amount"] == pytest.approx(138333.0)
    assert s_1001["department"] == "Cardiology"
    by_code_1001 = {i["service_code"]: i for i in s_1001["line_items"]}
    assert "ECHS PTCA & Single Vessel Stenting Package (ECHS-CARD-PTCA-01)" in by_code_1001["PKG-PTCA-01"]["service_name"]
    assert "Everolimus-Eluting" in by_code_1001["IMP-DES-NPPA"]["service_name"]

    # Case C: CSV upload with trailing Sub Total / Grand Total rows does NOT double-count totals
    csv_with_totals = (
        "service_code,service_name,category,quantity,unit_price,total_amount\n"
        "PKG-PMJAY-APP,PM-JAY Fixed Package Appendectomy,PACKAGE,1,18000,18000\n"
        "LAB-PREOP-CBC,Pre-Op CBC,DIAGNOSTICS,1,550,550\n"
        ",Sub Total,,, ,18550\n"
        ",Grand Total,,, ,18550\n"
    ).encode("utf-8")
    res_csv_tot = client.post(
        "/api/reconcile/upload",
        files={"file": ("bill_with_totals.csv", csv_with_totals, "text/csv")},
        data={"payer_category": "PM-JAY"},
    )
    assert res_csv_tot.status_code == 200, res_csv_tot.text
    assert len(res_csv_tot.json()["line_items"]) == 2
    assert res_csv_tot.json()["total_billed_amount"] == pytest.approx(18550.0)


def test_rerun_preserves_audited_decisions_consistent_with_audit_log(isolated_env):
    """Clicking Re-Run preserves governed line-item states already logged in audit_logs."""
    client, _his, _audit = isolated_env

    # 1. Initial audit of UHID-2026-1001 yields 4 flagged items
    ses1 = client.post("/api/reconcile/uhid/UHID-2026-1001").json()
    sid1 = ses1["session_id"]
    assert ses1["staging_count"] == 4

    # 2. Reviewer removes BL-1001-05 (₹1,200) and restores BL-1001-06 (₹350) with override
    assert (
        client.post(
            f"/api/sessions/{sid1}/items/BL-1001-05/transition",
            json={
                "action": "CONFIRM_REMOVAL",
                "employee_uid": "EMP-DEL-101",
                "justification_remark": "Confirmed duplicate night-shift Troponin-I removal",
            },
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/sessions/{sid1}/items/BL-1001-06/transition",
            json={
                "action": "RESTORE_WITH_OVERRIDE",
                "employee_uid": "EMP-DEL-101",
                "justification_remark": "Cardiology resident verified bedside repeat ECG",
            },
        ).status_code
        == 200
    )

    # 3. Click Re-Run (?force_rerun=true)
    res_rerun = client.post("/api/reconcile/uhid/UHID-2026-1001?force_rerun=true")
    assert res_rerun.status_code == 200, res_rerun.text
    ses2 = res_rerun.json()

    by_id = {i["item_id"]: i for i in ses2["line_items"]}
    # Already-audited items stay in REMOVED / CLEAN (NOT reverted into STAGING)
    assert by_id["BL-1001-05"]["current_state"] == "REMOVED_CONFIRMED"
    assert by_id["BL-1001-05"]["bucket"] == "REMOVED"
    assert by_id["BL-1001-05"]["last_modified_by_uid"] == "EMP-DEL-101"

    assert by_id["BL-1001-06"]["current_state"] == "RESTORED_WITH_OVERRIDE"
    assert by_id["BL-1001-06"]["bucket"] == "CLEAN"
    assert by_id["BL-1001-06"]["last_modified_by_uid"] == "EMP-DEL-101"

    # Only untouched flagged items remain in STAGING
    assert by_id["BL-1001-07"]["bucket"] == "STAGING"
    assert by_id["BL-1001-08"]["bucket"] == "STAGING"
    assert ses2["staging_count"] == 2
    assert ses2["removed_count"] == 1
    assert ses2["confirmed_savings_amount"] == pytest.approx(1200.0)

    # Immutable Audit Log and /api/patients agree with the re-run session
    logs = client.get("/api/audit-logs?uhid=UHID-2026-1001").json()
    assert len(logs) == 2

    patients = client.get("/api/patients").json()
    p1001 = next(p for p in patients if p["uhid"] == "UHID-2026-1001")
    assert p1001["latest_session_id"] == ses2["session_id"]
    assert p1001["flagged_count"] == 2


def test_config_ssl_cert_and_vertex_express_detection(monkeypatch: pytest.MonkeyPatch):
    """Verify macOS SSL cert bundle resolution, placeholder rejection, and AI Studio vs explicit Vertex routing."""
    import os
    from backend.config import get_settings

    monkeypatch.setenv("CLEARBILL_SKIP_DOTENV", "1")
    monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent/missing_cert.pem")

    # 1. Placeholder key -> DEMO_FALLBACK + SSL_CERT_FILE repaired via certifi
    monkeypatch.setenv("GEMINI_API_KEY", "your_gemini_api_key_here")
    s_placeholder = get_settings()
    assert s_placeholder.has_live_gemini_key is False
    assert s_placeholder.auth_mode == "DEMO_FALLBACK"
    assert os.path.exists(os.environ["SSL_CERT_FILE"])

    # 2. AQ.* and AIza* Google AI Studio keys default to AI_STUDIO (vertexai=False)
    monkeypatch.setenv("GEMINI_API_KEY", "AQ.DummyAIStudioKey0000000000000000000000000")
    s_aq_studio = get_settings()
    assert s_aq_studio.has_live_gemini_key is True
    assert s_aq_studio.use_vertex_ai is False
    assert s_aq_studio.auth_mode == "AI_STUDIO"

    monkeypatch.setenv("GEMINI_API_KEY", "AIzaDummyStudioKey000000000000000000000000")
    s_studio = get_settings()
    assert s_studio.has_live_gemini_key is True
    assert s_studio.use_vertex_ai is False
    assert s_studio.auth_mode == "AI_STUDIO"

    # 3. Explicit GOOGLE_GENAI_USE_VERTEXAI=true enables VERTEX_EXPRESS
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    s_vertex = get_settings()
    assert s_vertex.use_vertex_ai is True
    assert s_vertex.auth_mode == "VERTEX_EXPRESS"


def test_gemini_client_builder_routes_vertex_express(monkeypatch: pytest.MonkeyPatch):
    """Verify _build_genai_client selects generativelanguage by default and aiplatform when GOOGLE_GENAI_USE_VERTEXAI=true."""
    from backend.config import get_settings
    from backend.gemini_pipeline import _build_genai_client

    monkeypatch.setenv("CLEARBILL_SKIP_DOTENV", "1")
    monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)

    monkeypatch.setenv("GEMINI_API_KEY", "AQ.DummyAIStudioKey0000000000000000000000000")
    c_dev = _build_genai_client(get_settings())
    assert not c_dev._api_client.vertexai
    assert "generativelanguage.googleapis.com" in (c_dev._api_client._http_options.base_url or "")

    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    c_vtx = _build_genai_client(get_settings())
    assert c_vtx._api_client.vertexai is True
    assert "aiplatform.googleapis.com" in (c_vtx._api_client._http_options.base_url or "")


def test_gemini_503_failover_to_hydrogen(monkeypatch: pytest.MonkeyPatch):
    """Verify transient 503 service_unavailable on gemini-3.8-flash seamlessly fails over to gemini-4-hydrogen."""
    from types import SimpleNamespace
    from backend.config import get_settings
    from backend.gemini_pipeline import _invoke_structured
    from backend.schemas import ClinicalEventSummary, NormalizedClinicalLedger

    monkeypatch.setenv("CLEARBILL_SKIP_DOTENV", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "AQ.DummyAIStudioKey0000000000000000000000000")
    settings = get_settings()

    calls: list[str] = []
    expected = NormalizedClinicalLedger(
        uhid="UHID-2026-1001",
        payer_category="ECHS",
        normalized_items=[],
        clinical_summary=ClinicalEventSummary(),
    )

    class _FakeInteractions:
        def create(self, **kwargs):
            calls.append(kwargs["model"])
            if kwargs["model"] == "gemini-3.8-flash":
                raise RuntimeError("Error code: 503 - {'code': 'service_unavailable'}")
            return SimpleNamespace(output_text=expected.model_dump_json())

    fake_client = SimpleNamespace(interactions=_FakeInteractions())
    out = _invoke_structured(
        client=fake_client,
        settings=settings,
        model="gemini-3.8-flash",
        system_instruction="test",
        prompt="test",
        schema_cls=NormalizedClinicalLedger,
        thinking_level="medium",
    )
    assert calls == ["gemini-3.8-flash", "gemini-4-hydrogen"]
    assert out.uhid == "UHID-2026-1001"


def test_stale_fallback_session_auto_recovers_and_preserves_decisions(isolated_env, monkeypatch: pytest.MonkeyPatch):
    """Legacy pre-fix fallback sessions auto-heal on load/switch while preserving decisions and avoiding retry loops."""
    from backend.gemini_pipeline import local_fusion_audit, local_normalize_ledger

    client, _his, audit_path = isolated_env

    # 1. Initial UHID-2026-1001 session + human removal of BL-1001-05
    ses1 = client.post("/api/reconcile/uhid/UHID-2026-1001").json()
    sid1 = ses1["session_id"]
    assert (
        client.post(
            f"/api/sessions/{sid1}/items/BL-1001-05/transition",
            json={
                "action": "CONFIRM_REMOVAL",
                "employee_uid": "EMP-DEL-101",
                "justification_remark": "Confirmed duplicate night-shift Troponin-I removal",
            },
        ).status_code
        == 200
    )

    # Simulate legacy pre-fix 403 Agent Platform API notice in DB
    conn = sqlite3.connect(str(audit_path))
    try:
        conn.execute(
            "UPDATE audit_sessions SET fallback_notice = ? WHERE session_id = ?",
            (
                "Live Gemini Stage 5 unavailable (ClientError: 403 PERMISSION_DENIED. "
                "{'error': {'code': 403, 'message': 'Agent Platform API has not been used in project "
                "428963866989 before or it is disabled.'}}); using Local Clinical Rule Engine.",
                sid1,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    # 2. Configure live AI Studio key + simulate healthy Stage 3 & Stage 5 live responses
    monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "AQ.DummyAIStudioKey0000000000000000000000000")

    calls = {"s3": 0, "s5": 0}

    def _fake_s3(redacted_bundle):
        calls["s3"] += 1
        return local_normalize_ledger(redacted_bundle), "GEMINI_LIVE", None

    def _fake_s5(redacted_bundle, normalized, skill):
        calls["s5"] += 1
        return local_fusion_audit(redacted_bundle, normalized, skill), "GEMINI_LIVE", None

    monkeypatch.setattr("backend.reconciler.run_stage3_ingestion", _fake_s3)
    monkeypatch.setattr("backend.reconciler.run_stage5_fusion", _fake_s5)

    # Normal load (force_rerun=false) auto-recovers and preserves BL-1001-05 decision
    res2 = client.post("/api/reconcile/uhid/UHID-2026-1001")
    assert res2.status_code == 200, res2.text
    ses2 = res2.json()
    assert ses2["session_id"] != sid1
    assert ses2["engine_mode"] == "GEMINI_LIVE"
    assert ses2["fallback_notice"] is None
    assert calls == {"s3": 1, "s5": 1}

    by_id = {i["item_id"]: i for i in ses2["line_items"]}
    assert by_id["BL-1001-05"]["current_state"] == "REMOVED_CONFIRMED"
    assert by_id["BL-1001-05"]["bucket"] == "REMOVED"
    assert by_id["BL-1001-05"]["last_modified_by_uid"] == "EMP-DEL-101"

    # 3. Non-loop safety: modern (APIConnectionError: offline) fallback notice is NOT auto-retried
    monkeypatch.setenv("GEMINI_API_KEY", "")
    ses_1002 = client.post("/api/reconcile/uhid/UHID-2026-1002").json()
    sid_1002 = ses_1002["session_id"]
    ses_1009 = client.post("/api/reconcile/uhid/UHID-2026-1009").json()
    sid_1009 = ses_1009["session_id"]
    assert (
        client.post(
            f"/api/sessions/{sid_1009}/finalize",
            json={
                "employee_uid": "EMP-DEL-301",
                "justification_remark": "Final approval for clean dengue serial platelet admission",
            },
        ).status_code
        == 200
    )

    conn = sqlite3.connect(str(audit_path))
    try:
        conn.execute(
            "UPDATE audit_sessions SET engine_mode = 'LOCAL_RULE_ENGINE', fallback_notice = ? WHERE session_id = ?",
            (
                "Live Gemini Stage 5 unavailable (APIConnectionError: offline); using Local Clinical Rule Engine.",
                sid_1002,
            ),
        )
        conn.execute(
            "UPDATE audit_sessions SET engine_mode = 'LOCAL_RULE_ENGINE', fallback_notice = ? WHERE session_id = ?",
            (
                "Live Gemini Stage 5 unavailable (APIConnectionError); using Local Clinical Rule Engine.",
                sid_1009,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setenv("GEMINI_API_KEY", "AQ.DummyAIStudioKey0000000000000000000000000")
    calls["s3"] = 0
    calls["s5"] = 0

    res_1002_again = client.post("/api/reconcile/uhid/UHID-2026-1002")
    assert res_1002_again.status_code == 200
    assert res_1002_again.json()["session_id"] == sid_1002
    assert calls == {"s3": 0, "s5": 0}

    # 4. Locked FINAL_APPROVED session with legacy notice is never auto-retried
    res_1009_again = client.post("/api/reconcile/uhid/UHID-2026-1009")
    assert res_1009_again.status_code == 200
    assert res_1009_again.json()["session_id"] == sid_1009
    assert res_1009_again.json()["bill_state"] == "FINAL_APPROVED"
    assert calls == {"s3": 0, "s5": 0}


def test_gemini_timeout_config_and_timeout_failover(monkeypatch: pytest.MonkeyPatch):
    """Verify _build_genai_client configures fail-fast timeout/retry and _invoke_structured fails over across 3 models."""
    from types import SimpleNamespace
    from backend.config import get_settings
    from backend.gemini_pipeline import _build_genai_client, _invoke_structured
    from backend.schemas import ClinicalEventSummary, NormalizedClinicalLedger

    monkeypatch.setenv("CLEARBILL_SKIP_DOTENV", "1")
    monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "AQ.DummyAIStudioKey0000000000000000000000000")

    client = _build_genai_client(get_settings())
    http_opts = client._api_client._http_options
    assert http_opts.timeout == 45_000
    assert http_opts.retry_options.attempts == 1

    calls: list[str] = []
    expected = NormalizedClinicalLedger(
        uhid="UHID-2026-1005",
        payer_category="PM-JAY",
        normalized_items=[],
        clinical_summary=ClinicalEventSummary(),
    )

    class _FakeInteractions:
        def create(self, **kwargs):
            calls.append(kwargs["model"])
            if kwargs["model"] in ("gemini-3.8-flash", "gemini-4-hydrogen"):
                raise TimeoutError(f"ReadTimeout: timed out waiting for {kwargs['model']}")
            return SimpleNamespace(output_text=expected.model_dump_json())

    out = _invoke_structured(
        client=SimpleNamespace(interactions=_FakeInteractions()),
        settings=get_settings(),
        model="gemini-3.8-flash",
        system_instruction="test",
        prompt="test",
        schema_cls=NormalizedClinicalLedger,
        thinking_level="low",
    )
    assert calls == ["gemini-3.8-flash", "gemini-4-hydrogen", "gemini-3.5-flash-lite"]
    assert out.uhid == "UHID-2026-1005"


def test_concurrent_uhid_reconcile_and_audit_log_decision_recovery(isolated_env, monkeypatch: pytest.MonkeyPatch):
    """Verify per-UHID locking prevents duplicate concurrent runs and Re-Run heals zombie session overwrites from AuditLog."""
    import concurrent.futures
    import time
    from backend.gemini_pipeline import local_fusion_audit

    client, _his, audit_path = isolated_env

    # 1. Two concurrent initial requests on UHID-2026-1005 should execute Stage 5 only once
    s5_calls = {"count": 0}

    def _slow_s5(redacted_bundle, normalized, skill):
        s5_calls["count"] += 1
        time.sleep(0.15)
        return local_fusion_audit(redacted_bundle, normalized, skill), "LOCAL_RULE_ENGINE", None

    monkeypatch.setattr("backend.reconciler.run_stage5_fusion", _slow_s5)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(lambda: client.post("/api/reconcile/uhid/UHID-2026-1005").json())
        f2 = pool.submit(lambda: client.post("/api/reconcile/uhid/UHID-2026-1005").json())
        r1 = f1.result()
        r2 = f2.result()

    assert s5_calls["count"] == 1
    assert r1["session_id"] == r2["session_id"]
    sid1 = r1["session_id"]

    # 2. Reviewer confirms removal of BL-1005-02 (logged in immutable audit_logs)
    assert (
        client.post(
            f"/api/sessions/{sid1}/items/BL-1005-02/transition",
            json={
                "action": "CONFIRM_REMOVAL",
                "employee_uid": "EMP-DEL-101",
                "justification_remark": "Confirmed PM-JAY pre-op CBC bundled removal",
            },
        ).status_code
        == 200
    )

    # 3. Simulate an earlier zombie session overwriting latest audit_sessions with unmodified BL-1005-02
    conn = sqlite3.connect(str(audit_path))
    conn.row_factory = sqlite3.Row
    try:
        ses_row = dict(conn.execute("SELECT * FROM audit_sessions WHERE session_id = ?", (sid1,)).fetchone())
        ses_row["session_id"] = "SES-ZOMBIE-99"
        ses_row["created_at"] = "2099-01-01T00:00:00Z"
        ses_row["updated_at"] = "2099-01-01T00:00:00Z"
        cols = ", ".join(ses_row.keys())
        placeholders = ", ".join("?" for _ in ses_row)
        conn.execute(f"INSERT INTO audit_sessions ({cols}) VALUES ({placeholders})", tuple(ses_row.values()))

        item_rows = conn.execute("SELECT * FROM staged_line_items WHERE session_id = ?", (sid1,)).fetchall()
        for row in item_rows:
            item_dict = dict(row)
            item_dict.pop("id", None)
            item_dict["session_id"] = "SES-ZOMBIE-99"
            if item_dict["item_id"] == "BL-1005-02":
                item_dict["current_state"] = "FLAGGED_STAGING"
                item_dict["bucket"] = "STAGING"
                item_dict["last_modified_by_uid"] = None
                item_dict["last_modified_by_name"] = None
                item_dict["last_justification_remark"] = None
            icols = ", ".join(item_dict.keys())
            iplaceholders = ", ".join("?" for _ in item_dict)
            conn.execute(f"INSERT INTO staged_line_items ({icols}) VALUES ({iplaceholders})", tuple(item_dict.values()))
        conn.commit()
    finally:
        conn.close()

    # 4. Re-Run recovers BL-1005-02 from immutable AuditLog history
    res_rerun = client.post("/api/reconcile/uhid/UHID-2026-1005?force_rerun=true")
    assert res_rerun.status_code == 200, res_rerun.text
    ses_healed = res_rerun.json()
    by_id = {it["item_id"]: it for it in ses_healed["line_items"]}
    assert by_id["BL-1005-02"]["current_state"] == "REMOVED_CONFIRMED"
    assert by_id["BL-1005-02"]["bucket"] == "REMOVED"
    assert by_id["BL-1005-02"]["last_modified_by_uid"] == "EMP-DEL-101"
