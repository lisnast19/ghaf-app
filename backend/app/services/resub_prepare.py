"""
resub_prepare.py — "Prepare Resubmission" for one denied DHPO claim.

  build_draft(db, facility_id, claim_id)      facts + suggestions + requirements for the denied lines
  build_xml(draft, edits, disposition)        Claim.Submission XML (+ internal JSON mirror) and validation errors
  upload(db, facility_id, xml, ...)           DHPO UploadTransaction with the facility's stored credentials
  ensure_table(engine)                        CREATE TABLE IF NOT EXISTS resub_submissions (short, own transaction)

Facts (what is in the claim / RA / member history) and suggestions (resubmission type, justification
wording) are kept in separate keys so the UI can show them apart.

DHA rules used (see the research notes, most are UNVERIFIED against the official XSD):
  * Claim.ID and every Activity.ID stay the same as in the latest version of the claim.
  * Resubmission{Type, Comment, Attachment?} sits after the last Activity.
  * Net = Gross - PatientShare, 2 dp; dates dd/MM/yyyy HH:mm; exactly one Principal diagnosis.
  * DispositionFlag "TEST SUBMISSION" (default) or "PRODUCTION".

Passwords are never logged, returned or stored here.
"""
from __future__ import annotations

import base64
import binascii
import copy
import json
import logging
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from lxml import etree
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..models import Facility
from .denial_codes import denial_description
from .presub_queries import CONSULT_CHECKED, REPEAT_TESTS
from .resub_worklist import RECON_AGREED, _valid_until

log = logging.getLogger(__name__)

DUBAI = ZoneInfo("Asia/Dubai")
# No TEST file exists in raw_xml_store (all DHPO files seen are PRODUCTION) — "TEST" is the
# eClaimLink value per the research notes; NEEDS CONFIRMATION against the XSD before the first upload.
TEST = "TEST"
PRODUCTION = "PRODUCTION"
DISPOSITIONS = (TEST, PRODUCTION)
# seen in accepted PRODUCTION files in raw_xml_store: internal complaint (most), correction, reconciliation (116 in a
# 20k-row sample), legacy (1). Suggestions only ever use internal complaint / correction.
RESUB_TYPES = ("internal complaint", "correction", "reconciliation", "legacy")
COMMENT_WARN = 1268              # longest Comment seen in accepted files
ATTACHMENT_WARN_B64 = 1_350_000  # largest accepted Attachment (base64 chars)
FOLLOWUP_DAYS = 7          # .01 free follow-up (confirmed rule)
REPEAT_VISIT_DAYS = 28     # .02 repeat consultation 8–28 days (confirmed rule)
COMMENT_MAX = 2000         # DHA limit unknown — keep comments short
ATTACHMENT_MAX_BYTES = 5 * 1024 * 1024
CONSULT_CPT = re.compile(r"^992(0[1-5]|1[1-5])$")
DT_RE = re.compile(r"^\d{2}/\d{2}/\d{4} \d{2}:\d{2}$")
DEC_RE = re.compile(r"^-?\d+(\.\d{1,3})?$")   # accepted DHPO files carry up to 3 dp (e.g. 0.000)

# Claim child order per the DHPO schema
CLAIM_ORDER = ["ID", "IDPayer", "MemberID", "PayerID", "ProviderID", "EmiratesIDNumber",
               "Gross", "PatientShare", "Net", "Encounter", "Diagnosis", "Activity", "Resubmission", "Contract"]
ACTIVITY_ORDER = ["ID", "Start", "Type", "Code", "Quantity", "Net", "OrderingClinician", "Clinician",
                  "PriorAuthorizationID", "Observation"]


# ---------------------------------------------------------------------------
# Denial families → suggestion + requirements
# ---------------------------------------------------------------------------
FAMILIES: Dict[str, Dict[str, Any]] = {
    "MNEC": {"type": "internal complaint",
             "tpl": "{code} ({desc}) was medically necessary for the principal diagnosis {pdx} ({pdx_desc}). "
                    "Clinical indication: [symptoms / findings / reason for order on {dos}]. Clinical notes attached.",
             "reqs": ["clinical_notes", "clinical_evaluation"]},
    "AUTH": {"type": "correction",
             "tpl": "Prior approval {auth} obtained for {code} and valid on {dos}. Approval number added to the activity; "
                    "approval attached.",
             "reqs": ["approval_number", "approval_letter"]},
    "CODE": {"type": "correction",
             "tpl": "Coding corrected: {code} → [corrected code / modifier] as per the clinical documentation "
                    "(principal diagnosis {pdx}). Notes attached.",
             "reqs": ["corrected_code", "clinical_notes"]},
    "PRCE": {"type": "internal complaint",
             "tpl": "{code} billed as per the agreed contract price (AED {net}). Price list / contract reference attached.",
             "reqs": ["contract_price"]},
    "CLAI": {"type": "internal complaint",
             "tpl": "Claim details verified against the contract and documentation: [explain the claim data issue]. "
                    "Supporting document attached.",
             "reqs": ["attachment"]},
    "DUPL": {"type": "internal complaint",
             "tpl": "{code} on {dos} is not a duplicate: it is a distinct service [different time / side / dose / "
                    "separate encounter]. See attached notes.",
             "reqs": ["previous_history", "clinical_notes"]},
    "ELIG": {"type": "internal complaint",
             "tpl": "Patient was eligible on the date of service {dos} (member ID verified). Eligibility proof / card copy attached.",
             "reqs": ["eligibility_proof"]},
    "COPY": {"type": "correction",
             "tpl": "Patient share for {code} corrected as per the policy benefit table.",
             "reqs": ["attachment"]},
    "TIME": {"type": "internal complaint",
             "tpl": "Claim was submitted within the allowed time limit: [original submission date / reason for delay]. "
                    "Proof attached.",
             "reqs": ["attachment"]},
}
DEFAULT_FAMILY = {"type": "internal complaint",
                  "tpl": "Request reconsideration of {code} ({desc}): [justification]. Supporting documents attached.",
                  "reqs": ["attachment"]}

REQ_LABELS = {
    "clinical_notes": ("attachment", "Case sheet / clinical notes (PDF attachment)"),
    "clinical_evaluation": ("clinical_evaluation", "Clinical evaluation supporting the service (symptoms, findings)"),
    "approval_number": ("approval", "Prior approval number on the activity"),
    "approval_letter": ("attachment", "Approval letter / e-auth copy"),
    "corrected_code": ("correction", "Corrected code / modifier"),
    "contract_price": ("attachment", "Contract price list reference"),
    "previous_history": ("previous_history", "Previous history / earlier service details"),
    "eligibility_proof": ("attachment", "Eligibility proof / insurance card copy"),
    "attachment": ("attachment", "Supporting document"),
    "lab_report": ("attachment", "Lab report(s): previous and current results"),
    "additional_visit": ("additional_visit", "Additional visit justification (new complaint / diagnosis)"),
}


from .denial_category import classify

# denial-code family -> fallback category; category (from the payer reason) -> justification family
CODE_CATEGORY = {"MNEC": "Medical necessity", "AUTH": "Prior approval", "CODE": "Coding", "PRCE": "Contract & tariff",
                 "CLAI": "Other", "DUPL": "Duplicate", "ELIG": "Coverage / eligibility", "BENX": "Coverage / eligibility",
                 "NCOV": "Coverage / eligibility", "COPY": "Patient share", "TIME": "Documents"}
