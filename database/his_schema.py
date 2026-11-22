"""SQLAlchemy ORM definitions for the Read-Only Mock Hospital Information System (HIS) database."""

from __future__ import annotations

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class HISBase(DeclarativeBase):
    """Declarative base for Mock HIS tables."""


class Employee(HISBase):
    """Authorized hospital personnel authorized (or observer-restricted) for ClearBill actions."""

    __tablename__ = "employees"

    employee_uid: Mapped[str] = mapped_column(String(32), primary_key=True)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[str] = mapped_column(String(48), nullable=False)
    department: Mapped[str] = mapped_column(String(120), nullable=False)
    # None indicates unlimited override authority; 0.0 indicates read-only observer
    max_override_limit: Mapped[float | None] = mapped_column(Float, nullable=True)
    can_finalize_bill: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Patient(HISBase):
    """Patient demographic and payer policy master table (contains raw PHI locally)."""

    __tablename__ = "patients"

    uhid: Mapped[str] = mapped_column(String(32), primary_key=True)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    age: Mapped[int] = mapped_column(Integer, nullable=False)
    gender: Mapped[str] = mapped_column(String(16), nullable=False)
    phone: Mapped[str] = mapped_column(String(32), nullable=False)
    email: Mapped[str] = mapped_column(String(120), nullable=False)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    gov_id_type: Mapped[str] = mapped_column(String(48), nullable=False)
    gov_id_number: Mapped[str] = mapped_column(String(64), nullable=False)
    payer_category: Mapped[str] = mapped_column(String(24), nullable=False)
    payer_name: Mapped[str] = mapped_column(String(120), nullable=False)
    policy_or_card_no: Mapped[str] = mapped_column(String(64), nullable=False)
    room_entitlement: Mapped[str] = mapped_column(String(64), nullable=False)
    room_entitlement_cap_per_day: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)


class Admission(HISBase):
    """Inpatient admission, clinical diagnosis, package, and room upgrade consent details."""

    __tablename__ = "admissions"

    admission_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    uhid: Mapped[str] = mapped_column(String(32), ForeignKey("patients.uhid"), index=True, nullable=False)
    admission_date: Mapped[str] = mapped_column(String(32), nullable=False)
    discharge_date: Mapped[str] = mapped_column(String(32), nullable=False)
    department: Mapped[str] = mapped_column(String(80), nullable=False)
    attending_doctor: Mapped[str] = mapped_column(String(120), nullable=False)
    primary_diagnosis: Mapped[str] = mapped_column(String(255), nullable=False)
    icd10_code: Mapped[str] = mapped_column(String(24), nullable=False)
    package_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    package_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    package_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    room_category_billed: Mapped[str] = mapped_column(String(80), nullable=False)
    patient_upgrade_consent_signed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    discharge_summary_brief: Mapped[Text] = mapped_column(Text, nullable=False)


class BillingLedger(HISBase):
    """Itemized hospital billing ledger rows pending discharge audit."""

    __tablename__ = "billing_ledger"

    item_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    admission_id: Mapped[str] = mapped_column(String(32), ForeignKey("admissions.admission_id"), index=True, nullable=False)
    uhid: Mapped[str] = mapped_column(String(32), ForeignKey("patients.uhid"), index=True, nullable=False)
    service_date: Mapped[str] = mapped_column(String(32), nullable=False)
    service_code: Mapped[str] = mapped_column(String(48), nullable=False)
    service_name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(48), nullable=False)
    department: Mapped[str] = mapped_column(String(80), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    unit_price: Mapped[float] = mapped_column(Float, nullable=False)
    total_amount: Mapped[float] = mapped_column(Float, nullable=False)
    posted_by_shift: Mapped[str] = mapped_column(String(32), nullable=False)
    tariff_notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class DoctorOrder(HISBase):
    """Physician CPOE orders authorizing diagnostics, medications, implants, and procedures."""

    __tablename__ = "doctor_orders"

    order_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    admission_id: Mapped[str] = mapped_column(String(32), ForeignKey("admissions.admission_id"), index=True, nullable=False)
    uhid: Mapped[str] = mapped_column(String(32), ForeignKey("patients.uhid"), index=True, nullable=False)
    order_timestamp: Mapped[str] = mapped_column(String(32), nullable=False)
    ordered_by: Mapped[str] = mapped_column(String(120), nullable=False)
    order_type: Mapped[str] = mapped_column(String(48), nullable=False)
    order_details: Mapped[str] = mapped_column(Text, nullable=False)
    frequency: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="COMPLETED")


class ClinicalNote(HISBase):
    """Progress notes, operative notes, transfer notes, and pediatric evaluations."""

    __tablename__ = "clinical_notes"

    note_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    admission_id: Mapped[str] = mapped_column(String(32), ForeignKey("admissions.admission_id"), index=True, nullable=False)
    uhid: Mapped[str] = mapped_column(String(32), ForeignKey("patients.uhid"), index=True, nullable=False)
    note_timestamp: Mapped[str] = mapped_column(String(32), nullable=False)
    author_name: Mapped[str] = mapped_column(String(120), nullable=False)
    author_role: Mapped[str] = mapped_column(String(64), nullable=False)
    note_type: Mapped[str] = mapped_column(String(48), nullable=False)
    note_text: Mapped[str] = mapped_column(Text, nullable=False)


class NursingLog(HISBase):
    """Shift-by-shift inpatient nursing logs tracking ward location, infusions, and bedside care."""

    __tablename__ = "nursing_logs"

    log_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    admission_id: Mapped[str] = mapped_column(String(32), ForeignKey("admissions.admission_id"), index=True, nullable=False)
    uhid: Mapped[str] = mapped_column(String(32), ForeignKey("patients.uhid"), index=True, nullable=False)
    log_timestamp: Mapped[str] = mapped_column(String(32), nullable=False)
    shift: Mapped[str] = mapped_column(String(32), nullable=False)
    nurse_name: Mapped[str] = mapped_column(String(120), nullable=False)
    ward_location: Mapped[str] = mapped_column(String(80), nullable=False)
    entry_text: Mapped[str] = mapped_column(Text, nullable=False)


class LabReport(HISBase):
    """Verified laboratory and radiology diagnostic reports."""

    __tablename__ = "lab_reports"

    report_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    admission_id: Mapped[str] = mapped_column(String(32), ForeignKey("admissions.admission_id"), index=True, nullable=False)
    uhid: Mapped[str] = mapped_column(String(32), ForeignKey("patients.uhid"), index=True, nullable=False)
    sample_collected_at: Mapped[str] = mapped_column(String(32), nullable=False)
    reported_at: Mapped[str] = mapped_column(String(32), nullable=False)
    test_name: Mapped[str] = mapped_column(String(160), nullable=False)
    result_summary: Mapped[str] = mapped_column(Text, nullable=False)
    verified_by: Mapped[str] = mapped_column(String(120), nullable=False)
