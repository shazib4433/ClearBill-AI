# ClearBill AI

**ClearBill AI** is an autonomous, UHID-triggered hospital discharge billing reconciliation agent and full-stack clinical governance application built with **FastAPI**, **Google Gen AI SDK (`google-genai`)**, **SQLite**, and a **Clinical Reviewer Dashboard**.

---

## Architecture & 7-Stage Reconciliation Pipeline

1. **Stage 1 — UHID Trigger & Read-Only HIS Fetch (or CSV/Excel/PDF Upload):**
   - Queries `database/mock_his.db` strictly in read-only mode (`SQLite URI mode=ro` + `PRAGMA query_only = ON`) across 8 tables (`employees`, `patients`, `admissions`, `billing_ledger`, `doctor_orders`, `clinical_notes`, `nursing_logs`, `lab_reports`), or parses uploaded CSV/XLSX/PDF ledgers.
2. **Stage 2 — Local PHI De-Identification (`backend/phi_redactor.py`):**
   - Locally scrubs patient names, phone numbers, emails, residential addresses, policy numbers, and government IDs (`[PATIENT_NAME]`, `[PHONE_REDACTED]`, `[EMAIL_REDACTED]`, `[ADDRESS_REDACTED]`, `[GOV_ID_REDACTED]`, `[POLICY_ID_REDACTED]`) across all structured and clinical text fields before any cloud reasoning call.
3. **Stage 3 — Fast Ingestion & Normalization (`gemini-3.5-flash-lite`):**
   - Uses `google-genai` Interactions API with Pydantic Structured Outputs (`store=False`) to normalize aliased service codes, shifts, and clinical events.
4. **Stage 4 — Dynamic Payer Skill Loading (`backend/skill_loader.py`):**
   - Dynamically injects the matching payer rulebook from `.agents/skills/<payer>-audit/SKILL.md`:
     - `.agents/skills/echs-tariff-audit/SKILL.md`
     - `.agents/skills/cghs-tariff-audit/SKILL.md`
     - `.agents/skills/tpa-cashless-audit/SKILL.md`
     - `.agents/skills/pmjay-package-audit/SKILL.md`
5. **Stage 5 — Clinical + Billing Context Fusion (`gemini-3.8-flash`):**
   - Cross-checks every billing line item against doctor orders, clinical progress notes, nursing shift logs, lab/radiology reports, and payer rules into `VERIFIED_CLEAN`, `FLAGGED_DUPLICATE`, `FLAGGED_UNBUNDLED`, `FLAGGED_NO_MEDICAL_NECESSITY`, or `FLAGGED_TARIFF_CAP_EXCEEDED`.
   - *Hackathon-Safe Dual Mode:* Automatically switches to live Gemini when `GEMINI_API_KEY` is configured in `.env`, or transparently runs the deterministic **Demo Clinical Rule Engine** offline when no key is set.
6. **Stage 6 — Staging Isolation & Plain-English Remarks:**
   - Isolates flagged overbilling items into the Staging Bucket with exact `₹` savings, payer rules cited, and clinical evidence citations while keeping verified charges in the Ready-to-Submit Bill.
7. **Stage 7 — Governed Human-in-the-Loop Overrides & Immutable Audit Trail:**
   - Requires an active HIS **Employee UID**, RBAC override cap check, and a **Justification Remark** (`>= 10` chars).
   - Appends every mutation to `audit_logs` in `database/clearbill_audit.db`, protected by SQLite `BEFORE UPDATE` and `BEFORE DELETE` abort triggers.

---

## Quickstart Setup (3 Commands)

```bash
# 1. Create virtual environment & install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Seed Mock HIS (10 Inpatient UHIDs + 5 Staff UIDs) & Immutable Audit DB
python database/seed_his_db.py

# 3. Launch ClearBill AI Backend + Clinical Dashboard
cp .env.example .env   # Optional: add GEMINI_API_KEY for Live Gemini mode
uvicorn backend.main:app --reload
```

Open **http://127.0.0.1:8000** in your browser.

---

## Live Hackathon Demo Script

### Authorized Employee UIDs (Top Reviewer Bar)
| Employee UID | Reviewer Name | Role | Max Override Authority |
| :--- | :--- | :--- | :--- |
| `EMP-DEL-101` | Rohan Verma | `BILLING_EXECUTIVE` | `₹15,000` |
| `EMP-DEL-102` | Priya Nair | `TPA_COORDINATOR` | `₹25,000` |
| `EMP-DEL-201` | Dr. Arvind Kulkarni | `SENIOR_AUDITOR` | Unlimited |
| `EMP-DEL-301` | Dr. Meera Deshmukh | `MEDICAL_SUPERINTENDENT` | Unlimited |
| `EMP-DEL-999` | Karan Joshi | `TRAINEE_CLERK` | `₹0` (Read-Only Denied) |

### Key Demo Highlights Across the 10 Seeded UHIDs
- **`UHID-2026-1001` (ECHS PTCA):** Catches aliased Troponin-I (`CARD-TROP-STAT`) & ECG (`EMG-ECG-12`) shift duplicates + unbundled cath-lab guidewires/sheaths (`₹22,850` saved). Toggle **Privacy Shield** to show local PHI scrubbing before Gemini.
- **`UHID-2026-1002` (CGHS Bilateral TKR):** Flags imported knee implant exceeding CGHS/NPPA ceiling (`₹55,000` excess). Try overriding as `EMP-DEL-101` (`₹15,000` limit) to demonstrate RBAC rejection, then escalate to `EMP-DEL-201`.
- **`UHID-2026-1003` (TPA Star Health):** Flags unconsented Deluxe Room upgrade (`₹13,500` excess) with **TPA Proportionate Deduction Risk** warning + duplicate night-shift Ceftriaxone vials.
- **`UHID-2026-1004` (TPA HDFC ERGO Dengue):** Proves **zero false positives** — all 5 serial Q12H CBC/Platelets stay `VERIFIED_CLEAN`, isolating only the duplicate infusion pump rental (`₹1,500`).
- **`UHID-2026-1006` (ECHS ICU + Dialysis):** Flags 2 ghost ICU days billed after progress note & nursing logs confirmed General Ward transfer at 10:00 AM (`₹13,000`).
- **`UHID-2026-1009` (ECHS COPD):** 100% Clean Benchmark (`100%` Readiness Score, `0` flagged items).
- **`UHID-2026-1010` (TPA Polytrauma):** Nuanced clinical reasoning — urgent repeat NCCT Head (`GCS 15→11`) stays `VERIFIED_CLEAN`, while duplicate blood cross-match & IRDAI non-payable OT disposables are flagged (`₹11,600`).
- **Read-Only Clerk Test:** Select `EMP-DEL-999` and attempt any state modification or bill finalization to verify immediate `403` permission denial.
- **Immutable Audit Log Tab:** View append-only state transitions (`FLAGGED_STAGING → REMOVED_CONFIRMED` / `RESTORED_WITH_OVERRIDE` / `FINAL_APPROVED`).

---

## Running Automated Tests

```bash
.venv/bin/pytest tests/test_clearbill.py -v
```
