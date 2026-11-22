---
name: cghs-tariff-audit
description: >-
  Payer tariff and clinical audit rulebook for Central Government Health Scheme (CGHS)
  inpatient claims. Enforces CGHS NABH package bundling (7-day post-op dressings and routine
  ward consumables), NPPA/CGHS orthopedic knee implant ceilings, and Radiology HIS auto-sync
  duplicate detection.
---

# CGHS Inpatient Tariff & Clinical Audit Rulebook

Use this rulebook whenever `payer_category == "CGHS"`. CGHS empanelled hospitals are strictly bound by CGHS NABH package rates and NPPA orthopedic/cardiac implant price ceilings.

## 1. CGHS Surgical Package Bundling (7-Day Post-Op Rule)
- A CGHS surgical package (e.g., Bilateral Total Knee Replacement `CGHS-ORTH-TKR-BIL`, joint arthroplasty, or major surgical procedures) covers:
  - Pre-operative surgical preparation and OT charges
  - Surgeon, assistant surgeon, and anesthesia fees
  - Standard surgical room stay, routine nursing, and **post-operative wound dressings, sterile gauze, cotton, crepe bandages, and routine ward consumables for up to 7 days post-surgery**
- **Audit Action**: Any separate line item for routine post-op dressings, sterile gauze kits, bandages, or routine surgical consumables billed during an inpatient surgical package stay MUST be classified as **`FLAGGED_UNBUNDLED`** with `potential_savings = total_amount`.

## 2. NPPA / CGHS Orthopedic Implant Ceiling Rules
- **Bilateral Total Knee Replacement (TKR) Implant Ceiling**:
  - Under CGHS / NPPA ceiling guidelines for primary Cobalt-Chromium cemented knee implants, the maximum reimbursable implant ceiling is **`₹55,000` per knee** (`₹1,10,000` total for Bilateral TKR), inclusive of standard tibial/femoral/patellar components and bone cement unless an explicit prior CGHS Special Sanction / CMO approval for a revision or specialized tumor prosthesis is documented in `clinical_notes` / `doctor_orders`.
- **Audit Action**: If an imported or standard knee implant for Bilateral TKR is billed above `₹1,10,000` without documented prior CGHS Special Sanction:
  - Classify as **`FLAGGED_TARIFF_CAP_EXCEEDED`**.
  - Set `allowed_cap_amount = 110000.0`.
  - Set `potential_savings = total_amount - 110000.0` (excess above CGHS/NPPA ceiling).
  - Note clearly in `plain_english_remark` that high-value implant overrides require `SENIOR_AUDITOR` or `MEDICAL_SUPERINTENDENT` approval if excess exceeds `₹25,000`.

## 3. High-Cost Radiology & HIS Auto-Sync Duplicate Rule
- High-cost neuroimaging and diagnostics (`MRI Brain`, `MR Angiography`, `CT Angiography`, `PET-CT`) must correspond 1:1 with:
  1. A documented `doctor_orders` request, AND
  2. A distinct verified `lab_reports` / radiology report.
- **Audit Action**: If two MRI/MRA or high-cost imaging charges appear on the same date (especially posted within minutes/hours under `HIS Auto-Sync` or a secondary radiology code) when only 1 clinical order and 1 radiology report exist, classify the duplicate auto-posted line item as **`FLAGGED_DUPLICATE`** with `potential_savings = total_amount`.
- **Exception**: Repeat imaging ordered after documented clinical deterioration (new neurological deficit, GCS drop, post-procedure complication) with a second `doctor_orders` entry and second radiology report is **`VERIFIED_CLEAN`**.
