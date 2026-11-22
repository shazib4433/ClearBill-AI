"""Stage 1B: CSV, Excel (.xlsx), and PDF billing ledger parser for uploaded discharge bills."""

from __future__ import annotations

import csv
import io
import re
import uuid
from datetime import date
from typing import Any

from openpyxl import load_workbook
from pypdf import PdfReader

SAMPLE_CSV_CONTENT = """item_id,service_date,service_code,service_name,category,department,quantity,unit_price,total_amount,posted_by_shift,tariff_notes
UP-01,2026-11-15,PKG-PTCA-01,ECHS PTCA & Single Vessel Stenting Package (ECHS-CARD-PTCA-01),PACKAGE,Cardiology,1,95000,95000,Morning,All-inclusive ECHS PTCA package
UP-02,2026-11-15,IMP-DES-NPPA,Drug-Eluting Stent (NPPA Capped),IMPLANT,Cardiology,1,38933,38933,Morning,NPPA capped stent
UP-03,2026-11-15,LAB-TROP-I,Cardiac Troponin-I Quantitative Assay,DIAGNOSTICS,Biochemistry,1,1200,1200,Morning,Ordered in ER
UP-04,2026-11-15,CARD-TROP-STAT,Serum Troponin-I Assay (STAT Repeat),DIAGNOSTICS,Cardiology,1,1200,1200,Night,Night shift repeat entry without new order
UP-05,2026-11-15,CON-PTCA-GW,Hydrophilic Coronary Guidewire & Cath-Lab Drape Kit,CONSUMABLES,Cath Lab,1,14500,14500,Morning,Cath-lab guidewire consumable
"""

_COL_ALIASES: dict[str, list[str]] = {
    "item_id": ["item_id", "id", "line_id", "sr_no", "sno"],
    "service_date": ["service_date", "date", "bill_date", "tx_date"],
    "service_code": ["service_code", "code", "item_code", "his_code"],
    "service_name": ["service_name", "description", "item_name", "service", "particulars", "name"],
    "category": ["category", "head", "billing_head", "type"],
    "department": ["department", "dept", "specialty"],
    "quantity": ["quantity", "qty", "units", "days"],
    "unit_price": ["unit_price", "rate", "unit_rate", "price"],
    "total_amount": ["total_amount", "amount", "total", "gross_amount", "net_amount", "charges"],
    "posted_by_shift": ["posted_by_shift", "shift", "nursing_shift"],
    "tariff_notes": ["tariff_notes", "notes", "remarks", "comment"],
}

_SUMMARY_OR_HEADER_PHRASES = (
    "gross billed",
    "gross hospital billed",
    "gross amount",
    "gross total",
    "total billed",
    "grand total",
    "sub total",
    "subtotal",
    "net payable",
    "net consolidated",
    "net amount",
    "amount payable",
    "payable amount",
    "balance due",
    "balance payable",
    "total amount",
    "total charges",
    "total audited",
    "total deductions",
    "audited deductions",
    "deductions / savings",
    "total savings",
    "overbilling prevented",
    "total discount",
    "round off",
    "amount paid",
    "tpa approved",
    "patient payable",
    "cgst",
    "sgst",
    "igst",
    "gst total",
    "tax total",
    "bill total",
    "invoice total",
    "final total",
    "final amount",
    "clearbill ai",
    "service / procedure description",
    "service description",
    "deducted item",
    "particulars",
    "authorized sign-off",
    "approval timestamp",
    "sign-off remark",
)

_EXCLUDED_SECTION_MARKERS = (
    "audited deductions",
    "deductions & overbilling",
    "non-payable",
    "removed items",
    "disallowed items",
    "authorized sign-off",
    "financial summary",
    "payment summary",
)

_INCLUDED_SECTION_MARKERS = (
    "consolidated payable charges",
    "itemized charges",
    "billing details",
    "service / procedure description",
)

