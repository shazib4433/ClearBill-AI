"""Consolidated Final Discharge Bill generator (Downloadable PDF & Printable HTML)."""

from __future__ import annotations

import html
import json
import textwrap
from dataclasses import dataclass

from database.audit_schema import AuditSession, StagedLineItem


def _sanitize_pdf_text(text: str) -> str:
    """Normalize Unicode punctuation/currency to safe Latin-1 ASCII for standard PDF Type1 fonts."""
    if not text:
        return ""
    replacements = {
        "₹": "Rs. ",
        "—": "-",
        "–": "-",
        "→": "->",
        "•": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u00a0": " ",
    }
    out = str(text)
    for src, dst in replacements.items():
        out = out.replace(src, dst)
    return out.encode("latin-1", errors="replace").decode("latin-1")


def _pdf_escape(text: str) -> str:
    clean = _sanitize_pdf_text(text)
    return clean.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _fmt_num(val: float) -> str:
    return f"{float(val):,.2f}"


def _fmt_rs(val: float) -> str:
    return f"Rs. {_fmt_num(val)}"


def _fmt_inr_html(val: float) -> str:
    return f"₹{float(val):,.2f}"


@dataclass(frozen=True)
class _BillRow:
    code: str
    name: str
    service_date: str
    qty: str
    payable_amount: float
    note: str


@dataclass(frozen=True)
class _DeductionRow:
    code: str
    name: str
    billed: float
    allowed_cap: float | None
    saved: float
    reviewer: str
    remark: str


def _partition_consolidated_rows(
    items: list[StagedLineItem],
) -> tuple[list[_BillRow], list[_DeductionRow]]:
    payable_rows: list[_BillRow] = []
    deduction_rows: list[_DeductionRow] = []

    for it in items:
        qty_str = f"{it.quantity:g}"
        if it.current_state in ("VERIFIED_CLEAN", "RESTORED_WITH_OVERRIDE", "FINAL_APPROVED"):
            note = "Restored via Override" if it.last_modified_by_uid else "Verified Clean"
            payable_rows.append(
                _BillRow(
                    code=it.service_code,
                    name=it.service_name,
                    service_date=it.service_date,
                    qty=qty_str,
                    payable_amount=float(it.total_amount),
                    note=note,
                )
            )
        elif it.current_state == "REMOVED_CONFIRMED":
            saved = float(it.potential_savings if it.potential_savings > 0 else it.total_amount)
            cap = float(it.allowed_cap_amount) if it.allowed_cap_amount is not None else None
            if cap is not None and cap > 0:
                payable_rows.append(
                    _BillRow(
                        code=it.service_code,
                        name=f"{it.service_name} [Capped]",
                        service_date=it.service_date,
                        qty=qty_str,
                        payable_amount=cap,
                        note=f"Capped from {_fmt_rs(it.total_amount)}",
                    )
                )
            deduction_rows.append(
                _DeductionRow(
                    code=it.service_code,
                    name=it.service_name,
                    billed=float(it.total_amount),
                    allowed_cap=cap,
                    saved=saved,
                    reviewer=it.last_modified_by_uid or "-",
                    remark=it.last_justification_remark or it.plain_english_remark,
                )
            )

    return payable_rows, deduction_rows


def _extract_admission_dates(session: AuditSession) -> tuple[str, str, str]:
    try:
        raw = json.loads(session.raw_his_payload_json or "{}")
        adm = raw.get("admission", {}) or {}
        return (
            str(adm.get("admission_date") or "-"),
            str(adm.get("discharge_date") or "-"),
            str(adm.get("attending_doctor") or "-"),
        )
    except Exception:
        return ("-", "-", "-")


