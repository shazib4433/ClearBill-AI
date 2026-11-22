---
name: echs-tariff-audit
description: >-
  Payer tariff and clinical audit rulebook for Ex-Servicemen Contributory Health Scheme (ECHS)
  inpatient discharge bills. Governs ECHS/CGHS-aligned surgical and interventional cardiology
  packages, cath-lab consumable bundling, NPPA stent caps, nursing shift duplicate detection,
  and ICU step-down medical necessity verification.
---

# ECHS Inpatient Tariff & Clinical Audit Rulebook

Use this rulebook whenever `payer_category == "ECHS"`. ECHS follows CGHS NABH-empanelled package rates and National Pharmaceutical Pricing Authority (NPPA) medical device ceilings.

## 1. Package Bundling & Anti-Unbundling Rules

### Percutaneous Transluminal Coronary Angioplasty (PTCA) & Cardiology Packages
- **Reference Package (`ECHS-CARD-PTCA-01`)**: An ECHS PTCA / Coronary Stenting package rate is **all-inclusive** of:
  - Cath-lab facility, C-arm fluoroscopy, and procedure room charges
  - Diagnostic angiogram performed in the same sitting
  - Hydrophilic/coronary guidewires, guiding catheters, radial/femoral introducer sheaths, Y-connectors, inflation syringes, pressure monitoring lines, sterile cath-lab drapes, and routine cath-lab consumables
  - Routine pre-procedure and post-procedure inpatient monitoring and standard nursing care
- **Separately Reimbursable Item**: Only **Drug-Eluting Stents (DES)** or Bare Metal Stents (BMS) are separately billable up to the **NPPA notified ceiling rate** (plus GST where applicable) with serial/batch sticker documentation.
- **Audit Action**: Any separately billed guidewire kit, cath-lab drape kit, radial sheath, balloon/inflation syringe, or cath-lab consumable alongside an active PTCA package MUST be classified as **`FLAGGED_UNBUNDLED`** with `potential_savings = total_amount`.

### General Inpatient & Medical Packages
- Routine nursing care, thermometer/BP monitoring, standard IV cannulation, and basic ward consumables are bundled within daily bed/package charges and cannot be unbundled separately.

## 2. Nursing Shift & Duplicate Diagnostic Checks
- Cross-check all diagnostic tests (e.g., Troponin-I, 12-Lead ECG, ABG, Echo) across nursing shift handovers (`Morning`, `Evening`, `Night`) and aliased service codes (`LAB-TROP-I` vs `CARD-TROP-STAT`, `DIAG-ECG-12` vs `EMG-ECG-12`).
- **Rule**: If the same clinical diagnostic test is billed more than once on the same day or across consecutive shifts WITHOUT:
  1. A distinct repeat `doctor_orders` entry (or explicit Q-hourly/BD serial order), AND
  2. A distinct timestamped `lab_reports` or diagnostic finding justifying the repeat,
  classify the unbacked duplicate entry as **`FLAGGED_DUPLICATE`** with `potential_savings = total_amount`.
- **Zero-False-Positive Safeguard**: When serial tests (e.g., 3x ABG analyses, 12x Q8H nebulizations, BiPAP sessions in COPD exacerbation) match explicit doctor orders, nursing logs, and lab reports, classify EVERY justified instance as **`VERIFIED_CLEAN`**.

## 3. ICU Step-Down & Bed-Day Medical Necessity
- Cross-check every billed `ICU_CHARGES` day against `clinical_notes` (progress/transfer notes) and `nursing_logs` (ward locations).
- **Rule**: If `clinical_notes` or `nursing_logs` explicitly document clinical stabilization and transfer out of ICU to a General Ward or Step-Down Ward (e.g., *"Patient stable, shifted to General Ward at 10:00 AM"*), any ICU bed-days billed on or after the transfer day alongside General Ward bed charges MUST be classified as **`FLAGGED_NO_MEDICAL_NECESSITY`** with `potential_savings = total_amount`.

## 4. Plain-English Output Requirements
- Cite the exact ECHS rule (e.g., `ECHS PTCA All-Inclusive Package Rule`, `ECHS Shift Duplicate Rule`, `ECHS ICU Step-Down Continuity Rule`) and exact evidence IDs (`order_id`, `note_id`, `log_id`, `report_id`).