_MONEY_RE = re.compile(r"^(?:(?:₹|INR|Rs\.?)\s*)?([0-9][0-9,]*(?:\.\d{1,2})?)$", re.IGNORECASE)
_NEGATIVE_MONEY_RE = re.compile(r"^-\s*(?:(?:₹|INR|Rs\.?)\s*)?[0-9]|(?:₹|INR|Rs\.?)\s*-[0-9]", re.IGNORECASE)
_CODE_TOKEN_RE = re.compile(r"^[A-Z]{2,}[A-Z0-9_-]{1,24}$")
_SERIAL_TOKEN_RE = re.compile(r"^\d{1,3}\.?$")
_DATE_TOKEN_RE = re.compile(r"^(?:\d{4}-\d{2}-\d{2}|\d{2}[/-]\d{2}[/-]\d{4})$")
_QTY_TOKEN_RE = re.compile(r"^\d+(?:\.\d{1,2})?$")
_METADATA_LINE_RE = re.compile(
    r"^(?:Patient UHID|UHID|MRN|Patient Name|Patient|Payer|Insurance|TPA|Department|Specialty|"
    r"Admission(?: Date)?|Discharge(?: Date)?|Attending(?: Doctor)?|Doctor|Primary Diagnosis|Diagnosis|"
    r"Session|Finalized|Authorized Sign-Off|Approval Timestamp|Sign-Off Remark)\s*:",
    re.IGNORECASE,
)


def _to_float(val: Any, default: float = 0.0) -> float:
    if val is None:
        return default
    raw = str(val).strip()
    if _NEGATIVE_MONEY_RE.search(raw):
        return -abs(default if default > 0 else 1.0)
    cleaned = re.sub(r"(?:₹|INR|Rs\.?|,|\s)", "", raw, flags=re.IGNORECASE)
    if not cleaned:
        return default
    try:
        return float(cleaned)
    except ValueError:
        return default


def _parse_positive_money(token: str) -> float | None:
    stripped = token.strip()
    if not stripped or _NEGATIVE_MONEY_RE.search(stripped):
        return None
    m = _MONEY_RE.match(stripped)
    if not m:
        return None
    try:
        val = float(m.group(1).replace(",", ""))
        return val if val > 0 else None
    except ValueError:
        return None


def _is_summary_or_header_text(desc: str) -> bool:
    cleaned = (desc or "").strip()
    if len(re.findall(r"[A-Za-z]", cleaned)) < 3:
        return True
    lowered = re.sub(r"\s+", " ", cleaned.lower()).strip(" :-")
    if re.fullmatch(r"totals?", lowered):
        return True
    return any(phrase in lowered for phrase in _SUMMARY_OR_HEADER_PHRASES)


def _infer_category(service_name: str, raw_cat: str | None = None) -> str:
    if raw_cat and raw_cat.strip():
        return raw_cat.strip().upper()
    lower = service_name.lower()
    if "package" in lower or "pkg" in lower:
        return "PACKAGE"
    if "stent" in lower or "implant" in lower or "nail" in lower or "prosthesis" in lower:
        return "IMPLANT"
    if "icu" in lower:
        return "ICU_CHARGES"
    if "nicu" in lower or "phototherapy" in lower:
        return "NICU"
    if any(k in lower for k in ("room", "ward", "bed", "deluxe", "suite", "semi-private")):
        return "ROOM_RENT"
    if any(k in lower for k in ("guidewire", "sheath", "gauze", "dressing", "glove", "suture", "drape", "gown", "consumable")):
        return "CONSUMABLES"
    if any(k in lower for k in ("troponin", "ecg", "cbc", "platelet", "mri", "ncct", "ct ", "x-ray", "abg", "blood group", "usg", "echo", "kft")):
        return "DIAGNOSTICS"
    if any(k in lower for k in ("inj", "tab", "ceftriaxone", "antibiotic", "pharmacy", "med")):
        return "PHARMACY"
    if any(k in lower for k in ("infusion pump", "bipap", "ventilator")):
        return "EQUIPMENT"
    return "PROCEDURE"


