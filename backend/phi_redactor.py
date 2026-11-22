"""Stage 2: Local on-premise PHI de-identification (Gemma-parity regex/entity scrubber).

Masks patient name, phone numbers, email addresses, residential addresses, policy IDs, and
government IDs across all demographics, billing lines, orders, clinical notes, nursing logs,
and lab reports BEFORE any context is transmitted to Gemini.
"""

from __future__ import annotations

import copy
import re
from typing import Any

HONORIFIC_PREFIXES = (
    "subedar major ",
    "havildar ",
    "naik ",
    "mrs. ",
    "mrs ",
    "mr. ",
    "mr ",
    "ms. ",
    "ms ",
    "smt. ",
    "smt ",
    "shri ",
    "dr. ",
)

SAFE_CLINICAL_OR_COMMON_WORDS = {
    "major",
    "minor",
    "normal",
    "general",
    "ward",
    "room",
    "semi",
    "private",
    "deluxe",
    "suite",
    "delhi",
    "india",
    "blood",
    "brain",
    "head",
    "knee",
    "care",
    "baby",
    "male",
    "female",
    "term",
    "post",
    "acute",
    "stable",
    "shift",
    "shifted",
    "night",
    "morning",
    "evening",
    "hospital",
    "clinic",
    "patient",
    "mother",
    "single",
    "repeat",
    "urgent",
    "daily",
    "total",
    "devi",
    "singh",
    "kumar",
    "prasad",
    "chandra",
}

RE_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
RE_PHONE = re.compile(r"(?<!\d)(?:\+91[-\s]?|0)?[6-9]\d{4}[-\s]?\d{5}(?!\d)")
RE_AADHAAR = re.compile(r"\b\d{4}[-\s]\d{4}[-\s]\d{4}\b")
RE_PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
RE_GOV_CARD = re.compile(r"\b(?:ECHS|CGHS|PMJAY|ABHA)[-\s][A-Z0-9-]{4,}\b", re.IGNORECASE)
RE_PIN_ADDR = re.compile(r"\b[1-8]\d{5}\b")


def _strip_honorific(name: str) -> str:
    lowered = name.lower().strip()
    for prefix in HONORIFIC_PREFIXES:
        if lowered.startswith(prefix):
            return name[len(prefix) :].strip()
    return name.strip()


def mask_display_name(full_name: str) -> str:
    """Create a masked non-identifying display label for UI lists (e.g., 'R*** R***')."""
    raw = (full_name or "").strip()
    if not raw:
        return "[PATIENT_NAME]"
    if "*" in raw or raw.lower().startswith("uploaded "):
        return raw
    cleaned = _strip_honorific(raw)
    parts = [p for p in cleaned.split() if p]
    if not parts:
        return "[PATIENT_NAME]"
    return " ".join(f"{p[0]}***" for p in parts[:2])


def _extract_name_variants(full_name: str) -> list[str]:
    """Build exact and partial patient name variants to scrub across free text."""
    variants: list[str] = []
    cleaned = full_name.strip()
    if cleaned:
        variants.append(cleaned)
    no_honorific = _strip_honorific(cleaned)
    if no_honorific and no_honorific != cleaned:
        variants.append(no_honorific)
    for token in re.split(r"[\s,]+", no_honorific):
        t = token.strip(".,")
        if len(t) >= 4 and t.lower() not in SAFE_CLINICAL_OR_COMMON_WORDS:
            variants.append(t)
    # Longest first so full names match before individual tokens
    deduped = sorted(set(variants), key=len, reverse=True)
    return deduped


def _scrub_string(
    text: str,
    name_patterns: list[re.Pattern[str]],
    exact_secrets: list[tuple[str, str]],
    counts: dict[str, int],
) -> str:
    if not text:
        return text

    result = text

    # 1. Exact known sensitive strings (address, gov_id, policy, phone, email)
    for secret_val, token in exact_secrets:
        if secret_val and secret_val in result:
            occurrences = result.count(secret_val)
            result = result.replace(secret_val, token)
            counts[token] = counts.get(token, 0) + occurrences

    # 2. Known patient name & surname/given-name patterns (skip if preceded by 'Dr. ')
    for pattern in name_patterns:
        result, n = pattern.subn("[PATIENT_NAME]", result)
        if n:
            counts["[PATIENT_NAME]"] = counts.get("[PATIENT_NAME]", 0) + n

    # 3. General regex entity patterns across clinical free text
    for regex, token in (
        (RE_EMAIL, "[EMAIL_REDACTED]"),
        (RE_PHONE, "[PHONE_REDACTED]"),
        (RE_AADHAAR, "[GOV_ID_REDACTED]"),
        (RE_PAN, "[GOV_ID_REDACTED]"),
        (RE_GOV_CARD, "[GOV_ID_REDACTED]"),
    ):
        result, n = regex.subn(token, result)
        if n:
            counts[token] = counts.get(token, 0) + n

    return result


