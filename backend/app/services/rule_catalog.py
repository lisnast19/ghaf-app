"""
rule_catalog.py — every scrub rule BIX Lite applies, with the reference (journal / standard / evidence)
it is derived from. Read-only; shown on the admin "Scrub rules & references" page.

Static descriptions are written here; rule-pack contents (frequency windows, MUE, NAS guidelines,
Excludes-1 count, symptom-integral pairs) are read live from the packs so the page never drifts.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from . import dental_rules, icd_excludes

_RULES_DIR = Path(__file__).resolve().parent / "claim_scrubber" / "rules"

# ── references ────────────────────────────────────────────────────────────────────────────────
REFERENCES = {
    "ECL_SCHEMA": {
        "title": "DHA eClaimLink — Claim.Submission transaction schema (DHPO)",
        "publisher": "Dubai Health Authority / eClaimLink",
        "kind": "Regulator technical standard",
        "url": "https://dhpo.eclaimlink.ae/",
        "note": "Header, Claim, Encounter, Diagnosis, Activity and Observation structure and required fields.",
    },
    "SN_03_2019": {
        "title": "eClaimLink Standards Notice SN 03/2019 — Claim Cycle Standardisation",
        "publisher": "Dubai Health Authority / Dubai Health Insurance Corporation",
        "kind": "Regulator standards notice",
        "url": None,
        "note": "Published 31 Jul 2019, effective 1 Oct 2019. Submission, resubmission, remittance and reconciliation timelines; overrides contracted timelines.",
    },
    "DHA_GC07_2021": {
        "title": "DHA General Circular GC 07/2021 — General updates and clarification of frequent misinterpretations of invoicing of medical services",
        "publisher": "Dubai Health Authority",
        "kind": "Regulator circular",
        "url": None,
        "note": "Dated 26 Oct 2021, effective on publication. Appendix 1: follow-up within 7 calendar days for the same condition, same practitioner, same network is free (nil co-insurance / deductible; consultation day = day 1); repeat consultations by the same doctor at the same provider on one day are follow-ups, not billable. Appendix 2: consultation on the day of, or within 10 days after, a minor procedure is not separately billable.",
    },
    "DHA_DSL_CODES": {
        "title": "DHA Dubai Special Services code list (DSL), version 16 Jun 2025",
        "publisher": "Dubai Health Authority",
        "kind": "Regulator code set",
        "url": None,
        "note": "Defines 9 / 10 / 11 (GP / Specialist / Consultant consultation); 9.01 / 10.01 / 11.01 = free follow-up of the same diagnosis within 7 days of the initial consultation; 9.02 / 10.02 / 11.02 = repeat visit in weeks 2, 3 and 4 from the initial consultation for the same illness (effective 15 Dec 2013).",
    },
    "DOH_ADJ_2025": {
        "title": "DoH Claims and Adjudication Rules V2025",
        "publisher": "Department of Health — Abu Dhabi",
        "kind": "Regulator rule book",
        "url": "https://www.doh.gov.ae/-/media/Feature/shafifya/Prices/Adjudication-Rules/DOH-Claims-and-Adjudication-Rules-V2025.ashx",
        "note": "Adjudication edits, frequency, MUE and bundling principles used across UAE payers.",
    },
    "DOH_CODING": {
        "title": "DoH Coding Manual (CS v2021)",
        "publisher": "Department of Health — Abu Dhabi",
        "kind": "Regulator coding manual",
        "url": "https://www.doh.gov.ae/-/media/Feature/shafifya/standards/coding/DOH-Coding-Manual-CSv2021.ashx",
        "note": "Consultation/E&M follow-up windows, modifier use, drug administration sequencing.",
    },
    "ECL_GC06_2018": {
        "title": "eClaimLink General Circular GC06/2018 — Denial Code Updates",
        "publisher": "DHA eClaimLink",
        "kind": "Regulator circular",
        "url": "https://www.isahd.ae/content/docs/eClaimLink%20General%20Circular%20GC06%202018%20-%20Denial%20Code%20Updates.pdf",
        "note": "Denial code list (CODE-, MNEC-, AUTH-, PRCE-, DUPL- …) each rule is mapped to.",
    },
    "CMS_ICD10CM_2026": {
        "title": "ICD-10-CM 2026 Tabular List — Excludes1 notes",
        "publisher": "CMS / CDC (NCHS)",
        "kind": "Classification standard",
        "url": "https://www.cms.gov/medicare/coding-billing/icd-10-codes",
        "note": "Official Excludes-1 notes, built into the scrubber's pair list.",
    },
    "ICD_GUIDE_IB5": {
        "title": "ICD-10-CM Official Guidelines for Coding and Reporting, section I.B.5",
        "publisher": "CMS / CDC (NCHS)",
        "kind": "Classification guideline",
        "url": "https://www.cms.gov/medicare/coding-billing/icd-10-codes",
        "note": "Signs and symptoms routinely associated with a disease are not coded separately.",
    },
    "DHA_DENTAL_2018": {
        "title": "DHA Dental Billing Rules — Dubai Standards of Care 2018",
        "publisher": "Dubai Health Authority",
        "kind": "Regulator billing rules",
        "url": None,
        "note": "Introduced for Enaya / Saada; now applied by all payers.",
    },
    "CDT_2018": {
        "title": "CDT 2018 code list (ADA)",
        "publisher": "American Dental Association (list provided by Ghaf team)",
        "kind": "Code set",
        "url": None,
        "note": "691 valid CDT 2018 dental procedure codes (claim_scrubber/rules/cdt_2018_codes.json).",
    },
    "NAS_GUIDE": {
        "title": "NAS Clinical Guidelines",
        "publisher": "NAS Administration Services",
        "kind": "TPA guideline",
        "url": "https://www.nas.ae/clinical-guidelines/",
        "note": "Applied only to NAS claims.",
    },
    "PAYER_HISTORY": {
        "title": "BIX Lite payer history analysis",
        "publisher": "BIX Lite (facility remittances)",
        "kind": "Evidence from data",
        "url": None,
        "note": "First remittance of original claims, last 180 days, per facility and payer. Thresholds shown with each rule.",
    },
    "GHAF_PRACTICE": {
        "title": "Ghaf RCM team practice",
        "publisher": "Ghaf Services",
        "kind": "Confirmed house rule",
        "url": None,
        "note": "Rules confirmed by the Ghaf team from payer correspondence and claim queries.",
    },
}

# ── rules: (code, area, severity, engine, what it checks, references, denial code) ──────────────
_R = lambda code, area, sev, engine, check, refs, denial="": {
    "code": code, "area": area, "severity": sev, "engine": engine, "check": check, "refs": refs, "denial_code": denial}

XML = "XML scrubber"
PRE = "Pre-submission queries"
STG = "Batch scrub (production history)"

RULES = [
    # claim cycle timelines (SN 03/2019)
    _R("SN_SUBMIT_BY_15TH", "Claim cycle timelines", "ALERT", "Facility alerts", "Claims must be submitted by the 15th of the month after the service month; late claims carry a 0.03% per day delay fee on the net claimed (3.7.1–3.7.2).", ["SN_03_2019"]),
    _R("SN_RESUBMIT_21D", "Claim cycle timelines", "BLOCK", "Prepare resubmission, recall queue, worklist & alerts", "Disputed or rejected claims must be resubmitted within 21 calendar days of the RA with denial reason; delay fee 0.03% per day (3.8.1, 3.8.4).", ["SN_03_2019"]),
    _R("SN_MAX_2_RESUBMISSIONS", "Claim cycle timelines", "BLOCK", "Prepare resubmission, recall queue & worklist", "A claim may be resubmitted at most twice, including the reconciliation (2nd resubmission); correction, internal complaint and reconciliation together cannot exceed this (3.8.2, 3.8.5).", ["SN_03_2019"]),
    _R("SN_RECONCILIATION", "Claim cycle timelines", "RULE", "Prepare resubmission & worklist", "Reconciliation resubmission only after all parties agree; tagged as type Reconciliation; the 21-day limit does not apply (3.10.1–3.10.4).", ["SN_03_2019"]),
    _R("SN_PAYER_RA_45D", "Claim cycle timelines", "ALERT", "Facility alerts & payer turnaround", "Payer must post the RA with payment reference within 45 days of submission (3.9.1); 7 days for emergency claims (3.9.4).", ["SN_03_2019"]),
    _R("SN_PAYER_RESUB_RA_30D", "Claim cycle timelines", "ALERT", "Payer turnaround", "Payer must post the RA for a resubmission within 30 days (3.9.2).", ["SN_03_2019"]),
    _R("SN_CLAIM_VALIDITY_24M", "Claim cycle timelines", "BLOCK", "Prepare resubmission & worklist", "A claim is valid for at most 24 months from submission (3.10.5, eClaimLink archiving policy).", ["SN_03_2019"]),
    _R("SN_DENIAL_CODE_REQUIRED", "Claim cycle timelines", "RULE", "Reference (payer obligation)", "Payers must give a published eClaimLink denial code and a claim-level comment for every full or partial denial (3.9.6, 6.1.3).", ["SN_03_2019", "ECL_GC06_2018"]),
    # file structure
    _R("ROOT_NAME", "File structure", "BLOCK", XML, "File root element must be Claim.Submission.", ["ECL_SCHEMA"]),
    _R("HEADER_MISSING", "File structure", "BLOCK", XML, "Submission Header must be present.", ["ECL_SCHEMA"]),
    _R("HEADER_REQUIRED", "File structure", "BLOCK", XML, "Header needs SenderID, ReceiverID, TransactionDate, RecordCount, DispositionFlag.", ["ECL_SCHEMA"]),
    _R("DATE_FORMAT", "File structure", "BLOCK", XML, "TransactionDate must be dd/MM/yyyy HH:mm.", ["ECL_SCHEMA"]),
    _R("RECORD_COUNT", "File structure", "BLOCK", XML, "Header RecordCount must equal the number of Claim elements.", ["ECL_SCHEMA"]),
    _R("CLAIM_REQUIRED", "Claim", "BLOCK", XML, "Each Claim needs ID, MemberID, PayerID, ProviderID, Gross, PatientShare, Net, Encounter.", ["ECL_SCHEMA"]),
    _R("MONEY_FORMAT", "Claim", "BLOCK", XML, "Gross, PatientShare and Net must be numeric.", ["ECL_SCHEMA"]),
    _R("NEGATIVE_AMOUNT", "Claim", "BLOCK", XML, "Claim amounts cannot be negative.", ["ECL_SCHEMA"]),
    _R("CLAIM_TOTALS", "Claim", "BLOCK", XML, "Gross − PatientShare must equal Net.", ["ECL_SCHEMA", "DOH_ADJ_2025"]),
    _R("LINE_TOTALS", "Claim", "BLOCK", XML, "Sum of Activity Net must equal Claim Net.", ["ECL_SCHEMA", "DOH_ADJ_2025"]),
    _R("DUPLICATE_CLAIM", "Claim", "BLOCK", XML, "Two claims with the same member, encounter and activity signature in one file.", ["ECL_GC06_2018"], "DUPL-001"),
    _R("ENCOUNTER_REQUIRED", "Encounter", "BLOCK", XML, "Encounter needs FacilityID, Type, Start (and End where applicable).", ["ECL_SCHEMA"]),
    _R("ENCOUNTER_DATE", "Encounter", "BLOCK", XML, "Encounter Start/End must be valid dates.", ["ECL_SCHEMA"]),
    _R("ENCOUNTER_ORDER", "Encounter", "BLOCK", XML, "Encounter Start cannot be after End.", ["ECL_SCHEMA"]),
    _R("DIAGNOSIS_MISSING", "Diagnosis", "BLOCK", XML, "At least one Diagnosis per claim.", ["ECL_SCHEMA"], "CODE-001"),
    _R("DIAGNOSIS_CODE_MISSING", "Diagnosis", "BLOCK", XML, "Every Diagnosis needs a Code.", ["ECL_SCHEMA"], "CODE-001"),
    _R("ACTIVITY_MISSING", "Activity", "BLOCK", XML, "At least one Activity per claim.", ["ECL_SCHEMA"]),
    _R("ACTIVITY_REQUIRED", "Activity", "BLOCK", XML, "Activity needs ID, Start, Type, Code, Quantity, Net, Clinician.", ["ECL_SCHEMA"]),
    _R("ACTIVITY_ID_DUPLICATE", "Activity", "BLOCK", XML, "Activity IDs must be unique within a claim.", ["ECL_SCHEMA"]),
    _R("ACTIVITY_DATE", "Activity", "BLOCK", XML, "Activity Start must be a valid date/time.", ["ECL_SCHEMA"]),
    _R("ACTIVITY_OUTSIDE_ENCOUNTER", "Activity", "WARN", XML, "Activity Start outside the Encounter period.", ["ECL_SCHEMA", "DOH_ADJ_2025"]),
    _R("OBSERVATION_INCOMPLETE", "Activity", "WARN", XML, "Observation should carry Type, Code and ValueType.", ["ECL_SCHEMA"]),
    # coding
    _R("ICD10_FORMAT", "Coding", "WARN", XML, "Diagnosis code doesn't look like a valid ICD-10-CM code.", ["CMS_ICD10CM_2026"], "CODE-001"),
    _R("PRINCIPAL_DX_MISSING", "Coding", "WARN", XML, "No diagnosis marked Principal (CSV/JSON type 'Primary' is treated as Principal).", ["ECL_SCHEMA", "DOH_CODING"], "CODE-001"),
    _R("CPT_CODE_FORMAT", "Coding", "WARN", XML, "Activity code is not a five-digit CPT or a configured UAE consultation code.", ["DOH_CODING"]),
    _R("CPT_ICD_LINK_MISSING", "Coding", "WARN", XML, "Procedure billed with no diagnosis to support it.", ["DOH_ADJ_2025"], "CODE-006"),
    _R("MODIFIER_REVIEW", "Coding", "WARN", XML, "CPT has no modifier; review modifier requirements (comparison warning, not a universal UAE rule).", ["DOH_CODING"]),
    _R("ICD_EXCLUDES1", "Coding", "WARN", XML, "Two diagnoses that ICD-10-CM says are not coded together.", ["CMS_ICD10CM_2026"], "CODE-001"),
    _R("SYMPTOM_INTEGRAL", "Coding", "WARN", XML, "Symptom coded alongside the diagnosis that explains it (payer practice pairs).", ["ICD_GUIDE_IB5", "GHAF_PRACTICE"], "CODE-001"),
    # visits / frequency
    _R("CONSULT_FOLLOWUP_7D", "Visits & frequency", "WARN", XML, "Consultation 9 / 10 / 11 within 7 days of a previous one for the same member — bill as free follow-up (9.01 / 10.01 / 11.01, net 0). Not raised when the line is already .01.", ["DHA_GC07_2021", "DHA_DSL_CODES", "GHAF_PRACTICE"], "CODE-016"),
    _R("CONSULT_REVIEW_8_28D", "Visits & frequency", "WARN", XML, "Consultation 8–28 days after the previous one — repeat consultation (9.02 / 10.02 / 11.02). Not raised when the line is already .02.", ["DHA_DSL_CODES", "GHAF_PRACTICE"], "CODE-016"),
    _R("FOLLOWUP_NET_NOT_ZERO", "Visits & frequency", "WARN", XML, "Follow-up line 9.01 / 10.01 / 11.01 billed with net > 0 — free follow-up must be net 0.", ["DHA_GC07_2021", "DHA_DSL_CODES"], "CODE-016"),
    _R("CONSULT_SAME_DAY_DUPLICATE", "Visits & frequency", "WARN", XML, "More than one consultation for the same member on one day.", ["DHA_GC07_2021", "DOH_ADJ_2025"], "DUPL-002"),
    _R("SERVICE_FREQUENCY_LIMIT", "Visits & frequency", "WARN", XML, "Service repeated inside a configured frequency window (see rule pack below).", ["DOH_ADJ_2025", "DOH_CODING"], "CODE-016"),
    _R("FREQUENCY_MODIFIER_REQUIRED", "Visits & frequency", "WARN", XML, "Service in a follow-up window that needs a modifier (see rule pack below).", ["DOH_CODING"], "CODE-016"),
    _R("OVERLAPPING_ACTIVITY", "Visits & frequency", "WARN", XML, "Same member, same code, same date/time in two claims.", ["ECL_GC06_2018"], "DUPL-002"),
    _R("BUNDLING_SEQUENCE_REVIEW", "Bundling", "WARN", XML, "Several drug administration codes on one claim — review initial/sequential/add-on sequencing.", ["DOH_CODING", "DOH_ADJ_2025"], "MNEC-007"),
    _R("MUE_EXCEEDED", "Bundling", "WARN", XML, "Units above the configured medically unlikely edit (see rule pack below).", ["DOH_ADJ_2025"], "CODE-016"),
    _R("MUTUALLY_EXCLUSIVE_CODES", "Bundling", "WARN", XML, "Mutually exclusive services on one claim (pairs from the rule pack).", ["DOH_ADJ_2025"], "MNEC-007"),
    # batch scrub against production
    _R("FREE_FOLLOWUP", "Visits & frequency", "ERROR", STG, "ERROR: same patient at the same facility within 7 days of a previous visit (any status) — bill as free follow-up .01 (not raised when already .01). WARN (repeat tier): consultation 9 / 10 / 11 8–28 days after a visit with the same doctor (licence normalised, SHAHPL… = HPL…) or same specialty for the same condition — bill as repeat .02 (not raised when already .02).", ["DHA_GC07_2021", "DHA_DSL_CODES", "GHAF_PRACTICE"], "CODE-016"),
    _R("DUPLICATE", "Claim", "ERROR", STG, "Same patient, facility, date and service already submitted.", ["ECL_GC06_2018"], "DUPL-001"),
    _R("MISSING_AUTH", "Approval", "WARN", STG, "Charged CPT/HCPCS line with no authorization ID.", ["ECL_SCHEMA", "ECL_GC06_2018"], "AUTH-001"),
    _R("EXCLUDE_1 / EXCLUDE_2", "Coverage", "ERROR / WARN", STG, "Code on the DHA exclusion list (absolute / conditional).", ["ECL_GC06_2018"]),
    # pre-submission queries
    _R("EXCLUDES1", "Coding", "QUERY", PRE, "Excludes-1 pair: corrected in the ERP, or doctor to choose.", ["CMS_ICD10CM_2026"], "CODE-001"),
    _R("SYMPTOM_INTEGRAL", "Coding", "QUERY", PRE, "Symptom code with definitive diagnosis.", ["ICD_GUIDE_IB5", "GHAF_PRACTICE"], "CODE-001"),
    _R("DRUG_SEQUENCE / DRUG_PARENT", "Bundling", "QUERY", PRE, "Drug administration sequencing / wrong parent code — corrected, or correction required when an approval blocks it.", ["DOH_CODING"], "MNEC-007"),
    _R("FOLLOWUP / REPEAT / CONSULT_SAMEDAY", "Visits & frequency", "QUERY", PRE, "Consultation codes 9 / 10 / 11: 7-day free follow-up (.01, net 0) and 8–28-day repeat (.02), same doctor (licence normalised) or same specialty, same condition.", ["DHA_GC07_2021", "DHA_DSL_CODES", "GHAF_PRACTICE"], "CODE-016"),
    _R("FOLLOWUP_NET / FOLLOWUP_NO_VISIT", "Visits & frequency", "QUERY", PRE, "Follow-up 9.01 / 10.01 / 11.01 billed with net > 0 (FOLLOWUP_NET, to admin), or with no earlier visit.", ["DHA_GC07_2021", "DHA_DSL_CODES", "GHAF_PRACTICE"]),
    _R("APPROVAL_MISSING / APPROVAL_PARTIAL", "Approval", "QUERY", PRE, "No approval, or approval not covering all services, where this payer denies such lines (≥5 lines, ≥30% denied).", ["PAYER_HISTORY", "ECL_GC06_2018"], "AUTH-001"),
    _R("APPROVAL_DENTAL", "Approval", "QUERY", PRE, "Every CDT code (D####) needs prior approval. Dental diagnoses K00–K14 need approval only when a dentist is involved (CDT code on the claim or a dental clinician); K20+ is not dental.", ["DHA_DENTAL_2018", "GHAF_PRACTICE"], "AUTH-001"),
    _R("APPROVAL_DX / APPROVAL_MATERNITY", "Approval", "QUERY", PRE, "Diagnosis categories this payer wants approved, e.g. maternity, refraction (≥20% denied).", ["PAYER_HISTORY"], "AUTH-001"),
    _R("CDT_UNKNOWN", "Dental", "QUERY", PRE, "D#### code not in the CDT 2018 code list — not a recognised CDT code.", ["CDT_2018"]),
    _R("DENTAL_RULE", "Dental", "QUERY", PRE, "Same-day combinations and frequency limits from the DHA dental rules (listed below).", ["DHA_DENTAL_2018"]),
    _R("TEST_PAIR", "Medical necessity", "QUERY", PRE, "Test pairs this payer denies together.", ["PAYER_HISTORY"], "MNEC-003"),
    _R("REPEAT_TEST", "Medical necessity", "QUERY", PRE, "Test repeated within its interval (e.g. HbA1c 90 days, lipid 180 days).", ["PAYER_HISTORY", "NAS_GUIDE"], "MNEC-003"),
    _R("DX_FIT", "Medical necessity", "QUERY", PRE, "Payer denies this code with this diagnosis category but pays it with others (≥8 lines, ≥60% denied; others ≥60% paid).", ["PAYER_HISTORY", "ECL_GC06_2018"], "MNEC-003/004"),
    _R("ADMIN_NECESSITY", "Medical necessity", "QUERY", PRE, "Injection / IV where oral would do — 'alternative service should have been utilized'.", ["ECL_GC06_2018", "PAYER_HISTORY"], "MNEC-006"),
    _R("PAYER_POLICY / DENIAL_RISK", "Medical necessity", "INFO", PRE, "Code this payer denies whatever the diagnosis (≥20 lines, ≥75%) — payer policy, info only, not sent to the facility.", ["PAYER_HISTORY"]),
    _R("Panel components (Oman)", "Medical necessity", "QUERY", PRE, "Oman Insurance denies lipid panel 80061 but pays TG / HDL — order components; never split a completed panel.", ["PAYER_HISTORY", "GHAF_PRACTICE"], "MNEC-004"),
    _R("DX_DOC", "Documentation", "QUERY", PRE, "Diagnosis not supported by the case sheet (clinician query).", ["GHAF_PRACTICE"], "CODE-001"),
    _R("DX_EXCLUDED", "Coverage", "QUERY", PRE, "Diagnosis excluded under the policy's schedule of benefits.", ["PAYER_HISTORY"]),
    _R("MATERNITY_CODING", "Coding", "QUERY", PRE, "Pregnancy diagnosis coding checks.", ["CMS_ICD10CM_2026"]),
    _R("OPTICAL_BENEFIT", "Coverage", "QUERY", PRE, "Optical services needing benefit / approval.", ["PAYER_HISTORY"]),
    _R("GP_REFERRAL", "Approval", "QUERY", PRE, "Specialist visit without GP referral (NAS).", ["NAS_GUIDE", "PAYER_HISTORY"]),
    _R("DUPLICATE_LINES", "Claim", "QUERY", PRE, "Quantity / duplicate / price billing mistakes, incl. ERP-doubled invoices.", ["GHAF_PRACTICE"], "DUPL-001"),
    _R("LICENCE", "Clinician", "QUERY", PRE, "Clinician whose lines this payer denies for licence (≥3 lines, ≥30%, 90 days).", ["PAYER_HISTORY"]),
]


# the scrubber's own finding → denial-code map (claim_scrubber/scrubber.py, risk score); other XML rules are unmapped
_XML_DENIAL = {
    "DIAGNOSIS_MISSING": "CODE-001", "PRINCIPAL_DX_MISSING": "CODE-001",
    "CPT_ICD_LINK_MISSING": "CODE-006", "CPT_CODE_FORMAT": "CODE-006",
    "MODIFIER_REVIEW": "CODE-016", "FREQUENCY_MODIFIER_REQUIRED": "CODE-016",
    "SERVICE_FREQUENCY_LIMIT": "CODE-016", "MUE_EXCEEDED": "CODE-016",
    "BUNDLING_SEQUENCE_REVIEW": "MNEC-007", "MUTUALLY_EXCLUSIVE_CODES": "MNEC-007",
    "ICD_EXCLUDES1": "CODE-001", "SYMPTOM_INTEGRAL": "CODE-001",
}
for _r in RULES:
    if _r["engine"] == XML:
        _r["denial_code"] = _XML_DENIAL.get(_r["code"], "")
    elif _r["denial_code"].startswith(("AUTH-", "DUPL-")):
        _r["denial_code"] = ""          # not confirmed against a payer mapping


def _load(name):
    try:
        return json.loads((_RULES_DIR / name).read_text())
    except Exception:
        return {}


@lru_cache(maxsize=1)
def catalog() -> dict:
    uae = _load("uae-rules-2025.json")
    nas = _load("nas-guidelines-2026.json")
    exc = _load("icd10cm_2026_excludes1.json")
    packs = [
        {"id": uae.get("rule_pack_id"), "title": "UAE rule pack", "version": uae.get("version"),
         "effective_from": uae.get("effective_from"), "authority": uae.get("authority"), "sources": uae.get("sources", []),
         "frequency": uae.get("service_frequency", []), "mue": (uae.get("bundling") or {}).get("mue", []),
         "mutually_exclusive": (uae.get("bundling") or {}).get("mutually_exclusive_pairs", [])},
        {"id": nas.get("guideline_pack_id"), "title": "NAS guideline pack", "version": nas.get("version"),
         "scope": nas.get("scope"), "sources": [(nas.get("source") or {}).get("url")],
         "captured": (nas.get("source") or {}).get("captured"),
         "rules": [{"rule_id": r.get("rule_id"), "severity": r.get("severity"), "guideline": r.get("guideline"),
                    "message": r.get("message")} for r in nas.get("rules", [])]},
        {"id": "ICD10CM-2026-EXCLUDES1", "title": "ICD-10-CM Excludes-1 list", "source_note": exc.get("source"),
         "count": exc.get("count") or len(exc.get("rules", []))},
    ]
    practice = [{"with": list(w), "drop": d, "why": why} for w, d, why in icd_excludes.PRACTICE_PAIRS]
    dental = [{"codes": sorted(c), "max": n, "days": d, "rule": t} for c, n, d, t in dental_rules.FREQUENCY]
    return {"references": REFERENCES, "rules": RULES, "packs": packs,
            "symptom_pairs": practice, "dental_frequency": dental,
            "dental_max_fillings_per_day": dental_rules.MAX_FILLINGS_PER_DAY}