CATEGORY_FAMILY = {"Medical necessity": "MNEC", "Medical necessity — frequency": "MNEC",
                   "Medical necessity — alternative service": "MNEC", "Prior approval": "AUTH", "Coding": "CODE",
                   "Coding — panel composition": "CODE", "Contract & tariff": "PRCE", "Duplicate": "DUPL",
                   "Duplicate — already paid": "DUPL", "Coverage / eligibility": "ELIG", "Patient share": "COPY",
                   "Late submission": "TIME", "Documents": "TIME", "Clinician licence": "CLAI", "GP referral": "CLAI"}


def _family(code: str) -> str:
    return (code or "").split("-")[0].strip().upper()


def _d(v) -> Decimal:
    try:
        return Decimal(str(v if v not in (None, "") else 0)).quantize(Decimal("0.01"))
    except InvalidOperation:
        return Decimal("0.00")


def _fmt_dt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        if v.tzinfo:
            v = v.astimezone(DUBAI)
        return v.strftime("%d/%m/%Y %H:%M")
    if isinstance(v, date):
        return v.strftime("%d/%m/%Y 00:00")
    return str(v)


def _parse_dt(s: str) -> Optional[datetime]:
    for f in ("%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime((s or "").strip(), f)
        except ValueError:
            pass
    return None


def _txt(el, tag) -> str:
    x = el.find(tag) if el is not None else None
    return (x.text or "").strip() if x is not None and x.text else ""


def mask(v: Optional[str]) -> str:
    v = v or ""
    return v if len(v) <= 4 else "•" * (len(v) - 4) + v[-4:]


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
_CLAIM_SQL = text("""
SELECT * FROM claims WHERE facility_id = :f AND claim_id = :c
ORDER BY submission_date DESC NULLS LAST, id DESC LIMIT 1""")
_ACT_SQL = text("SELECT * FROM claim_activities WHERE claim_id_fk = :id ORDER BY sequence_no, id")
_DX_SQL = text("SELECT * FROM claim_diagnoses WHERE claim_id_fk = :id ORDER BY sequence_no, id")
_RA_SQL = text("""
SELECT * FROM remittances WHERE facility_id = :f AND claim_id = :c
ORDER BY remittance_date DESC NULLS LAST, id DESC LIMIT 1""")
_RAD_SQL = text("SELECT * FROM remittance_activity_details WHERE remittance_id_fk = :id ORDER BY id")
_HIST_SQL = text("""
SELECT DISTINCT ON (c.claim_id) c.id, c.claim_id, c.encounter_start, c.clinician_id,
       (SELECT x.diagnosis_code FROM claim_diagnoses x WHERE x.claim_id_fk = c.id
        ORDER BY (lower(x.diagnosis_type) IN ('primary', 'principal')) DESC, x.sequence_no, x.id LIMIT 1) AS pdx
FROM claims c
WHERE c.facility_id = :f AND c.member_id = :m AND c.claim_id <> :c
  AND c.encounter_start >= :d0 AND c.encounter_start < :d1
ORDER BY c.claim_id, c.submission_date DESC NULLS LAST, c.id DESC""")
# Earlier billing of the same codes for this member (MNEC frequency check, 12 months back).
FREQ_LOOKBACK_DAYS = 365
_FREQ_SQL = text("""
SELECT DISTINCT ON (c.claim_id, upper(a.activity_code)) upper(a.activity_code) AS code, c.claim_id, c.encounter_start,
       a.net, ra.paid_amount AS paid, ra.denial_code, ra.remittance_date
FROM claims c JOIN claim_activities a ON a.claim_id_fk = c.id
LEFT JOIN LATERAL (
    SELECT d.paid_amount, d.denial_code, r.remittance_date
    FROM remittances r JOIN remittance_activity_details d ON d.remittance_id_fk = r.id
    WHERE r.facility_id = c.facility_id AND r.claim_id = c.claim_id AND d.activity_id = a.activity_id
    ORDER BY r.remittance_date DESC NULLS LAST, r.id DESC LIMIT 1) ra ON true
WHERE c.facility_id = :f AND c.member_id = :m AND c.claim_id <> :c
  AND c.encounter_start >= :d0 AND c.encounter_start < :d1 AND upper(a.activity_code) = ANY(:codes)
ORDER BY c.claim_id, upper(a.activity_code), c.submission_date DESC NULLS LAST, c.id DESC""")


def _ra_status(r) -> str:
    if r.remittance_date is None and r.paid is None:
        return "no RA"
    paid = float(r.paid or 0)
    if paid > 0 and not (r.denial_code or "").strip():
        return "paid"
    if paid > 0:
        return f"part paid ({r.denial_code})"
    return f"denied ({r.denial_code})" if (r.denial_code or "").strip() else "not paid"


def _test_frequency(db: Session, facility_id: int, c, codes: List[str]) -> Dict[str, Dict[str, Any]]:
    """code -> {count, last_date, days_since, prior:[{date, days_before, claim_id}]} for the last 12 months."""
    codes = sorted({x.upper() for x in codes if x})
    if not codes or not c.member_id or not c.encounter_start:
        return {}
    d1 = c.encounter_start
    rows = db.execute(_FREQ_SQL, {"f": facility_id, "m": c.member_id, "c": c.claim_id, "codes": codes,
                                  "d0": d1 - timedelta(days=FREQ_LOOKBACK_DAYS), "d1": d1}).all()
    out: Dict[str, Dict[str, Any]] = {k: {"count": 0, "prior": []} for k in codes}
    for r in sorted(rows, key=lambda r: r.encounter_start, reverse=True):
        out[r.code]["prior"].append({"date": r.encounter_start.astimezone(DUBAI).date().isoformat(),
                                     "days_before": (d1 - r.encounter_start).days, "claim_id": r.claim_id,
                                     "status": _ra_status(r), "paid": float(r.paid or 0)})
    for v in out.values():
        v["paid_count"] = sum(1 for p in v["prior"] if p["status"] == "paid" or p["status"].startswith("part"))
        v["last_status"] = v["prior"][0]["status"] if v["prior"] else None
        last_paid = next((p for p in v["prior"] if p["paid"] > 0), None)
        v["last_paid_date"] = last_paid["date"] if last_paid else None
        v["last_paid_days"] = last_paid["days_before"] if last_paid else None
        v["count"] = len(v["prior"])
        v["last_date"] = v["prior"][0]["date"] if v["prior"] else None
        v["days_since"] = v["prior"][0]["days_before"] if v["prior"] else None
    return out


_HIST_ACT_SQL = text("""
SELECT a.claim_id_fk, a.activity_code, a.activity_start, a.net FROM claim_activities a
WHERE a.claim_id_fk = ANY(:ids)""")


def _original_claim_xml(db: Session, raw_xml_id: Optional[int], claim_id: str):
    """(claim element, header dict) from the stored original file, or (None, {})."""
    if not raw_xml_id:
        return None, {}
    row = db.execute(text("SELECT xml_type, xml_content FROM raw_xml_store WHERE id = :i"), {"i": raw_xml_id}).first()
    if not row or not row.xml_content:
        return None, {}
    if "Riayati" in (row.xml_type or ""):
        try:
            ent = json.loads(row.xml_content).get("Entity") or {}
        except ValueError:
            return None, {}
        header = dict(ent.get("Header") or {}, _format="riayati_json")
        for cl in ent.get("Claim") or []:
            if str(cl.get("ID", "")).strip() == claim_id:
                return copy.deepcopy(cl), header
        return None, header
    try:
        parser = etree.XMLParser(huge_tree=True, remove_blank_text=True, resolve_entities=False, no_network=True)
        root = etree.fromstring(row.xml_content.encode("utf-8"), parser)
    except etree.XMLSyntaxError:
        log.warning("resub_prepare: raw_xml %s does not parse", raw_xml_id)
        return None, {}
    hdr = root.find("Header")
    header = {t: _txt(hdr, t) for t in ("SenderID", "ReceiverID", "TransactionDate", "RecordCount", "DispositionFlag")}
    # keep the original root's namespaces / attributes exactly (several variants exist in real files)
    header["_root_nsmap"] = {k: v for k, v in root.nsmap.items()}
    header["_root_attrib"] = dict(root.attrib)
    for cl in root.findall("Claim"):
        if _txt(cl, "ID") == claim_id:
            return copy.deepcopy(cl), header
    return None, header


def _is_principal(t) -> bool:
    """DB stores the principal diagnosis as 'Primary'; eClaimLink XML uses 'Principal'. Accept either."""
    return str(t or "").strip().lower() in ("principal", "primary")


def _dx_type(t):
    """Map the DB diagnosis_type to the eClaimLink Diagnosis/Type value (Primary -> Principal)."""
    return "Principal" if _is_principal(t) else t


def _claim_from_tables(c, acts, dxs) -> etree._Element:
    cl = etree.Element("Claim")

    def sub(p, tag, val):
        if val not in (None, ""):
            etree.SubElement(p, tag).text = str(val)

    sub(cl, "ID", c.claim_id)
    sub(cl, "IDPayer", c.claim_id_payer)
    sub(cl, "MemberID", c.member_id)
    sub(cl, "PayerID", c.payer_id)
    sub(cl, "ProviderID", c.sender_id)
    sub(cl, "EmiratesIDNumber", c.emirates_id)
    sub(cl, "Gross", _d(c.gross_amount))
    sub(cl, "PatientShare", _d(c.patient_share_total))
    sub(cl, "Net", _d(c.net_amount))
    enc = etree.SubElement(cl, "Encounter")
    sub(enc, "FacilityID", c.sender_id)
    sub(enc, "Type", c.encounter_type)
    sub(enc, "PatientID", c.patient_id)
    sub(enc, "Start", _fmt_dt(c.encounter_start))
    sub(enc, "End", _fmt_dt(c.encounter_end))
    sub(enc, "StartType", c.start_type)
    sub(enc, "EndType", c.end_type)
    sub(enc, "TransferSource", c.transfer_source)
    for d in dxs:
        dx = etree.SubElement(cl, "Diagnosis")
        sub(dx, "Type", _dx_type(d.diagnosis_type))
        sub(dx, "Code", d.diagnosis_code)
    enc_start = c.encounter_start.astimezone(DUBAI) if c.encounter_start else None
    for a in acts:
        ac = etree.SubElement(cl, "Activity")
        sub(ac, "ID", a.activity_id)
        st = a.activity_start
        if st and enc_start and enc_start.date() == st:
            sub(ac, "Start", _fmt_dt(enc_start))
        else:
            sub(ac, "Start", _fmt_dt(st))
        sub(ac, "Type", a.activity_type)
        sub(ac, "Code", a.activity_code)
        sub(ac, "Quantity", a.quantity if a.quantity is not None else 1)
        sub(ac, "Net", _d(a.net))
        sub(ac, "Clinician", a.clinician_id or c.clinician_id)
        sub(ac, "PriorAuthorizationID", a.authorization_id)
    return cl


def _riayati_to_el(cl: Dict[str, Any]) -> etree._Element:
    """Riayati JSON claim → lxml Claim (facts only; output stays JSON)."""
    el = etree.Element("Claim")

    def sub(p, tag, v):
        if v not in (None, "") and not isinstance(v, (list, dict)):
            etree.SubElement(p, tag).text = str(v).strip()
    for k in ("ID", "IDPayer", "MemberID", "PayerID", "ProviderID"):
        sub(el, k, cl.get(k))
    sub(el, "EmiratesIDNumber", cl.get("NationalIDNumber") or cl.get("EmiratesIDNumber"))
    for k in ("Gross", "PatientShare", "Net"):
        sub(el, k, cl.get(k))
    enc = etree.SubElement(el, "Encounter")
    for k, v in (cl.get("Encounter") or {}).items():
        sub(enc, k, v)
    for d in cl.get("Diagnosis") or []:
        dx = etree.SubElement(el, "Diagnosis"); sub(dx, "Type", d.get("Type")); sub(dx, "Code", d.get("Code"))
    for a in cl.get("Activity") or []:
        ae = etree.SubElement(el, "Activity")
        for k in ("ID", "Start", "Type", "Code", "Quantity", "Net", "Clinician", "PriorAuthorizationID"):
            sub(ae, k, a.get(k))
    return el


def _build_riayati(draft, edits, disposition, now) -> Dict[str, Any]:
    cl = copy.deepcopy(draft["_riayati_claim"])
    errors: List[str] = []
    ce = edits.get("claim") or {}
    for key, tag in (("id_payer", "IDPayer"), ("member_id", "MemberID"), ("emirates_id", "NationalIDNumber")):
        if ce.get(key) not in (None, ""):
            cl[tag] = str(ce[key]).strip()
    acts = {str(a.get("ID")): a for a in cl.get("Activity") or []}
    for aid, ae in (edits.get("activities") or {}).items():
        a = acts.get(str(aid))
        if a is None:
            errors.append(f"Activity {aid} is not in the latest version of the claim"); continue
        for key, tag, num in (("code", "Code", False), ("quantity", "Quantity", True), ("net", "Net", True),
                              ("prior_authorization_id", "PriorAuthorizationID", False), ("clinician", "Clinician", False)):
            if ae.get(key) not in (None, ""):
                a[tag] = float(ae[key]) if num else str(ae[key]).strip()
    if edits.get("diagnoses"):
        cl["Diagnosis"] = [{"Type": d.get("type"), "Code": str(d.get("code", "")).upper(), "DxInfo": []} for d in edits["diagnoses"]]
    for key, tag in (("gross", "Gross"), ("patient_share", "PatientShare"), ("net", "Net")):
        if ce.get(key) not in (None, ""):
            cl[tag] = float(_d(ce[key]))
    rtype = (edits.get("resubmission_type") or draft["suggestion"]["resubmission_type"]).strip().lower()
    comment = (edits.get("comment") if edits.get("comment") is not None else draft["suggestion"]["comment"]).strip()
    cl["Type"] = "Resubmission"
    cl["Resubmission"] = {"Comment": comment, "Type": rtype}
    errors.extend(sn_errors(draft, rtype))
    if edits.get("attachment_b64"):
        cl["Resubmission"]["Attachment"] = edits["attachment_b64"].split(",", 1)[-1]
    hdr = {k: v for k, v in draft["_header"].items() if not k.startswith("_")}
    hdr.update(TransactionDate=now.strftime("%d/%m/%Y %H:%M"), RecordCount=1, DispositionFlag=disposition)
    g, ps, n = _d(cl.get("Gross")), _d(cl.get("PatientShare")), _d(cl.get("Net"))
    if rtype not in RESUB_TYPES:
        errors.append(f"Resubmission/Type must be one of {RESUB_TYPES}")
    if not comment:
        errors.append("Resubmission/Comment required")
    if not cl.get("Diagnosis") or sum(1 for d in cl["Diagnosis"] if _is_principal(d.get("Type"))) != 1:
        errors.append("exactly one Principal diagnosis required")
    anet = sum((_d(a.get("Net")) for a in cl.get("Activity") or []), Decimal("0.00"))
    if abs(anet - n) > Decimal("0.05"):
        errors.append(f"sum of Activity Net {anet} ≠ Claim Net {n}")
    warn = ["Riayati JSON — download only; no Riayati upload is implemented (no public spec/endpoint)."]
    if abs(g - ps - n) > Decimal("0.01"):   # Riayati originals often carry discounts — informational only
        warn.append(f"Gross {g} − PatientShare {ps} ≠ Net {n} (as in the original) — check")
    payload = {"Entity": {"Header": hdr, "Claim": [cl]}}
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", draft["facts"]["claim_id"])
    return {"xml": json.dumps(payload, ensure_ascii=False, indent=2), "json": payload, "errors": errors,
            "warnings": warn, "format": "riayati_json",
            "filename": f"RIAYATI_RESUB_{safe}_{now.strftime('%Y%m%d%H%M%S')}.json"}


def _member_history(db: Session, facility_id: int, c, pdx: str) -> List[Dict[str, Any]]:
    if not c.member_id or not c.encounter_start:
        return []
    d1 = c.encounter_start
    d0 = d1 - timedelta(days=max(v[1] for v in REPEAT_TESTS.values()))
    rows = db.execute(_HIST_SQL, {"f": facility_id, "m": c.member_id, "c": c.claim_id, "d0": d0, "d1": d1}).all()
    if not rows:
        return []
    acts: Dict[int, list] = {}
    for a in db.execute(_HIST_ACT_SQL, {"ids": [r.id for r in rows]}):
        acts.setdefault(a.claim_id_fk, []).append(a)
    out = []
    for r in sorted(rows, key=lambda r: r.encounter_start, reverse=True):
        out.append({"claim_id": r.claim_id, "date": r.encounter_start.astimezone(DUBAI).date().isoformat(),
                    "days_before": (d1 - r.encounter_start).days, "clinician_id": r.clinician_id,
                    "principal_dx": r.pdx, "same_dx": bool(pdx and r.pdx and r.pdx.upper() == pdx.upper()),
                    "codes": sorted({(a.activity_code or "").upper() for a in acts.get(r.id, [])})})
    return out


# ---------------------------------------------------------------------------
# Draft
# ---------------------------------------------------------------------------
SN_MAX_RESUBMISSIONS = 2      # SN 03/2019 3.8.2 / 3.8.5 — inclusive of the reconciliation
SN_RESUBMIT_DAYS = 21         # 3.8.1 — from the RA with denial reason (not for reconciliation, 3.10.4)
SN_VALIDITY_MONTHS = 24       # 3.10.5 — from the first submission

_SN_SQL = text("""
SELECT count(*) FILTER (WHERE resubmission_key <> '')            AS resubs,
       min(submission_date) FILTER (WHERE resubmission_key = '')  AS first_sub,
       min(submission_date)                                       AS any_sub
FROM claims WHERE facility_id = :f AND claim_id = :c
""")


_RECON_SQL = text("""
SELECT EXISTS (SELECT 1 FROM resub_tracking WHERE facility_id = :f AND claim_id = :c AND status = :s)""")


def sn_status(db: Session, facility_id: int, claim_id: str, ra_date=None) -> Dict[str, Any]:
    """eClaimLink Standards Notice SN 03/2019 limits for this claim: resubmissions used, 21-day deadline, 24-month validity."""
    r = db.execute(_SN_SQL, {"f": facility_id, "c": claim_id}).first()
    used = int(r.resubs or 0) if r else 0
    first = (r.first_sub or r.any_sub) if r else None
    first_d = first.date() if hasattr(first, "date") else first
    ra_d = ra_date.date() if hasattr(ra_date, "date") else ra_date
    today = datetime.now(DUBAI).date()
    due = ra_d + timedelta(days=SN_RESUBMIT_DAYS) if ra_d else None
    # first ORIGINAL submission (resubmission_key = ''); fallback: earliest submission of any kind
    valid_until = _valid_until(first_d, None) if first_d else None
    agreed = bool(db.execute(_RECON_SQL, {"f": facility_id, "c": claim_id, "s": RECON_AGREED}).scalar())
    notes = []
    if used >= SN_MAX_RESUBMISSIONS:
        notes.append(f"SN 03/2019 3.8.2: this claim has already been resubmitted {used} times — the limit is "
                     f"{SN_MAX_RESUBMISSIONS} (including reconciliation). No further resubmission is permitted.")
    elif used == SN_MAX_RESUBMISSIONS - 1:
        notes.append("SN 03/2019 3.8.2 / 3.10: one resubmission already made — the next one is the last permitted and "
                     "should be the agreed Reconciliation.")
    if due and today > due:
        notes.append(f"SN 03/2019 3.8.1: the 21-day resubmission window closed on {due.isoformat()} (RA {ra_d.isoformat()}). "
                     "Only an agreed Reconciliation is outside this limit (3.10.4); late resubmission carries a 0.03%/day fee (3.8.4).")
    if valid_until and today > valid_until:
        notes.append(f"SN 03/2019 3.10.5: claim validity (24 months from {first_d.isoformat()}) ended {valid_until.isoformat()}.")
    return {"resubmissions_used": used, "max_resubmissions": SN_MAX_RESUBMISSIONS,
            "limit_reached": used >= SN_MAX_RESUBMISSIONS,
            "first_submission": first_d.isoformat() if first_d else None,
            "resubmit_by": due.isoformat() if due else None, "window_closed": bool(due and today > due),
            "valid_until": valid_until.isoformat() if valid_until else None,
            "expired": bool(valid_until and today > valid_until), "recon_agreed": agreed, "notes": notes}


def sn_errors(draft: Dict[str, Any], rtype: str) -> List[str]:
    """Hard SN 03/2019 stops applied when a resubmission file is generated."""
    sn = draft.get("sn") or {}
    errs = []
    if sn.get("limit_reached"):
        errs.append(f"SN 03/2019 3.8.2: resubmission limit reached ({sn['resubmissions_used']} of {sn['max_resubmissions']})")
    if sn.get("expired"):
        errs.append(f"SN 03/2019 3.10.5: claim validity ended {sn['valid_until']}")
    agreed = bool(sn.get("recon_agreed"))
    if rtype == "reconciliation" and not agreed:
        errs.append("Reconciliation needs the facility's confirmed agreement with the payer (SN 3.10.1) — "
                    "mark it Reconciliation agreed in the Resubmission worklist")
    if sn.get("resubmissions_used") == SN_MAX_RESUBMISSIONS - 1 and rtype != "reconciliation":
        errs.append("SN 03/2019 3.8.2 / 3.10: one resubmission already made — the second (last) resubmission "
                    "must be type 'reconciliation'")
    if sn.get("window_closed") and not (rtype == "reconciliation" and agreed):
        errs.append(f"SN 03/2019 3.8.1: 21-day window closed {sn['resubmit_by']} — only an agreed Reconciliation may be sent")
    return errs


def build_draft(db: Session, facility_id: int, claim_id: str) -> Dict[str, Any]:
    c = db.execute(_CLAIM_SQL, {"f": facility_id, "c": claim_id}).first()
    if c is None:
        raise LookupError(f"Claim {claim_id} not found for facility {facility_id}")
    fac = db.get(Facility, facility_id)
    acts = db.execute(_ACT_SQL, {"id": c.id}).all()
    dxs = db.execute(_DX_SQL, {"id": c.id}).all()
    ra = db.execute(_RA_SQL, {"f": facility_id, "c": claim_id}).first()
    rads = db.execute(_RAD_SQL, {"id": ra.id}).all() if ra else []
    warnings: List[str] = []
    rj = None

    claim_el, header = _original_claim_xml(db, c.raw_xml_id, claim_id)
    if header.get("_format") == "riayati_json" and claim_el is not None:
        rj = claim_el
        claim_el = _riayati_to_el(rj)
    source = "raw_xml"
    if claim_el is None:
        source = "tables"
        claim_el = _claim_from_tables(c, acts, dxs)
        warnings.append("Original XML not available — claim rebuilt from stored tables; observations/attachments "
                        "from the original file are not included. Check every field before upload.")
    if rj is not None:
        warnings.append("Riayati (MOH) claim — output is Riayati JSON in the original format; DHPO upload does not apply.")
    elif c.source_channel and c.source_channel != "FILE":
        warnings.append(f"Claim came in via {c.source_channel} — DHPO resubmission may not apply.")
    if ra and ra.remittance_date and c.submission_date and c.submission_date > ra.remittance_date:
        warnings.append("The latest submission is newer than the latest RA — a resubmission may already be pending.")

    sn = sn_status(db, facility_id, claim_id, ra.remittance_date if ra else None)
    warnings.extend(sn["notes"])

    # IDPayer from the RA when the claim lacks it
    if not _txt(claim_el, "IDPayer") and ra and ra.claim_id_payer:
        _set_child(claim_el, "IDPayer", ra.claim_id_payer, CLAIM_ORDER)

    dx_list = [{"type": _txt(d, "Type"), "code": _txt(d, "Code")} for d in claim_el.findall("Diagnosis")]
    dx_desc = {(d.diagnosis_code or "").upper(): d.diagnosis_description for d in dxs}
    for d in dx_list:
        d["description"] = dx_desc.get(d["code"].upper()) or ""
    pdx = next((d for d in dx_list if _is_principal(d["type"])), dx_list[0] if dx_list else {})
    db_act = {str(a.activity_id): a for a in acts}
    ra_act = {str(r.activity_id): r for r in rads}

    activities = []
    for ae in claim_el.findall("Activity"):
        aid = _txt(ae, "ID")
        a, r = db_act.get(aid), ra_act.get(aid)
        activities.append({
            "activity_id": aid, "start": _txt(ae, "Start"), "type": _txt(ae, "Type"), "code": _txt(ae, "Code"),
            "description": (a.activity_description if a else "") or "", "quantity": _txt(ae, "Quantity"),
            "net": _txt(ae, "Net"), "gross": float(a.gross) if a and a.gross is not None else None,
            "patient_share": float(a.patient_share) if a and a.patient_share is not None else None,
            "clinician": _txt(ae, "Clinician"), "prior_auth": _txt(ae, "PriorAuthorizationID"),
            "ra": None if r is None else {
                "paid": float(r.paid_amount or 0), "net": float(r.net or 0),
                "denial_code": r.denial_code or "", "denial_type": r.denial_type or "",
                "denial_description": denial_description(r.denial_code, r.denial_description) or ""},
        })

    history = _member_history(db, facility_id, c, pdx.get("code", ""))
    enc_start = c.encounter_start.astimezone(DUBAI).date() if c.encounter_start else None
    dos = enc_start.strftime("%d/%m/%Y") if enc_start else "[date]"

    denied = []
    for act in activities:
        r = act["ra"]
        if not r or not r["denial_code"]:
            continue
        fam_code = _family(r["denial_code"])
        category, cat_text, _ = classify(r["denial_description"], act["code"], CODE_CATEGORY.get(fam_code, "Other"),
                                         r["denial_code"])
        fam_code = CATEGORY_FAMILY.get(category, fam_code)   # template from the payer's actual reason
        fam = FAMILIES.get(fam_code, DEFAULT_FAMILY)
        just = fam["tpl"].format(code=act["code"], desc=act["description"] or "service", pdx=pdx.get("code", "[Dx]"),
                                 pdx_desc=pdx.get("description") or "see notes", dos=dos,
                                 auth=act["prior_auth"] or "[approval no.]", net=act["net"])
        reqs = [_req(k, "Expected for " + fam_code + " denials") for k in fam["reqs"]]
        if fam_code == "AUTH":
            reqs[0]["evidence"] = ([f"Activity already carries approval {act['prior_auth']} — check validity/dates"]
                                   if act["prior_auth"] else ["No PriorAuthorizationID on the activity"])
        code = act["code"].upper()
        # repeat lab within interval
        if code in REPEAT_TESTS:
            name, days = REPEAT_TESTS[code]
            prev = [h for h in history if code in h["codes"] and h["days_before"] <= days]
            if prev:
                ev = [f"{name} {code} also billed on {h['date']} ({h['days_before']} days earlier, claim {h['claim_id']})" for h in prev]
                reqs.append(_req("previous_history", f"Repeat {name} within {days} days", ev))
                reqs.append(_req("lab_report", "Show previous result and the reason to repeat", ev))
                reqs.append(_req("clinical_evaluation", "Change in condition / treatment monitoring must be documented", ev))
                just += (f" Repeat {name} within {days} days is justified: previous test on {prev[0]['date']}; "
                         f"[reason: change in condition / medication adjustment / abnormal previous result].")
        # consultation within the follow-up windows
        if CONSULT_CHECKED.match(code) or CONSULT_CPT.match(code):
            prev = [h for h in history if h["days_before"] <= REPEAT_VISIT_DAYS and
                    any(CONSULT_CHECKED.match(x) or CONSULT_CPT.match(x) for x in h["codes"])]
            if prev:
                h = prev[0]
                window = ("free follow-up (≤7 days, should be billed as .01 with net 0)" if h["days_before"] <= FOLLOWUP_DAYS
                          else "repeat consultation (8–28 days, .02)")
                ev = [f"Consultation on {x['date']} ({x['days_before']} days earlier, Dx {x['principal_dx'] or '-'}"
                      f"{', same Dx' if x['same_dx'] else ''}, claim {x['claim_id']})" for x in prev]
                reqs.append(_req("additional_visit", f"Previous visit falls in the {window} window", ev))
                reqs.append(_req("previous_history", "Previous visit details", ev))
                just += (f" This visit is not a follow-up of the visit on {h['date']}: new complaint / diagnosis "
                         f"{pdx.get('code', '[ICD]')} [describe].")
        denied.append({
            "activity_id": act["activity_id"], "code": act["code"], "description": act["description"],
            "facts": {"denial_code": r["denial_code"], "denial_description": r["denial_description"],
                      "denial_type": r["denial_type"], "net": act["net"], "paid": r["paid"],
                      "prior_auth": act["prior_auth"]},
            "suggestion": {"family": fam_code, "category": category, "resubmission_type": fam["type"],
                           "justification": just},
            "requirements": _dedupe(reqs),
        })

    types = {d["suggestion"]["resubmission_type"] for d in denied}
    overall = "correction" if "correction" in types else "internal complaint"
    if sn.get("resubmissions_used") == SN_MAX_RESUBMISSIONS - 1:
        overall = "reconciliation"   # SN 03/2019 3.8.2 / 3.10: the 2nd resubmission is the agreed reconciliation
    # MNEC: rule-based evidence (guideline table + case sheet findings + abnormal results), grouped by indication
    from . import mnec_evidence
    case_sheet = db.execute(text("SELECT case_sheet FROM scrub_claims WHERE facility_id = :f AND claim_id = :c "
                                 "AND COALESCE(case_sheet, '') <> '' ORDER BY id DESC LIMIT 1"),
                            {"f": facility_id, "c": claim_id}).scalar()
    freq = _test_frequency(db, facility_id, c, [d["code"] for d in denied
                                                if str(d["facts"].get("denial_code") or "").upper().startswith("MNEC")
                                                or str(d["suggestion"].get("category") or "").startswith("Medical necessity")])
    mnec = mnec_evidence.evidence(denied, dx_list, case_sheet, claim_el, pdx, dos, freq)
    mnec["case_sheet"] = bool(case_sheet)
    others = [d for d in denied if "mnec" not in d]
    comment = " | ".join([mnec["comment"]] * bool(mnec["comment"]) +
                         [f"Act {d['activity_id']} {d['code']} ({d['facts']['denial_code']}): {d['suggestion']['justification']}"
                          for d in others])
    if not denied:
        warnings.append("No denied activity on the latest RA." if ra else "No remittance found for this claim.")

    return {
        "facts": {
            "facility_id": facility_id, "facility_name": fac.facility_name if fac else "",
            "sender_id": header.get("SenderID") or c.sender_id or (fac.license_id if fac else ""),
            "receiver_id": header.get("ReceiverID") or c.receiver_id or c.payer_id,
            "receiver_name": c.receiver_name, "payer_id": c.payer_id, "payer_name": c.payer_name,
            "claim_id": c.claim_id, "id_payer": _txt(claim_el, "IDPayer"), "member_id": c.member_id,
            "member_id_masked": mask(c.member_id), "encounter_start": dos, "clinician": c.clinician_name or c.clinician_id,
            "gross": float(c.gross_amount or 0), "patient_share": float(c.patient_share_total or 0),
            "net": float(c.net_amount or 0), "submission_date": c.submission_date.isoformat() if c.submission_date else None,
            "resubmission_count": c.resubmission_count, "last_resubmission_type": c.resubmission_type,
            "source": source, "raw_xml_id": c.raw_xml_id,
            "ra": None if not ra else {"remittance_date": ra.remittance_date.isoformat() if ra.remittance_date else None,
                                       "paid": float(ra.paid_amount or 0), "denied": float(ra.denial_amount or 0),
                                       "status": ra.claim_status_ra, "id_payer": ra.claim_id_payer},
            "diagnoses": dx_list, "activities": activities,
        },
        "denied": denied,
        "history": history,
        "suggestion": {"resubmission_type": overall, "comment": comment[:COMMENT_MAX]},
        "mnec": mnec,
        "warnings": warnings,
        "sn": sn,
        "_claim_xml": etree.tostring(claim_el, encoding="unicode"),
        "_riayati_claim": rj,
        "format": "riayati_json" if rj is not None else "dhpo_xml",
        "_header": header,
    }


def _req(key, reason, evidence=None):
    kind, label = REQ_LABELS[key]
    return {"key": key, "kind": kind, "label": label, "needed": True, "reason": reason, "evidence": evidence or []}


def _dedupe(reqs):
    seen, out = {}, []
    for r in reqs:
        if r["key"] in seen:
            o = seen[r["key"]]
            o["evidence"] += [e for e in r["evidence"] if e not in o["evidence"]]
            if r["reason"] not in o["reason"]:
                o["reason"] += "; " + r["reason"]
        else:
            seen[r["key"]] = r
            out.append(r)
    return out


def public_draft(draft: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in draft.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# XML
# ---------------------------------------------------------------------------
def _set_child(parent, tag, value, order):
    el = parent.find(tag)
    if value in (None, ""):
        if el is not None:
            parent.remove(el)
        return
    if el is None:
        el = etree.Element(tag)
        idx = order.index(tag)
        pos = len(parent)
        for i, ch in enumerate(parent):
            if ch.tag in order and order.index(ch.tag) > idx:
                pos = i
                break
        parent.insert(pos, el)
    el.text = str(value)


def _to_json(el):
    kids = list(el)
    if not kids:
        return el.text
    out: Dict[str, Any] = {}
    for k in kids:
        v = _to_json(k)
        if k.tag in ("Diagnosis", "Activity", "Observation", "Claim"):
            out.setdefault(k.tag, []).append(v)
        else:
            out[k.tag] = v
    return out


def build_xml(draft: Dict[str, Any], edits: Optional[Dict[str, Any]] = None, disposition: str = TEST,
              now: Optional[datetime] = None) -> Dict[str, Any]:
    """edits: {resubmission_type, comment, attachment_b64, scope: whole|denied_only,
               claim: {member_id, emirates_id, id_payer, payer_id, gross, patient_share, net},
               activities: {activity_id: {code, quantity, net, prior_authorization_id, clinician}},
               diagnoses: [{type, code}] (replaces all)}"""
    edits = edits or {}
    errors: List[str] = []
    if disposition not in DISPOSITIONS:
        errors.append(f"DispositionFlag must be one of {DISPOSITIONS}")
        disposition = TEST
    now = now or datetime.now(DUBAI)
    if draft.get("_riayati_claim") is not None:
        out = _build_riayati(draft, edits, disposition, now)
        out["errors"] = errors + out["errors"]
        return out
    facts = draft["facts"]
    claim = etree.fromstring(draft["_claim_xml"])
    for old in claim.findall("Resubmission"):
        claim.remove(old)

    ce = edits.get("claim") or {}
    for key, tag in (("id_payer", "IDPayer"), ("member_id", "MemberID"), ("payer_id", "PayerID"),
                     ("emirates_id", "EmiratesIDNumber")):
        if ce.get(key) not in (None, ""):
            _set_child(claim, tag, str(ce[key]).strip(), CLAIM_ORDER)

    if edits.get("diagnoses"):
        for d in claim.findall("Diagnosis"):
            claim.remove(d)
        anchor = claim.find("Activity")
        for d in edits["diagnoses"]:
            dx = etree.Element("Diagnosis")
            etree.SubElement(dx, "Type").text = str(d.get("type", "")).strip()
            etree.SubElement(dx, "Code").text = str(d.get("code", "")).strip().upper()
            if anchor is not None:
                anchor.addprevious(dx)
            else:
                claim.append(dx)

    act_edits = edits.get("activities") or {}
    by_id = {_txt(a, "ID"): a for a in claim.findall("Activity")}
    for aid, ae in act_edits.items():
        a = by_id.get(str(aid))
        if a is None:
            errors.append(f"Activity {aid} is not in the latest version of the claim (Activity IDs must not change)")
            continue
        for key, tag in (("code", "Code"), ("quantity", "Quantity"), ("net", "Net"),
                         ("prior_authorization_id", "PriorAuthorizationID"), ("clinician", "Clinician")):
            if key in ae and ae[key] is not None:
                _set_child(a, tag, str(ae[key]).strip(), ACTIVITY_ORDER)

    # scope: internal complaint may resend only the disputed lines (payer-dependent, see open questions)
    if edits.get("scope") == "denied_only":
        keep = {d["activity_id"] for d in draft["denied"]}
        ps_by = {a["activity_id"]: a.get("patient_share") or 0 for a in facts["activities"]}
        ps = Decimal("0.00")
        for aid, a in list(by_id.items()):
            if aid not in keep:
                claim.remove(a)
            else:
                ps += _d(ps_by.get(aid))
        net = sum((_d(_txt(a, "Net")) for a in claim.findall("Activity")), Decimal("0.00"))
        _set_child(claim, "Net", net, CLAIM_ORDER)
        _set_child(claim, "PatientShare", ps, CLAIM_ORDER)
        _set_child(claim, "Gross", net + ps, CLAIM_ORDER)
    for key, tag in (("gross", "Gross"), ("patient_share", "PatientShare"), ("net", "Net")):
        if ce.get(key) not in (None, ""):
            _set_child(claim, tag, _d(ce[key]), CLAIM_ORDER)

    rtype = (edits.get("resubmission_type") or draft["suggestion"]["resubmission_type"]).strip().lower()
    comment = (edits.get("comment") if edits.get("comment") is not None else draft["suggestion"]["comment"]).strip()
    if rtype == "correction" and edits.get("scope") == "denied_only":
        errors.append("A correction must resubmit all activities (paid and denied) — use the whole claim")
    if not comment:
        errors.append("Resubmission comment is required")
    # AUTH denials: the approval number must be on the activity and quoted in the comment
    if rtype != "reconciliation":
        for d in draft.get("denied", []):
            if not str((d.get("facts") or {}).get("denial_code") or "").upper().startswith("AUTH"):
                continue
            a = by_id.get(d["activity_id"])
            auth = _txt(a, "PriorAuthorizationID") if a is not None else ""
            if not auth:
                errors.append(f"Activity {d['activity_id']} ({d['code']}) was denied for authorisation — enter the prior approval number")
            elif auth not in comment:
                errors.append(f"Mention approval {auth} for {d['code']} in the resubmission comment")
    rs = etree.Element("Resubmission")
    etree.SubElement(rs, "Type").text = rtype
    errors.extend(sn_errors(draft, rtype))
    etree.SubElement(rs, "Comment").text = comment
    att = (edits.get("attachment_b64") or "").strip()
    if att:
        if att.startswith("data:"):
            att = att.split(",", 1)[-1]
        etree.SubElement(rs, "Attachment").text = att
    last_act = claim.findall("Activity")
    if last_act:
        last_act[-1].addnext(rs)
    else:
        claim.append(rs)

    h = draft.get("_header") or {}
    if "_root_nsmap" in h:   # same root namespaces / attributes as the original file
        root = etree.Element("Claim.Submission", nsmap=h["_root_nsmap"] or None)
        for k, v in (h.get("_root_attrib") or {}).items():
            root.set(k, v)
    else:
        root = etree.Element("Claim.Submission", nsmap={"xsi": "http://www.w3.org/2001/XMLSchema-instance",
                                                        "xsd": "http://www.w3.org/2001/XMLSchema"})
    hdr = etree.SubElement(root, "Header")
    etree.SubElement(hdr, "SenderID").text = facts["sender_id"] or ""
    etree.SubElement(hdr, "ReceiverID").text = facts["receiver_id"] or ""
    etree.SubElement(hdr, "TransactionDate").text = now.strftime("%d/%m/%Y %H:%M")
    etree.SubElement(hdr, "RecordCount").text = "1"
    etree.SubElement(hdr, "DispositionFlag").text = disposition
    root.append(claim)

    xml = etree.tostring(root, xml_declaration=True, encoding="utf-8", pretty_print=True).decode("utf-8")
    errors += validate_xml(xml)
    warn = []
    if len(comment) > COMMENT_WARN:
        warn.append(f"Comment is {len(comment)} chars — longer than any accepted file seen ({COMMENT_WARN}).")
    if len(att) > ATTACHMENT_WARN_B64:
        warn.append("Attachment is larger than any accepted file seen (~1.35 MB base64).")
    if facts.get("source") == "tables":
        warn.append("Built from stored tables (no original XML).")
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", facts["claim_id"])
    filename = f"{re.sub(r'[^A-Za-z0-9_-]', '_', facts['sender_id'] or 'SENDER')}_RESUB_{safe_id}_{now.strftime('%Y%m%d%H%M%S')}.xml"
    mirror = {"Header": _to_json(hdr), "Claim": _to_json(claim)}
    if att:
        mirror["Claim"]["Resubmission"]["Attachment"] = f"<base64, {len(att)} chars>"
    return {"xml": xml, "json": mirror, "errors": errors, "warnings": warn, "filename": filename, "format": "dhpo_xml"}


def validate_xml(xml: str) -> List[str]:
    errs: List[str] = []
    try:
        root = etree.fromstring(xml.encode("utf-8"), etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True))
    except etree.XMLSyntaxError as e:
        return [f"XML not well-formed: {e}"]
    if root.tag != "Claim.Submission":
        errs.append("Root element must be Claim.Submission")
    hdr = root.find("Header")
    if hdr is None:
        return errs + ["Header missing"]
    for t in ("SenderID", "ReceiverID", "TransactionDate", "RecordCount", "DispositionFlag"):
        if not _txt(hdr, t):
            errs.append(f"Header/{t} missing")
    if _txt(hdr, "TransactionDate") and not DT_RE.match(_txt(hdr, "TransactionDate")):
        errs.append("Header/TransactionDate must be dd/MM/yyyy HH:mm")
    if _txt(hdr, "DispositionFlag") and _txt(hdr, "DispositionFlag") not in DISPOSITIONS:
        errs.append("Header/DispositionFlag invalid")
    claims = root.findall("Claim")
    if _txt(hdr, "RecordCount") != str(len(claims)):
        errs.append(f"RecordCount {_txt(hdr, 'RecordCount')} ≠ number of claims {len(claims)}")
    for cl in claims:
        cid = _txt(cl, "ID") or "?"
        p = f"Claim {cid}: "
        for t in ("ID", "MemberID", "PayerID", "ProviderID", "EmiratesIDNumber", "Gross", "PatientShare", "Net"):
            if not _txt(cl, t):
                errs.append(p + f"{t} missing")
        for t in ("Gross", "PatientShare", "Net"):
            if _txt(cl, t) and not DEC_RE.match(_txt(cl, t)):
                errs.append(p + f"{t} must be a decimal with ≤3 dp")
        g, ps, n = _d(_txt(cl, "Gross")), _d(_txt(cl, "PatientShare")), _d(_txt(cl, "Net"))
        if abs(g - ps - n) > Decimal("0.01"):
            errs.append(p + f"Net {n} ≠ Gross {g} − PatientShare {ps}")
        enc = cl.find("Encounter")
        if enc is None:
            errs.append(p + "Encounter missing")
        else:
            for t in ("FacilityID", "Type", "PatientID", "Start"):
                if not _txt(enc, t):
                    errs.append(p + f"Encounter/{t} missing")
            for t in ("Start", "End"):
                if _txt(enc, t) and not DT_RE.match(_txt(enc, t)):
                    errs.append(p + f"Encounter/{t} must be dd/MM/yyyy HH:mm")
        dxs = cl.findall("Diagnosis")
        if not dxs:
            errs.append(p + "at least one Diagnosis required")
        if sum(1 for d in dxs if _is_principal(_txt(d, "Type"))) != 1:
            errs.append(p + "exactly one Principal diagnosis required")
        for d in dxs:
            if not _txt(d, "Type") or not _txt(d, "Code"):
                errs.append(p + "Diagnosis Type and Code required")
        acts = cl.findall("Activity")
        if not acts:
            errs.append(p + "at least one Activity required")
        ids = [_txt(a, "ID") for a in acts]
        if len(ids) != len(set(ids)):
            errs.append(p + "duplicate Activity ID")
        anet = Decimal("0.00")
        for a in acts:
            ap = p + f"Activity {_txt(a, 'ID') or '?'}: "
            for t in ("ID", "Start", "Type", "Code", "Quantity", "Net", "Clinician"):
                if not _txt(a, t):
                    errs.append(ap + f"{t} missing")
            if _txt(a, "Start") and not DT_RE.match(_txt(a, "Start")):
                errs.append(ap + "Start must be dd/MM/yyyy HH:mm")
            try:
                Decimal(_txt(a, "Net") or "0")
                Decimal(_txt(a, "Quantity") or "0")
            except InvalidOperation:
                errs.append(ap + "Net / Quantity must be numeric")
            anet += _d(_txt(a, "Net"))
        if acts and abs(anet - n) > Decimal("0.05"):
            errs.append(p + f"sum of Activity Net {anet} ≠ Claim Net {n}")
        rs = cl.find("Resubmission")
        if rs is None:
            errs.append(p + "Resubmission element missing")
        else:
            if _txt(rs, "Type") not in RESUB_TYPES:
                errs.append(p + f"Resubmission/Type must be one of {RESUB_TYPES}")
            if not _txt(rs, "Comment"):
                errs.append(p + "Resubmission/Comment required")
            elif len(_txt(rs, "Comment")) > COMMENT_MAX:
                errs.append(p + f"Resubmission/Comment longer than {COMMENT_MAX} characters")
            if _txt(rs, "Attachment"):
                try:
                    raw = base64.b64decode(_txt(rs, "Attachment"), validate=True)
                    if not raw.startswith(b"%PDF"):
                        errs.append(p + "Attachment is not a PDF")
                    if len(raw) > ATTACHMENT_MAX_BYTES:
                        errs.append(p + "Attachment larger than 5 MB")
                except (binascii.Error, ValueError):
                    errs.append(p + "Attachment is not valid base64")
            if cl.index(rs) < max((cl.index(a) for a in acts), default=-1):
                errs.append(p + "Resubmission must come after the activities")
    return errs


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
_DDL = """
CREATE TABLE IF NOT EXISTS resub_submissions (
    id           BIGSERIAL PRIMARY KEY,
    facility_id  INTEGER NOT NULL,
    claim_id     VARCHAR(100) NOT NULL,
    disposition  VARCHAR(30) NOT NULL,
    xml          TEXT,
    json         JSONB,
    status       VARCHAR(30) NOT NULL,
    dhpo_result  INTEGER,
    error_report TEXT,
    created_by   INTEGER,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_resub_submissions_claim ON resub_submissions (facility_id, claim_id, id);
"""
_table_ready = False


def ensure_table(engine) -> None:
    """Idempotent; runs in its own short transaction with a lock timeout so it never waits on other sessions."""
    global _table_ready
    if _table_ready:
        return
    with engine.connect() as conn:
        exists = conn.execute(text("SELECT to_regclass('public.resub_submissions')")).scalar()
        conn.rollback()
        if not exists:
            with conn.begin():
                conn.execute(text("SET LOCAL lock_timeout = '3s'"))
                conn.execute(text("SET LOCAL statement_timeout = '10s'"))
                for stmt in filter(None, (s.strip() for s in _DDL.split(";"))):
                    conn.execute(text(stmt))
    _table_ready = True


def record(db: Session, facility_id: int, claim_id: str, disposition: str, xml: Optional[str], mirror: Any,
           status: str, user_id: Optional[int], dhpo_result: Optional[int] = None,
           error_report: Optional[str] = None) -> int:
    rid = db.execute(text("""
        INSERT INTO resub_submissions (facility_id, claim_id, disposition, xml, json, status, dhpo_result, error_report, created_by)
        VALUES (:f, :c, :d, :x, CAST(:j AS JSONB), :s, :r, :e, :u) RETURNING id"""),
        {"f": facility_id, "c": claim_id, "d": disposition, "x": xml, "j": json.dumps(mirror) if mirror is not None else None,
         "s": status, "r": dhpo_result, "e": error_report, "u": user_id}).scalar()
    db.commit()
    return int(rid)


def history(db: Session, facility_id: int, claim_id: str) -> List[Dict[str, Any]]:
    rows = db.execute(text("""
        SELECT s.id, s.disposition, s.status, s.dhpo_result, s.error_report, s.created_at, u.full_name AS by_name
        FROM resub_submissions s LEFT JOIN users u ON u.id = s.created_by
        WHERE s.facility_id = :f AND s.claim_id = :c ORDER BY s.id DESC LIMIT 50"""),
        {"f": facility_id, "c": claim_id}).mappings().all()
    db.commit()
    return [{**dict(r), "created_at": r["created_at"].isoformat() if r["created_at"] else None} for r in rows]


def get_xml(db: Session, facility_id: int, sub_id: int) -> Optional[Dict[str, Any]]:
    r = db.execute(text("SELECT id, claim_id, xml FROM resub_submissions WHERE id = :i AND facility_id = :f"),
                   {"i": sub_id, "f": facility_id}).mappings().first()
    db.commit()
    return dict(r) if r else None


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------
class UploadRefused(Exception):
    pass


def _default_client():
    from .fetch_service import DHPO_LIVE_WSDL, DHPOSoapClient
    return DHPOSoapClient(DHPO_LIVE_WSDL)


def upload(db: Session, facility_id: int, xml: str, filename: str, disposition: str, user,
           confirm: bool = False, typed_claim_id: Optional[str] = None,
           client_factory: Callable[[], Any] = _default_client) -> Dict[str, Any]:
    """DHPO UploadTransaction. PRODUCTION needs admin + confirm + typed claim ID equal to the XML's Claim ID."""
    if disposition not in DISPOSITIONS:
        raise UploadRefused("Unknown disposition")
    if xml.lstrip().startswith("{"):
        raise UploadRefused("Riayati JSON cannot be uploaded to DHPO")
    errs = validate_xml(xml)
    if errs and "well-formed" in errs[0]:
        raise UploadRefused(errs[0])
    root = etree.fromstring(xml.encode("utf-8"), etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True))
    flag = _txt(root.find("Header"), "DispositionFlag")
    if flag != disposition:
        raise UploadRefused(f"XML DispositionFlag is '{flag}' but upload requested '{disposition}' — regenerate the XML")
    claim_ids = [_txt(c, "ID") for c in root.findall("Claim")]
    if len(claim_ids) != 1:
        raise UploadRefused("Exactly one claim per resubmission file")
    claim_id = claim_ids[0]
    if errs:
        raise UploadRefused("XML has validation errors: " + "; ".join(errs[:5]))
    if disposition == PRODUCTION:
        if getattr(user, "role", None) != "admin":
            raise UploadRefused("PRODUCTION upload is admin only")
        if not confirm or (typed_claim_id or "").strip() != claim_id:
            raise UploadRefused("PRODUCTION upload needs confirmation and the claim ID typed exactly")
    fac = db.get(Facility, facility_id)
    if fac is None:
        raise UploadRefused("Unknown facility")
    login, pwd = fac.eclaimlink_username, fac.credential_key
    db.commit()   # release the read transaction before the network call
    if not login or not pwd:
        raise UploadRefused("Facility has no eClaimLink credentials")
    mirror = {"filename": filename}
    status, code, report, msg = "error", None, None, ""
    try:
        client = client_factory()
        res = client._client.service.UploadTransaction(login=login, pwd=pwd, fileContent=xml.encode("utf-8"),
                                                       fileName=filename)
        code = int(getattr(res, "UploadTransactionResult", res if isinstance(res, int) else -99))
        msg = getattr(res, "errorMessage", "") or ""
        rep = getattr(res, "errorReport", None)
        if rep:
            report = base64.b64encode(rep).decode("ascii") if isinstance(rep, (bytes, bytearray)) else str(rep)
        status = "uploaded" if code == 0 else "uploaded_with_warnings" if code == 1 else "rejected"
    except Exception as exc:   # network / SOAP fault — never include credentials
        msg = f"{type(exc).__name__}: {str(exc)[:300]}"
        log.warning("resub upload failed facility=%s claim=%s: %s", facility_id, claim_id, type(exc).__name__)
    mirror["message"] = msg
    rid = record(db, facility_id, claim_id, disposition, xml, mirror, status, getattr(user, "id", None),
                 dhpo_result=code, error_report=report)
    return {"id": rid, "status": status, "dhpo_result": code, "message": msg, "error_report": report,
            "disposition": disposition, "claim_id": claim_id}
