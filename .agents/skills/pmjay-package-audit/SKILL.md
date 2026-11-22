---
name: pmjay-package-audit
description: >-
  Payer tariff and clinical audit rulebook for Ayushman Bharat Pradhan Mantri Jan Arogya Yojana
  (PM-JAY HBP 2.2) fixed surgical and medical packages. Enforces strict all-inclusive package
  caps and zero-tolerance anti-unbundling of pre-op diagnostics and OT consumables.
---

# Ayushman Bharat PM-JAY Package Audit Rulebook

Use this rulebook whenever `payer_category == "PM-JAY"`. PM-JAY operates on fixed **Health Benefit Package (HBP)** rates that are strictly cashless and all-inclusive from admission through discharge.

## 1. Strict All-Inclusive Fixed Package Rule
Under National Health Authority (NHA) PM-JAY HBP guidelines, a surgical or medical package rate (such as Laparoscopic/Open Appendectomy `PKG-PMJAY-APP`) is a **100% all-inclusive bundled tariff** covering:
1. **Bed & Ward Charges** (pre-op, intra-op, and post-op stay)
2. **Pre-Operative & Routine Diagnostics** (CBC, Hemoglobin, Blood Grouping & Rh Typing, Urine Routine, RFT/LFT/Blood Sugar, ECG, Chest X-Ray required for pre-anesthetic clearance)
3. **Operation Theatre & Anesthesia Charges**
4. **All Routine OT & Ward Consumables** (sterile surgical gloves, surgical blades, Vicryl/silk/nylon sutures, IV sets, syringes, swabs, gauze, antiseptics, dressings)
5. **Surgeon Fees, Specialist Consultations, and Routine Peri-Operative Medications**

## 2. Anti-Unbundling Enforcement
- Whenever a PM-JAY surgical package (`PKG-PMJAY-*`) is present on the discharge ledger:
  - Keep the primary PM-JAY package line item as **`VERIFIED_CLEAN`**.
  - Flag **EVERY** separately billed pre-operative diagnostic (e.g., `Pre-Op CBC`, `Blood Grouping & Rh Typing`) and **EVERY** separately billed surgical/OT consumable (`Sterile Surgical Gloves`, `Vicryl Sutures`, dressings, IV sets) as **`FLAGGED_UNBUNDLED`** with `potential_savings = total_amount`.
  - Cite `PM-JAY HBP Fixed All-Inclusive Package & Anti-Unbundling Mandate` in `payer_rule_cited`.
- Under PM-JAY rules, beneficiaries cannot be balance-billed and the National/State Health Agency rejects any claim carrying unbundled routine consumables or pre-op workup alongside an HBP surgical code.