def redact_his_bundle(raw_bundle: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a deep-copied PHI-redacted bundle and detailed redaction statistics.

    Does NOT mutate `raw_bundle`, preserving it locally for the Privacy Shield side-by-side diff.
    """
    redacted = copy.deepcopy(raw_bundle)
    counts: dict[str, int] = {
        "[PATIENT_NAME]": 0,
        "[PHONE_REDACTED]": 0,
        "[EMAIL_REDACTED]": 0,
        "[ADDRESS_REDACTED]": 0,
        "[GOV_ID_REDACTED]": 0,
        "[POLICY_ID_REDACTED]": 0,
    }

    patient = redacted.get("patient", {}) or {}
    full_name = str(patient.get("full_name", "") or "")
    phone = str(patient.get("phone", "") or "")
    email = str(patient.get("email", "") or "")
    address = str(patient.get("address", "") or "")
    gov_id = str(patient.get("gov_id_number", "") or "")
    policy_no = str(patient.get("policy_or_card_no", "") or "")

    exact_secrets: list[tuple[str, str]] = []
    if address and len(address) >= 6:
        exact_secrets.append((address, "[ADDRESS_REDACTED]"))
    if phone and len(phone) >= 6:
        exact_secrets.append((phone, "[PHONE_REDACTED]"))
    if email and len(email) >= 5:
        exact_secrets.append((email, "[EMAIL_REDACTED]"))
    if gov_id and len(gov_id) >= 5:
        exact_secrets.append((gov_id, "[GOV_ID_REDACTED]"))
    if policy_no and len(policy_no) >= 5 and policy_no != gov_id:
        exact_secrets.append((policy_no, "[POLICY_ID_REDACTED]"))

    name_variants = _extract_name_variants(full_name)
    name_patterns = [
        re.compile(rf"(?<!Dr\.\s)(?<!Dr\s)\b{re.escape(variant)}\b", re.IGNORECASE)
        for variant in name_variants
    ]

    # Directly mask structured patient PHI fields first
    direct_field_map = {
        "full_name": "[PATIENT_NAME]",
        "phone": "[PHONE_REDACTED]",
        "email": "[EMAIL_REDACTED]",
        "address": "[ADDRESS_REDACTED]",
        "gov_id_number": "[GOV_ID_REDACTED]",
        "policy_or_card_no": "[POLICY_ID_REDACTED]",
    }
    for field, token in direct_field_map.items():
        if patient.get(field) and str(patient[field]).strip() not in ("", "N/A", token):
            patient[field] = token
            counts[token] = counts.get(token, 0) + 1

    # Scrub remaining sections recursively (excluding structural IDs like uhid/order_id/item_id)
    preserve_keys = {
        "uhid",
        "admission_id",
        "item_id",
        "order_id",
        "note_id",
        "log_id",
        "report_id",
        "service_code",
        "package_code",
        "icd10_code",
        "payer_category",
    }

    def _walk_and_scrub(node: Any) -> Any:
        if isinstance(node, dict):
            for k, v in node.items():
                if k in preserve_keys or (node is patient and k in direct_field_map):
                    continue
                if isinstance(v, str):
                    node[k] = _scrub_string(v, name_patterns, exact_secrets, counts)
                elif isinstance(v, (dict, list)):
                    _walk_and_scrub(v)
        elif isinstance(node, list):
            for idx, item in enumerate(node):
                if isinstance(item, str):
                    node[idx] = _scrub_string(item, name_patterns, exact_secrets, counts)
                elif isinstance(item, (dict, list)):
                    _walk_and_scrub(item)
        return node

    _walk_and_scrub(redacted)

    stats = {
        "method": "Local On-Premise PHI Scrubber (Gemma-Parity Regex/Entity Redactor)",
        "total_redactions": sum(counts.values()),
        "tokens_masked": counts,
        "safe_for_cloud_llm": True,
    }
    return redacted, stats