def _normalize_row(row: dict[str, Any], idx: int, default_date: str) -> dict[str, Any] | None:
    lowered = {str(k).strip().lower().replace(" ", "_"): v for k, v in row.items() if k is not None}

    def pick(field: str) -> Any:
        for alias in _COL_ALIASES[field]:
            if alias in lowered and lowered[alias] not in (None, ""):
                return lowered[alias]
        return None

    service_name = str(pick("service_name") or "").strip()
    if not service_name or _is_summary_or_header_text(service_name):
        return None

    qty = _to_float(pick("quantity"), 1.0)
    if qty <= 0:
        qty = 1.0
    total = _to_float(pick("total_amount"), -1.0)
    unit = _to_float(pick("unit_price"), -1.0)

    if total < 0 and unit >= 0:
        total = round(qty * unit, 2)
    elif unit < 0 and total >= 0:
        unit = round(total / qty, 2)
    elif total < 0 and unit < 0:
        return None

    if total <= 0:
        return None

    item_id = str(pick("item_id") or f"UP-{idx:02d}").strip()
    service_code = str(pick("service_code") or f"SVC-{idx:03d}").strip()
    category = _infer_category(service_name, str(pick("category") or ""))
    department = str(pick("department") or "General Inpatient").strip()
    service_date = str(pick("service_date") or default_date).strip()
    shift = str(pick("posted_by_shift") or "Morning").strip()
    notes = str(pick("tariff_notes") or "").strip() or None

    return {
        "item_id": item_id,
        "service_date": service_date,
        "service_code": service_code,
        "service_name": service_name,
        "category": category,
        "department": department,
        "quantity": qty,
        "unit_price": unit,
        "total_amount": total,
        "posted_by_shift": shift,
        "tariff_notes": notes,
    }


def _parse_csv(file_bytes: bytes, default_date: str) -> list[dict[str, Any]]:
    text = file_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    items: list[dict[str, Any]] = []
    for idx, row in enumerate(reader, start=1):
        norm = _normalize_row(row, idx, default_date)
        if norm:
            items.append(norm)
    return items


