# ClearBill AI — Architectural Rules & Security Invariants

ClearBill AI is an autonomous, UHID-triggered hospital discharge billing reconciliation agent built with FastAPI, SQLite, Google Gen AI SDK (`google-genai >= 3.2.0` Interactions API), and a clinical reviewer dashboard.

## 1. Repository & Pipeline Architecture

7-Stage UHID-Triggered Reconciliation Pipeline:
1. **Stage 1 — UHID Trigger & Read-Only HIS Fetch (or CSV/Excel/PDF Upload):** Retrieve patient demographics, admission record, itemized `billing_ledger`, `doctor_orders`, `clinical_notes`, `nursing_logs`, and `lab_reports` from `database/mock_his.db` (or parse uploaded bill documents).
2. **Stage 2 — Local PHI De-Identification:** `backend/phi_redactor.py` runs locally on-premise before any text is sent to an external LLM, replacing names, phones, emails, residential addresses, policy IDs, and government IDs with `[PATIENT_NAME]`, `[PHONE_REDACTED]`, `[EMAIL_REDACTED]`, `[ADDRESS_REDACTED]`, `[POLICY_ID_REDACTED]`, and `[GOV_ID_REDACTED]`.
3. **Stage 3 — Fast Ingestion & Normalization (`gemini-3.5-flash-lite`):** Normalize raw billing items, shifts, canonical service names, and clinical chronology into a structured `NormalizedClinicalLedger`.
4. **Stage 4 — Dynamic Payer Skill Loading:** `backend/skill_loader.py` resolves the patient's payer category (`ECHS`, `CGHS`, `TPA`, `PM-JAY`, or `Self-Pay`) and dynamically injects `.agents/skills/<payer>-audit/SKILL.md` into the reasoning system prompt.
5. **Stage 5 — Clinical + Billing Context Fusion (`gemini-3.8-flash` + Pydantic Structured Outputs):** Cross-check every billed item against clinical orders, progress notes, nursing shift logs, lab/radiology reports, and payer package rules into `VERIFIED_CLEAN`, `FLAGGED_DUPLICATE`, `FLAGGED_UNBUNDLED`, `FLAGGED_NO_MEDICAL_NECESSITY`, or `FLAGGED_TARIFF_CAP_EXCEEDED`.
6. **Stage 6 — Staging Isolation & Plain-English Remarks:** Route `VERIFIED_CLEAN` charges to the Ready-to-Submit Bill and isolate `FLAGGED_*` items into the Flagged Staging Bucket with exact ₹ savings, clinical evidence citations, and plain-English remarks.
7. **Stage 7 — Governed Human-in-the-Loop State Transitions & Immutable Audit Trail:** Authenticated hospital staff modify line-item states and finalize discharge bills subject to RBAC override limits, mandatory clinical justification remarks, and append-only logging in `database/clearbill_audit.db`.

---

## 2. Hard Security Invariants (Non-Negotiable)

1. **Strict Read-Only HIS Access:**
   - `database/mock_his.db` MUST ONLY be mounted by the application backend using SQLite URI `mode=ro` together with `PRAGMA query_only = ON`.
   - No backend API route, pipeline stage, or AI agent may ever `INSERT`, `UPDATE`, `DELETE`, or alter schema on `mock_his.db`.
2. **Mandatory Local PHI De-Identification & Zero Retention:**
   - Raw patient identifiers (`full_name`, `phone`, `email`, `address`, `gov_id_number`, `policy_or_card_no`) MUST NEVER be transmitted to Gemini API prompts.
   - All `google-genai` `client.interactions.create(...)` calls MUST set `store=False` so no hospital billing or clinical context is retained server-side.
   - Raw HIS payloads are retained solely in local hospital storage for authorized reviewer Privacy Shield diff inspection.
3. **Governed Human-in-the-Loop Overrides & RBAC Enforcement:**
   - Every line-item state transition (`CONFIRM_REMOVAL`, `RESTORE_WITH_OVERRIDE`, `REFLAG_ITEM`) and bill finalization (`FINAL_APPROVED`) MUST authenticate an active `employee_uid` from HIS `employees` and require a non-empty `justification_remark` of at least 10 characters.
   - Role override limits MUST be enforced server-side:
     - `TRAINEE_CLERK` (`EMP-DEL-999`): Read-only (`₹0` override limit; cannot confirm removals, override items, or finalize bills).
     - `BILLING_EXECUTIVE` (`EMP-DEL-101`): Up to `₹15,000` single-item override exposure.
     - `TPA_COORDINATOR` (`EMP-DEL-102`): Up to `₹25,000` single-item override exposure.
     - `SENIOR_AUDITOR` (`EMP-DEL-201`) & `MEDICAL_SUPERINTENDENT` (`EMP-DEL-301`): Unlimited override and final sign-off authority.
   - Once an audit session transitions to `FINAL_APPROVED`, all line-item state modifications are permanently locked.
4. **Immutable Append-Only Audit Log:**
   - Every human billing decision MUST append a record to `audit_logs` in `database/clearbill_audit.db`.
   - `audit_logs` is protected at the SQLite engine level by `BEFORE UPDATE` and `BEFORE DELETE` triggers (`IMMUTABLE_AUDIT_LOG`) that abort any mutation or deletion attempt.
5. **Dynamic Payer Skill Injection:**
   - Payer audit logic MUST load dynamically from `.agents/skills/*/SKILL.md` based on the admission's `payer_category` (`ECHS`, `CGHS`, `TPA`, `PM-JAY`) rather than hardcoding payer policies into application prompts.
6. **Zero-False-Positive Clinical Precision:**
   - Medically necessary repeat diagnostics or procedures (e.g., Q12H serial CBC/Platelet counts in dengue or repeat urgent NCCT Head following acute GCS deterioration) that are backed by explicit doctor orders, clinical progress notes, and separate lab/radiology reports MUST be classified `VERIFIED_CLEAN`.