class _PDFCanvas:
    """Minimal multi-page A4 PDF content stream builder."""

    WIDTH = 595.0
    HEIGHT = 842.0
    LEFT = 42.0
    RIGHT = 553.0
    BOTTOM_LIMIT = 68.0

    def __init__(self, header_meta: str) -> None:
        self.header_meta = header_meta
        self.pages: list[list[str]] = []
        self.y = self.HEIGHT - 44.0
        self._new_page(first=True)

    def _new_page(self, first: bool = False) -> None:
        self.pages.append([])
        if first:
            self.y = self.HEIGHT - 40.0
        else:
            self.y = self.HEIGHT - 48.0
            self.text(self.LEFT, self.y, "ClearBill AI - Consolidated Final Discharge Bill (Continued)", size=9, bold=True, color=(0.35, 0.42, 0.52))
            self.y -= 10
            self.line(self.LEFT, self.y, self.RIGHT, self.y, color=(0.82, 0.86, 0.92))
            self.y -= 16

    def ensure_space(self, needed_pt: float) -> None:
        if self.y - needed_pt < self.BOTTOM_LIMIT:
            self._new_page(first=False)

    def _emit(self, cmd: str) -> None:
        self.pages[-1].append(cmd)

    def rect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        fill: tuple[float, float, float] | None = None,
        stroke: tuple[float, float, float] | None = None,
        line_width: float = 0.6,
    ) -> None:
        parts = ["q"]
        if fill is not None:
            parts.append(f"{fill[0]:.3f} {fill[1]:.3f} {fill[2]:.3f} rg")
        if stroke is not None:
            parts.append(f"{stroke[0]:.3f} {stroke[1]:.3f} {stroke[2]:.3f} RG {line_width:.2f} w")
        parts.append(f"{x:.2f} {y:.2f} {w:.2f} {h:.2f} re")
        if fill is not None and stroke is not None:
            parts.append("B")
        elif fill is not None:
            parts.append("f")
        else:
            parts.append("S")
        parts.append("Q")
        self._emit(" ".join(parts))

    def line(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        color: tuple[float, float, float] = (0.85, 0.88, 0.92),
        width: float = 0.6,
    ) -> None:
        self._emit(
            f"q {color[0]:.3f} {color[1]:.3f} {color[2]:.3f} RG {width:.2f} w "
            f"{x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S Q"
        )

    def text(
        self,
        x: float,
        y: float,
        content: str,
        size: float = 9.5,
        bold: bool = False,
        color: tuple[float, float, float] = (0.08, 0.12, 0.2),
    ) -> None:
        font = "/F2" if bold else "/F1"
        esc = _pdf_escape(content)
        self._emit(
            f"BT {font} {size:.1f} Tf {color[0]:.3f} {color[1]:.3f} {color[2]:.3f} rg "
            f"{x:.2f} {y:.2f} Td ({esc}) Tj ET"
        )

    def text_right(
        self,
        right_x: float,
        y: float,
        content: str,
        size: float = 9.5,
        bold: bool = False,
        color: tuple[float, float, float] = (0.08, 0.12, 0.2),
    ) -> None:
        approx_w = len(_sanitize_pdf_text(content)) * size * 0.51
        self.text(max(self.LEFT, right_x - approx_w), y, content, size=size, bold=bold, color=color)

    def finalize_bytes(self) -> bytes:
        total_pages = len(self.pages)
        for idx, page_cmds in enumerate(self.pages, start=1):
            footer_y = 32.0
            self.pages[idx - 1].append(
                "q 0.850 0.880 0.920 RG 0.50 w 42.00 44.00 m 553.00 44.00 l S Q"
            )
            self.pages[idx - 1].append(
                f"BT /F1 8.0 Tf 0.400 0.450 0.540 rg 42.00 {footer_y:.2f} Td "
                f"({_pdf_escape(self.header_meta)}) Tj ET"
            )
            page_lbl = f"Page {idx} of {total_pages}"
            approx_w = len(page_lbl) * 8.0 * 0.51
            self.pages[idx - 1].append(
                f"BT /F1 8.0 Tf 0.400 0.450 0.540 rg {553.0 - approx_w:.2f} {footer_y:.2f} Td "
                f"({_pdf_escape(page_lbl)}) Tj ET"
            )

        # Assemble valid PDF 1.4 objects:
        # 1: Catalog, 2: Pages, 3: F1 Helvetica, 4: F2 Helvetica-Bold,
        # then pairs (Page obj, Content stream obj)
        objs: list[bytes] = []
        objs.append(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")

        page_obj_nums = [5 + i * 2 for i in range(total_pages)]
        kids_str = " ".join(f"{n} 0 R" for n in page_obj_nums)
        objs.append(f"2 0 obj\n<< /Type /Pages /Kids [{kids_str}] /Count {total_pages} >>\nendobj\n".encode("latin-1"))
        objs.append(b"3 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n")
        objs.append(b"4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>\nendobj\n")

        for i, cmds in enumerate(self.pages):
            page_num = 5 + i * 2
            content_num = page_num + 1
            stream_bytes = "\n".join(cmds).encode("latin-1", errors="replace")
            page_obj = (
                f"{page_num} 0 obj\n"
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                f"/Contents {content_num} 0 R /Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> >>\n"
                f"endobj\n"
            ).encode("latin-1")
            content_obj = (
                f"{content_num} 0 obj\n<< /Length {len(stream_bytes)} >>\nstream\n".encode("latin-1")
                + stream_bytes
                + b"\nendstream\nendobj\n"
            )
            objs.append(page_obj)
            objs.append(content_obj)

        out = bytearray()
        out.extend(b"%PDF-1.4\n")
        offsets = [0]
        for obj in objs:
            offsets.append(len(out))
            out.extend(obj)
        xref_pos = len(out)
        out.extend(f"xref\n0 {len(objs) + 1}\n".encode("latin-1"))
        out.extend(b"0000000000 65535 f \n")
        for off in offsets[1:]:
            out.extend(f"{off:010d} 00000 n \n".encode("latin-1"))
        out.extend(
            f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode(
                "latin-1"
            )
        )
        return bytes(out)


def build_consolidated_bill_pdf(session: AuditSession, items: list[StagedLineItem]) -> bytes:
    """Build a downloadable consolidated final discharge bill PDF for a FINAL_APPROVED session."""
    payable_rows, deduction_rows = _partition_consolidated_rows(items)
    adm_date, disch_date, doctor = _extract_admission_dates(session)

    c = _PDFCanvas(
        f"ClearBill AI Consolidated Discharge Bill | UHID: {session.uhid} | Session: {session.session_id}"
    )

    # Top Hospital Header Banner
    c.rect(c.LEFT, c.y - 54, c.RIGHT - c.LEFT, 58, fill=(0.06, 0.15, 0.27))
    c.text(c.LEFT + 14, c.y - 20, "CLEARBILL AI - CONSOLIDATED FINAL DISCHARGE BILL", size=13, bold=True, color=(1, 1, 1))
    c.text(
        c.LEFT + 14,
        c.y - 36,
        f"Session: {session.session_id}   |   Finalized: {session.finalized_at or session.updated_at}",
        size=8.5,
        color=(0.78, 0.85, 0.94),
    )
    c.rect(c.RIGHT - 118, c.y - 34, 104, 20, fill=(0.04, 0.48, 0.46))
    c.text(c.RIGHT - 108, c.y - 27, "FINAL APPROVED", size=8.5, bold=True, color=(1, 1, 1))
    c.y -= 70

    # Patient & Payer Details Box
    c.rect(c.LEFT, c.y - 78, c.RIGHT - c.LEFT, 76, fill=(0.97, 0.98, 0.99), stroke=(0.82, 0.86, 0.92))
    c.text(c.LEFT + 12, c.y - 18, f"Patient UHID: {session.uhid}", size=9.5, bold=True)
    c.text(c.LEFT + 230, c.y - 18, f"Patient: {session.patient_display_masked}", size=9.5, bold=True)
    c.text(c.LEFT + 12, c.y - 33, f"Payer: {session.payer_category} - {session.payer_name}", size=9.0)
    c.text(c.LEFT + 340, c.y - 33, f"Department: {session.department}", size=9.0)
    c.text(c.LEFT + 12, c.y - 48, f"Admission: {adm_date}  |  Discharge: {disch_date}  |  Attending: {doctor}", size=8.5, color=(0.25, 0.32, 0.42))
    diag_lines = textwrap.wrap(f"Primary Diagnosis: {session.primary_diagnosis}", width=92)
    c.text(c.LEFT + 12, c.y - 63, diag_lines[0] if diag_lines else "", size=8.5, color=(0.25, 0.32, 0.42))
    c.y -= 96

    # Section 1: Consolidated Approved Payable Charges
    c.ensure_space(60)
    c.text(c.LEFT, c.y, "1. CONSOLIDATED PAYABLE CHARGES (READY FOR SUBMISSION)", size=10.5, bold=True, color=(0.06, 0.15, 0.27))
    c.y -= 14

    # Table Header
    c.rect(c.LEFT, c.y - 18, c.RIGHT - c.LEFT, 18, fill=(0.93, 0.95, 0.98), stroke=(0.82, 0.86, 0.92))
    c.text(c.LEFT + 6, c.y - 12, "Code", size=8.5, bold=True)
    c.text(c.LEFT + 92, c.y - 12, "Service / Procedure Description", size=8.5, bold=True)
    c.text(c.LEFT + 356, c.y - 12, "Date", size=8.5, bold=True)
    c.text(c.LEFT + 422, c.y - 12, "Qty", size=8.5, bold=True)
    c.text_right(c.RIGHT - 8, c.y - 12, "Payable (Rs.)", size=8.5, bold=True)
    c.y -= 22

    for row in payable_rows:
        wrapped = textwrap.wrap(row.name, width=48) or [row.name]
        row_h = max(18, len(wrapped) * 11 + 8)
        c.ensure_space(row_h + 6)
        c.text(c.LEFT + 6, c.y - 10, row.code[:16], size=8.3, bold=True)
        for line_i, line_txt in enumerate(wrapped):
            c.text(c.LEFT + 92, c.y - 10 - line_i * 11, line_txt, size=8.5)
        c.text(c.LEFT + 356, c.y - 10, row.service_date, size=8.3)
        c.text(c.LEFT + 422, c.y - 10, row.qty, size=8.3)
        c.text_right(c.RIGHT - 8, c.y - 10, _fmt_num(row.payable_amount), size=8.5, bold=True)
        c.y -= row_h
        c.line(c.LEFT, c.y + 2, c.RIGHT, c.y + 2, color=(0.91, 0.93, 0.96), width=0.4)

    c.y -= 14

    # Section 2: Audited Deductions & Savings
    if deduction_rows:
        c.ensure_space(58)
        c.text(c.LEFT, c.y, "2. AUDITED DEDUCTIONS & OVERBILLING PREVENTED", size=10.5, bold=True, color=(0.68, 0.14, 0.14))
        c.y -= 14
        c.rect(c.LEFT, c.y - 18, c.RIGHT - c.LEFT, 18, fill=(0.99, 0.94, 0.94), stroke=(0.94, 0.8, 0.8))
        c.text(c.LEFT + 6, c.y - 12, "Code", size=8.5, bold=True)
        c.text(c.LEFT + 92, c.y - 12, "Deducted Item & Audit Remark", size=8.5, bold=True)
        c.text(c.LEFT + 358, c.y - 12, "UID", size=8.5, bold=True)
        c.text_right(c.RIGHT - 8, c.y - 12, "Saved (Rs.)", size=8.5, bold=True)
        c.y -= 22

        for d in deduction_rows:
            desc_wrapped = textwrap.wrap(f"{d.name} - {d.remark}", width=50)[:2]
            row_h = max(20, len(desc_wrapped) * 11 + 8)
            c.ensure_space(row_h + 6)
            c.text(c.LEFT + 6, c.y - 10, d.code[:16], size=8.3, bold=True)
            for line_i, line_txt in enumerate(desc_wrapped):
                c.text(c.LEFT + 92, c.y - 10 - line_i * 11, line_txt, size=8.2)
            c.text(c.LEFT + 358, c.y - 10, d.reviewer, size=8.2)
            c.text_right(c.RIGHT - 8, c.y - 10, f"-{_fmt_num(d.saved)}", size=8.5, bold=True, color=(0.05, 0.45, 0.28))
            c.y -= row_h
            c.line(c.LEFT, c.y + 2, c.RIGHT, c.y + 2, color=(0.95, 0.90, 0.90), width=0.4)

        c.y -= 14

    # Section 3: Consolidated Totals Box
    c.ensure_space(135)
    c.rect(c.LEFT, c.y - 68, c.RIGHT - c.LEFT, 68, fill=(0.95, 0.98, 0.97), stroke=(0.65, 0.85, 0.78), line_width=0.9)
    c.text(c.LEFT + 14, c.y - 18, "Gross Billed Amount:", size=9.5, color=(0.25, 0.32, 0.42))
    c.text_right(c.RIGHT - 14, c.y - 18, _fmt_rs(session.total_billed_amount), size=9.5, bold=True)

    c.text(c.LEFT + 14, c.y - 35, "Total Audited Deductions / Savings:", size=9.5, color=(0.05, 0.45, 0.28))
    c.text_right(c.RIGHT - 14, c.y - 35, f"-{_fmt_rs(session.confirmed_savings_amount)}", size=9.5, bold=True, color=(0.05, 0.45, 0.28))

    c.line(c.LEFT + 12, c.y - 43, c.RIGHT - 12, c.y - 43, color=(0.7, 0.85, 0.78), width=0.7)
    c.text(c.LEFT + 14, c.y - 58, "NET CONSOLIDATED PAYABLE AMOUNT:", size=11, bold=True, color=(0.06, 0.15, 0.27))
    c.text_right(c.RIGHT - 14, c.y - 58, _fmt_rs(session.net_payable_amount), size=11.5, bold=True, color=(0.06, 0.15, 0.27))
    c.y -= 82

    # Section 4: Digital Governance Sign-Off
    c.rect(c.LEFT, c.y - 56, c.RIGHT - c.LEFT, 54, fill=(0.98, 0.98, 0.99), stroke=(0.82, 0.86, 0.92))
    c.text(
        c.LEFT + 12,
        c.y - 16,
        f"Authorized Sign-Off: {session.finalized_by_name or '-'} ({session.finalized_by_uid or '-'}, {session.finalized_by_role or '-'})",
        size=9.0,
        bold=True,
    )
    c.text(c.LEFT + 12, c.y - 30, f"Approval Timestamp: {session.finalized_at or '-'}   |   Payer Rulebook: {session.skill_loaded}", size=8.3, color=(0.30, 0.36, 0.45))
    rem_lines = textwrap.wrap(f"Sign-Off Remark: {session.finalization_remark or '-'}", width=92)
    c.text(c.LEFT + 12, c.y - 44, rem_lines[0] if rem_lines else "", size=8.3, color=(0.25, 0.30, 0.38))

    return c.finalize_bytes()


def build_printable_bill_html(session: AuditSession, items: list[StagedLineItem]) -> str:
    """Build a crisp print-ready Consolidated Final Discharge Bill HTML view."""
    payable_rows, deduction_rows = _partition_consolidated_rows(items)
    adm_date, disch_date, doctor = _extract_admission_dates(session)
    e = html.escape

    payable_html = "\n".join(
        f"""
        <tr>
          <td class="mono">{e(r.code)}</td>
          <td>
            <div class="svc-name">{e(r.name)}</div>
            <div class="svc-note">{e(r.note)}</div>
          </td>
          <td>{e(r.service_date)}</td>
          <td class="num">{e(r.qty)}</td>
          <td class="num bold">{e(_fmt_inr_html(r.payable_amount))}</td>
        </tr>"""
        for r in payable_rows
    )

    deductions_section = ""
    if deduction_rows:
        ded_rows_html = "\n".join(
            f"""
            <tr>
              <td class="mono">{e(d.code)}</td>
              <td>
                <div class="svc-name">{e(d.name)}</div>
                <div class="svc-note">{e(d.remark)}</div>
              </td>
              <td class="num">{e(_fmt_inr_html(d.billed))}</td>
              <td class="num">{e(_fmt_inr_html(d.allowed_cap)) if d.allowed_cap is not None else "₹0.00"}</td>
              <td class="num bold saved">-{e(_fmt_inr_html(d.saved))}</td>
              <td class="mono">{e(d.reviewer)}</td>
            </tr>"""
            for d in deduction_rows
        )
        deductions_section = f"""
        <section class="section">
          <h2>2. Audited Deductions &amp; Overbilling Prevented</h2>
          <table>
            <thead>
              <tr>
                <th>Code</th>
                <th>Deducted Item &amp; Audit Justification</th>
                <th class="num">Billed</th>
                <th class="num">Allowed Cap</th>
                <th class="num">Saved</th>
                <th>Reviewer UID</th>
              </tr>
            </thead>
            <tbody>{ded_rows_html}</tbody>
          </table>
        </section>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Consolidated Discharge Bill — {e(session.uhid)}</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", sans-serif; margin: 0; padding: 24px; background: #f1f5f9; color: #0f172a; }}
    .toolbar {{ max-width: 920px; margin: 0 auto 16px auto; display: flex; justify-content: space-between; align-items: center; gap: 12px; background: #ffffff; border: 1px solid #cbd5e1; border-radius: 10px; padding: 12px 16px; }}
    .btn {{ display: inline-flex; align-items: center; gap: 6px; padding: 8px 14px; border-radius: 8px; font-size: 13px; font-weight: 600; text-decoration: none; border: 1px solid #cbd5e1; cursor: pointer; background: #ffffff; color: #0f172a; }}
    .btn-primary {{ background: #0f2744; color: #ffffff; border-color: #0f2744; }}
    .btn-teal {{ background: #0b7a75; color: #ffffff; border-color: #0b7a75; }}
    .sheet {{ max-width: 920px; margin: 0 auto; background: #ffffff; border: 1px solid #cbd5e1; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 16px rgba(15, 23, 42, 0.08); }}
    .banner {{ background: #0f2744; color: #ffffff; padding: 20px 24px; display: flex; justify-content: space-between; align-items: center; gap: 12px; }}
    .banner h1 {{ margin: 0; font-size: 18px; letter-spacing: 0.02em; }}
    .banner p {{ margin: 4px 0 0 0; font-size: 12px; color: #cbd5e1; }}
    .seal {{ background: #0b7a75; color: #ffffff; padding: 6px 12px; border-radius: 9999px; font-size: 11px; font-weight: 700; letter-spacing: 0.04em; }}
    .meta-grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px 20px; padding: 16px 24px; background: #f8fafc; border-bottom: 1px solid #e2e8f0; font-size: 13px; }}
    .section {{ padding: 18px 24px 8px 24px; }}
    .section h2 {{ margin: 0 0 10px 0; font-size: 13px; text-transform: uppercase; letter-spacing: 0.04em; color: #1e3a5f; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 12.5px; }}
    th {{ text-align: left; padding: 8px 10px; background: #f1f5f9; border-bottom: 1px solid #cbd5e1; color: #334155; font-size: 11.5px; }}
    td {{ padding: 8px 10px; border-bottom: 1px solid #e2e8f0; vertical-align: top; }}
    .num {{ text-align: right; white-space: nowrap; }}
    .mono {{ font-family: ui-monospace, Menlo, monospace; font-weight: 600; font-size: 11.5px; }}
    .bold {{ font-weight: 700; }}
    .saved {{ color: #047857; }}
    .svc-name {{ font-weight: 600; color: #0f172a; }}
    .svc-note {{ font-size: 11px; color: #64748b; margin-top: 2px; }}
    .summary {{ margin: 18px 24px; padding: 16px 20px; background: #ecfdf5; border: 1px solid #a7f3d0; border-radius: 10px; }}
    .summary-row {{ display: flex; justify-content: space-between; font-size: 13px; margin-bottom: 6px; }}
    .summary-total {{ display: flex; justify-content: space-between; font-size: 17px; font-weight: 800; color: #064e3b; padding-top: 8px; border-top: 1px solid #6ee7b7; margin-top: 6px; }}
    .signoff {{ margin: 0 24px 24px 24px; padding: 12px 16px; background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 8px; font-size: 12px; color: #334155; }}
    @media print {{
      body {{ background: #ffffff; padding: 0; }}
      .no-print {{ display: none !important; }}
      .sheet {{ border: none; box-shadow: none; max-width: 100%; }}
    }}
  </style>
</head>
<body>
  <div class="toolbar no-print">
    <div>
      <strong>Consolidated Final Discharge Bill</strong> &mdash; {e(session.uhid)} ({e(session.payer_category)})
    </div>
    <div style="display:flex; gap:8px;">
      <a class="btn btn-teal" href="/api/sessions/{e(session.session_id)}/final-bill.pdf" download>Download PDF</a>
      <button class="btn btn-primary" onclick="window.print()">Print / Save as PDF</button>
    </div>
  </div>

  <div class="sheet">
    <div class="banner">
      <div>
        <h1>CLEARBILL AI &mdash; CONSOLIDATED FINAL DISCHARGE BILL</h1>
        <p>Audit Session: {e(session.session_id)} &bull; Payer Rulebook: {e(session.skill_loaded)}</p>
      </div>
      <div class="seal">FINAL APPROVED</div>
    </div>

    <div class="meta-grid">
      <div><strong>UHID:</strong> {e(session.uhid)}</div>
      <div><strong>Patient:</strong> {e(session.patient_display_masked)}</div>
      <div><strong>Payer:</strong> {e(session.payer_category)} &mdash; {e(session.payer_name)}</div>
      <div><strong>Department:</strong> {e(session.department)}</div>
      <div><strong>Admission / Discharge:</strong> {e(adm_date)} to {e(disch_date)}</div>
      <div><strong>Attending Doctor:</strong> {e(doctor)}</div>
      <div style="grid-column: 1 / -1;"><strong>Primary Diagnosis:</strong> {e(session.primary_diagnosis)}</div>
    </div>

    <section class="section">
      <h2>1. Consolidated Payable Charges</h2>
      <table>
        <thead>
          <tr>
            <th>Code</th>
            <th>Service / Procedure Description</th>
            <th>Date</th>
            <th class="num">Qty</th>
            <th class="num">Payable</th>
          </tr>
        </thead>
        <tbody>{payable_html}</tbody>
      </table>
    </section>

    {deductions_section}

    <div class="summary">
      <div class="summary-row">
        <span>Gross Hospital Billed Amount</span>
        <strong>{e(_fmt_inr_html(session.total_billed_amount))}</strong>
      </div>
      <div class="summary-row saved">
        <span>Audited Deductions &amp; Overbilling Prevented</span>
        <strong>-{e(_fmt_inr_html(session.confirmed_savings_amount))}</strong>
      </div>
      <div class="summary-total">
        <span>NET CONSOLIDATED PAYABLE AMOUNT</span>
        <span>{e(_fmt_inr_html(session.net_payable_amount))}</span>
      </div>
    </div>

    <div class="signoff">
      <div><strong>Final Sign-Off:</strong> {e(session.finalized_by_name or "-")} ({e(session.finalized_by_uid or "-")}, {e(session.finalized_by_role or "-")}) at {e(session.finalized_at or "-")}</div>
      <div style="margin-top:4px;"><strong>Sign-Off Remark:</strong> {e(session.finalization_remark or "-")}</div>
    </div>
  </div>
</body>
</html>"""
