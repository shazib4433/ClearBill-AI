---
name: tpa-cashless-audit
description: >-
  Payer tariff and clinical audit rulebook for Private TPA Cashless health insurance policies
  (Star Health, HDFC ERGO, ICICI Lombard, Bajaj Allianz). Enforces room-rent entitlement caps
  and proportionate deduction alerts, IRDAI non-payable consumables exclusion, nursing shift
  duplicate detection, medical necessity checks, and zero-false-positive verification for
  clinically ordered serial monitoring or acute deterioration scans.
---

# Private TPA Cashless Audit Rulebook

Use this rulebook whenever `payer_category == "TPA"`. Private TPAs adjudicate cashless claims strictly against policy room-rent sub-limits, IRDAI Master Circular non-payable item lists, clinical progress notes, and physician order counts.

## 1. Room-Rent Entitlement Cap & Proportionate Deduction Risk
- Check `patient.room_entitlement_cap_per_day` against billed `ROOM_RENT` `unit_price`, and verify `admission.patient_upgrade_consent_signed`.
- **Rule**: When a patient entitled to a lower room category (e.g., Semi-Private Room capped at `₹4,500/day`) is billed for an upgraded room (e.g., Deluxe Suite at `₹9,000/day`) **without** a signed patient room-upgrade consent form (`patient_upgrade_consent_signed == False`):
  - Classify the room rent line item as **`FLAGGED_TARIFF_CAP_EXCEEDED`**.
  - Set `allowed_cap_amount = room_entitlement_cap_per_day * quantity`.
  - Set `potential_savings = total_amount - allowed_cap_amount`.
  - **CRITICAL WARNING**: Explicitly warn in `plain_english_remark` about **TPA Proportionate Deduction Risk** — private TPAs proportionally reduce all associated surgeon, OT, visit, and diagnostic charges by the room-rent ratio (`entitled_cap / billed_room_rate`) if an unconsented room upgrade is submitted on the claim.

## 2. IRDAI Non-Payable OT & Ward Consumables (Anti-Unbundling)
- Under IRDAI Standardization Guidelines (List I–IV Non-Payables) and standard surgical package terms, **disposable surgical gowns, sterile drapes, cautery pads, surgeon gloves, masks, shoe covers, ECG electrodes, and routine OT disposables** are non-payable or bundled into OT/surgery charges.
- **Audit Action**: Separately billed OT disposable packs (`CON-OT-DISP`, disposable gowns/drapes/cautery pads) during a surgical admission MUST be classified as **`FLAGGED_UNBUNDLED`** with `potential_savings = total_amount`.

## 3. Shift Duplicate & Dose-Count Reconciliation
- Reconcile pharmacy vials, infusion pump rentals, and blood-bank fees against `doctor_orders` dose schedules and `nursing_logs` administration entries:
  - **Medication Vial Over-Posting**: Count total ordered doses from `doctor_orders` (e.g., `BD x 3 days = 6 doses/vials`). If an extra night-shift batch of vials is posted beyond the ordered and administered dose count, flag the extra posting as **`FLAGGED_DUPLICATE`**.
  - **Equipment Shift Duplication**: If an infusion pump or monitoring device is already covered under a continuous multi-day package (`3-day package`) and a separate night-shift daily rental is double-posted on an overlapping day, flag the overlapping rental as **`FLAGGED_DUPLICATE`**.
  - **Blood Bank Cross-Matching Duplication**: If blood cross-matching is billed both inside a PRBC transfusion package/order and again as a standalone duplicate fee for the same 2-unit transfusion event without a separate second transfusion order, flag the duplicate fee as **`FLAGGED_DUPLICATE`**.

## 4. Clinical Medical Necessity Validation
- **Neonatal / NICU Charges**: If `NICU` admission, incubator, or phototherapy charges are billed for a newborn, verify pediatric `clinical_notes` and `lab_reports`. If pediatric notes state *"Normal APGAR"*, *"roomed-in with mother"*, *"no jaundice"*, or *"no NICU indication"*, classify the NICU/phototherapy billing as **`FLAGGED_NO_MEDICAL_NECESSITY`** with `potential_savings = total_amount`.

## 5. Zero-False-Positive Clinical Nuance (Mandatory Safeguard)
- **Serial Monitoring in Acute Illness (e.g., Dengue Thrombocytopenia)**: When `doctor_orders` specifies serial testing (e.g., *"CBC with Platelet Count Q12H"*) AND `lab_reports` contains distinct timestamped reports for each collection, **ALL** matching serial line items MUST be classified as **`VERIFIED_CLEAN`**. Never misclassify clinically ordered serial labs as duplicates.
- **Repeat Imaging on Acute Clinical Deterioration (e.g., Polytrauma)**: When a repeat `NCCT Head` or MRI has a **separate** `doctor_orders` entry, a **clinical progress note** documenting sudden clinical deterioration (e.g., *"GCS dropped from 15 to 11"*, *"anisocoria"*, *"urgent repeat NCCT Head ordered"*), and a **second** radiology report, classify the repeat scan as **`VERIFIED_CLEAN`**.