def _parse_excel(file_bytes: bytes, default_date: str) -> list[dict[str, Any]]:
    wb = load_workbook(io.BytesIO(file_bytes), data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    headers = [str(h or f"col_{i}").strip() for i, h in enumerate(rows[0])]
    items: list[dict[str, Any]] = []
    for idx, values in enumerate(rows[1:], start=1):
        row_dict = {headers[i]: values[i] if i < len(values) else None for i in range(len(headers))}
        norm = _normalize_row(row_dict, idx, default_date)
        if norm:
            items.append(norm)
    return items


def _extract_pdf_metadata(layout_text: str) -> dict[str, str]:
    """Extract patient, payer, admission, and diagnosis metadata from PDF headers."""
    meta: dict[str, str] = {}
    key_patterns: list[tuple[str, re.Pattern[str]]] = [
        ("uhid", re.compile(r"^(?:Patient\s+)?(?:UHID|MRN)\s*:\s*(.+)$", re.IGNORECASE)),
        ("patient_name", re.compile(r"^Patient(?:\s+Name)?\s*:\s*(.+)$", re.IGNORECASE)),
        ("payer", re.compile(r"^(?:Payer|Insurance|TPA)\s*:\s*(.+)$", re.IGNORECASE)),
        ("department", re.compile(r"^(?:Department|Specialty)\s*:\s*(.+)$", re.IGNORECASE)),
        ("admission_date", re.compile(r"^Admission(?:\s+Date)?\s*:\s*(.+)$", re.IGNORECASE)),
        ("discharge_date", re.compile(r"^Discharge(?:\s+Date)?\s*:\s*(.+)$", re.IGNORECASE)),
        ("attending_doctor", re.compile(r"^(?:Attending(?:\s+Doctor)?|Doctor)\s*:\s*(.+)$", re.IGNORECASE)),
        ("primary_diagnosis", re.compile(r"^(?:Primary\s+)?Diagnosis\s*:\s*(.+)$", re.IGNORECASE)),
    ]

    for raw_line in layout_text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        segments = re.split(r"\s*\|\s*|\s{2,}", stripped)
        for seg in segments:
            seg_clean = seg.strip()
            if not seg_clean or ":" not in seg_clean:
                continue
            for field, pat in key_patterns:
                m = pat.match(seg_clean)
                if m:
                    val = m.group(1).strip()
                    if val and field not in meta:
                        meta[field] = val
                    break
    return meta


def _parse_pdf(file_bytes: bytes, default_date: str) -> tuple[list[dict[str, Any]], str, dict[str, str]]:
    reader = PdfReader(io.BytesIO(file_bytes))
    page_texts: list[str] = []
    for page in reader.pages:
        try:
            txt = page.extract_text(extraction_mode="layout") or ""
        except Exception:
            txt = page.extract_text() or ""
        if not txt.strip():
            txt = page.extract_text() or ""
        page_texts.append(txt)

    raw_text = "\n".join(page_texts)
    pdf_meta = _extract_pdf_metadata(raw_text)
    dept_default = pdf_meta.get("department") or "Uploaded PDF Ledger"

    items: list[dict[str, Any]] = []
    line_idx = 0
    in_excluded_section = False
    accept_continuation = False

    def _append_item(
        code: str,
        name: str,
        svc_date: str,
        qty: float,
        unit_price: float,
        total_amount: float,
    ) -> None:
        nonlocal line_idx, accept_continuation
        line_idx += 1
        items.append(
            {
                "item_id": f"UP-{line_idx:02d}",
                "service_date": svc_date,
                "service_code": code,
                "service_name": name,
                "category": _infer_category(name),
                "department": dept_default,
                "quantity": qty,
                "unit_price": unit_price,
                "total_amount": total_amount,
                "posted_by_shift": "Morning",
                "tariff_notes": "Extracted from uploaded hospital PDF bill.",
            }
        )
        accept_continuation = True

    for raw_line in raw_text.splitlines():
        leading_spaces = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = raw_line.strip()
        if not stripped:
            accept_continuation = False
            continue

        lowered = stripped.lower()

        # Section switches & page footers/headers
        if any(m in lowered for m in _EXCLUDED_SECTION_MARKERS):
            in_excluded_section = True
            accept_continuation = False
            continue
        if any(m in lowered for m in _INCLUDED_SECTION_MARKERS):
            in_excluded_section = False
            accept_continuation = False
            continue
        if re.search(r"\bPage\s+\d+\s+of\s+\d+\b", stripped, re.IGNORECASE) or "clearbill ai" in lowered:
            accept_continuation = False
            continue
        if _METADATA_LINE_RE.match(stripped):
            accept_continuation = False
            continue
        if in_excluded_section:
            accept_continuation = False
            continue

        # Format A: Pipe-delimited ("CODE | Description | Qty | Amount")
        if "|" in stripped:
            parts = [p.strip() for p in stripped.split("|")]
            if len(parts) >= 3:
                amt = _parse_positive_money(parts[-1])
                if amt is not None:
                    code = parts[0] if len(parts) >= 4 else f"PDF-{line_idx + 1:03d}"
                    name = parts[1] if len(parts) >= 4 else parts[0]
                    qty = _to_float(parts[-2], 1.0) if len(parts) >= 4 else 1.0
                    if qty <= 0 or qty > 365:
                        qty = 1.0
                    if not _is_summary_or_header_text(name):
                        _append_item(code, name, default_date, qty, round(amt / qty, 2), amt)
                        continue
            accept_continuation = False
            continue

        # Format B: Multi-column layout tokens split on 2+ spaces
        cols = [c.strip() for c in re.split(r"\s{2,}", stripped) if c.strip()]
        if len(cols) >= 2:
            amt = _parse_positive_money(cols[-1])
            if amt is not None:
                mid = list(cols[:-1])
                code = ""
                if len(mid) >= 2 and _CODE_TOKEN_RE.match(mid[0]):
                    code = mid.pop(0)
                elif len(mid) >= 2 and _SERIAL_TOKEN_RE.match(mid[0]):
                    mid.pop(0)

                svc_date = default_date
                qty = 1.0
                unit_price: float | None = None

                # Check trailing optional columns before final amount: [Date] [Qty] [Rate]
                if len(mid) >= 2:
                    maybe_rate = _parse_positive_money(mid[-1])
                    if len(mid) >= 3 and maybe_rate is not None and _QTY_TOKEN_RE.match(mid[-2]):
                        maybe_qty = float(mid[-2])
                        if 0 < maybe_qty <= 365 and abs(maybe_qty * maybe_rate - amt) <= max(1.0, amt * 0.02):
                            unit_price = maybe_rate
                            qty = maybe_qty
                            mid.pop()
                            mid.pop()
                    if unit_price is None and _QTY_TOKEN_RE.match(mid[-1]):
                        maybe_num = float(mid[-1])
                        if 0 < maybe_num <= 365:
                            qty = maybe_num
                            mid.pop()

                if len(mid) >= 2 and _DATE_TOKEN_RE.match(mid[-1]):
                    svc_date = mid.pop()

                # Also handle single-space "YYYY-MM-DD [qty]" combined inside a single column
                if mid:
                    m_tail = re.match(
                        r"^(.*\S)\s+(\d{4}-\d{2}-\d{2}|\d{2}[/-]\d{2}[/-]\d{4})(?:\s+(\d+(?:\.\d{1,2})?))?$",
                        mid[-1],
                    )
                    if m_tail:
                        mid[-1] = m_tail.group(1).strip()
                        svc_date = m_tail.group(2)
                        if m_tail.group(3):
                            q_val = float(m_tail.group(3))
                            if 0 < q_val <= 365:
                                qty = q_val

                desc = " ".join(mid).strip(" -:\t")
                if desc and not _is_summary_or_header_text(desc):
                    _append_item(
                        code or f"PDF-{line_idx + 1:03d}",
                        desc,
                        svc_date,
                        qty,
                        round(unit_price if unit_price is not None else amt / qty, 2),
                        amt,
                    )
                    continue

        # Wrapped continuation line under an active multi-column item (no monetary amount on line)
        if (
            accept_continuation
            and items
            and leading_spaces >= 6
            and not _NEGATIVE_MONEY_RE.search(stripped)
            and _parse_positive_money(cols[-1] if cols else "") is None
            and len(re.findall(r"[A-Za-z0-9]", stripped)) >= 2
            and not any(p in lowered for p in _SUMMARY_OR_HEADER_PHRASES)
        ):
            items[-1]["service_name"] = f"{items[-1]['service_name']} {stripped}".strip()
            items[-1]["category"] = _infer_category(items[-1]["service_name"])
            continue

        # Format C: Single-space fallback ("CODE Description [Date] [Qty] [Rs.] 1,200.00")
        accept_continuation = False
        if _NEGATIVE_MONEY_RE.search(stripped):
            continue
        m_single = re.match(
            r"^(?:([A-Z]{2,}[A-Z0-9_-]{1,24})\s+)?(.{4,120}?)\s+(?:₹|INR|Rs\.?)?\s*([0-9][0-9,]*(?:\.\d{1,2})?)$",
            stripped,
        )
        if m_single:
            amt = _parse_positive_money(m_single.group(3))
            desc_raw = m_single.group(2).strip(" -:\t")
            svc_date = default_date
            qty = 1.0

            m_dq = re.match(
                r"^(.*\S)\s+(\d{4}-\d{2}-\d{2}|\d{2}[/-]\d{2}[/-]\d{4})(?:\s+(\d+(?:\.\d{1,2})?))?$",
                desc_raw,
            )
            if m_dq:
                desc_raw = m_dq.group(1).strip()
                svc_date = m_dq.group(2)
                if m_dq.group(3):
                    q_val = float(m_dq.group(3))
                    if 0 < q_val <= 365:
                        qty = q_val

            if amt is not None and not _is_summary_or_header_text(desc_raw):
                _append_item(
                    m_single.group(1) or f"PDF-{line_idx + 1:03d}",
                    desc_raw,
                    svc_date,
                    qty,
                    round(amt / qty, 2),
                    amt,
                )

    return items, raw_text, pdf_meta


def parse_uploaded_bill(
    file_bytes: bytes,
    filename: str,
    payer_category: str,
    clinical_notes_text: str = "",
    room_entitlement_cap_per_day: float = 4500.0,
) -> tuple[dict[str, Any], str]:
    """Parse CSV, Excel (.xlsx), or PDF discharge bill into a standard ClearBill HIS bundle."""
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    today = date.today().isoformat()
    extracted_extra_notes = ""
    pdf_meta: dict[str, str] = {}

    if ext == "csv":
        source_type = "UPLOAD_CSV"
        ledger = _parse_csv(file_bytes, today)
    elif ext in ("xlsx", "xlsm"):
        source_type = "UPLOAD_EXCEL"
        ledger = _parse_excel(file_bytes, today)
    elif ext == "pdf":
        source_type = "UPLOAD_PDF"
        ledger, extracted_extra_notes, pdf_meta = _parse_pdf(file_bytes, today)
    else:
        raise ValueError(f"Unsupported file format '.{ext}'. Please upload CSV, XLSX, or PDF.")

    if not ledger:
        raise ValueError(
            "No valid itemized billing rows could be parsed from the uploaded file. "
            "Download the sample CSV template or ensure columns include service_name and total_amount."
        )

    extracted_uhid = re.sub(r"[^A-Za-z0-9-]", "", pdf_meta.get("uhid", "")).upper()
    short_id = uuid.uuid4().hex[:6].upper()
    uhid = f"UPLOAD-{extracted_uhid}-{short_id[:4]}" if extracted_uhid else f"UHID-UPLOAD-{short_id}"

    patient_name = pdf_meta.get("patient_name") or f"Uploaded Discharge Case ({filename})"
    department = pdf_meta.get("department") or ledger[0]["department"] or "Inpatient Care"
    primary_dx = pdf_meta.get("primary_diagnosis") or f"Uploaded Bill Audit ({filename})"
    admission_date = pdf_meta.get("admission_date") or ledger[0]["service_date"]
    discharge_date = pdf_meta.get("discharge_date") or today
    attending_doctor = pdf_meta.get("attending_doctor") or "Dr. Attending Consultant"
    payer_name = pdf_meta.get("payer") or f"{payer_category} Uploaded Claim"

    has_package = next((row for row in ledger if row["category"] == "PACKAGE"), None)
    combined_notes = (clinical_notes_text or "").strip()
    if not combined_notes:
        combined_notes = (
            f"Uploaded discharge ledger '{filename}' ({len(ledger)} items). "
            "No separate repeat orders recorded for duplicate shift postings."
        )
    if extracted_extra_notes and len(extracted_extra_notes.strip()) > 0 and not clinical_notes_text:
        combined_notes += f"\nPDF Context Snippet: {extracted_extra_notes[:400]}"

    # Synthesize minimal doctor orders for legitimate non-duplicate primary items so rule & AI audit have context
    orders = []
    seen_services: set[str] = set()
    for idx, row in enumerate(ledger, start=1):
        base_key = row["service_name"].split("(")[0].strip().lower()[:28]
        if row["category"] in ("CONSUMABLES",) and has_package:
            continue
        if "duplicate" in row["service_name"].lower() or "dup" in row["service_code"].lower() or "stat repeat" in row["service_name"].lower():
            continue
        if base_key not in seen_services:
            seen_services.add(base_key)
            orders.append(
                {
                    "order_id": f"ORD-UP-{idx:02d}",
                    "order_timestamp": f"{row['service_date']} 09:00",
                    "ordered_by": attending_doctor,
                    "order_type": row["category"],
                    "order_details": f"Ordered {row['service_name']} ({row['service_code']}).",
                    "frequency": "ONCE",
                    "status": "COMPLETED",
                }
            )

    bundle = {
        "patient": {
            "uhid": uhid,
            "full_name": patient_name,
            "age": 45,
            "gender": "Unspecified",
            "phone": "+91-98000-00000",
            "email": "upload-intake@clearbill.local",
            "address": "Uploaded Hospital Ledger Intake",
            "gov_id_type": "UPLOAD_REF",
            "gov_id_number": uhid,
            "payer_category": payer_category,
            "payer_name": payer_name,
            "policy_or_card_no": f"POL-{uhid}",
            "room_entitlement": f"Entitled Cap ₹{room_entitlement_cap_per_day:,.0f}/day",
            "room_entitlement_cap_per_day": room_entitlement_cap_per_day,
        },
        "admission": {
            "admission_id": f"ADM-UP-{short_id}",
            "uhid": uhid,
            "admission_date": admission_date,
            "discharge_date": discharge_date,
            "department": department,
            "attending_doctor": attending_doctor,
            "primary_diagnosis": primary_dx,
            "icd10_code": "Z00.0",
            "package_code": has_package["service_code"] if has_package else None,
            "package_name": has_package["service_name"] if has_package else None,
            "package_rate": has_package["total_amount"] if has_package else None,
            "room_category_billed": "Uploaded Ledger Room Category",
            "patient_upgrade_consent_signed": False,
            "discharge_summary_brief": combined_notes,
        },
        "billing_ledger": ledger,
        "doctor_orders": orders,
        "clinical_notes": [
            {
                "note_id": "NOTE-UP-01",
                "note_timestamp": f"{today} 10:00",
                "author_name": "Uploaded Clinical Summary",
                "author_role": "Discharge Summary",
                "note_type": "PROGRESS_NOTE",
                "note_text": combined_notes,
            }
        ],
        "nursing_logs": [],
        "lab_reports": [],
    }
    return bundle, source_type
