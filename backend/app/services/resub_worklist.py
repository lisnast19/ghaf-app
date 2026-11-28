"""
resub_worklist.py — Daily resubmission worklist built from RA denials.

Every denied activity whose resubmission window is still open becomes a work
item with: owner (Ghaf team or Facility), action, what to ask the facility for,
documents, a draft resubmission comment, deadline, and an expected recovery
based on how often this receiver has paid on resubmission for this denial code
at this facility.

Used by:
  • routers/resub.py              — the Resubmission Worklist screen
  • report-scripts/resub_worklist.py — nightly Excel files (Ghaf team + facility)
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..models import ResubPlaybook, ResubTracking
from .licence import norm_licence

WINDOW_DAYS = 21
URGENT_DAYS = 5
# Only send the facility items worth their time: P1/P2 and a payer that
# actually pays on resubmission. Everything else stays with the Ghaf team.
FACILITY_MIN_RECOVERY_PCT = 20
LOW_VALUE_ACTION = ("Low chance of recovery (payer rarely pays this denial on resubmission). "
                    "Not sent to the facility — close as write-off unless the claim shows a clear case.")
LOOKBACK_DAYS = 400

STATUSES = [
    "New", "In progress", "Sent to facility", "Facility replied", "Ready to resubmit",
    "Resubmitted", "Not resubmitting (write-off)", "Collect from patient",
    # reconciliation flow: Ghaf sends → facility takes it up with the payer → facility confirms → Ghaf resubmits
    "Sent to facility (reconciliation)", "Facility discussing with payer",
    "Agreed — resubmit as Reconciliation", "Not agreed with payer (write-off)",
]
CLOSED_BY_TEAM = ("Resubmitted", "Not resubmitting (write-off)", "Collect from patient",
                  "Not agreed with payer (write-off)")
RECON_AGREED = "Agreed — resubmit as Reconciliation"

# (pattern, category, owner, facility_role, action, facility_request, documents, draft_comment)
CONSULT_ACTIVITY = r"^(9|10|11)(\.\d+)?$"
CONSULT_REASON = r"too soon|too frequent|repeat|follow[ -]?up|within \d+ days|free (follow|visit)"
CONSULT_RULE = dict(
    sort_order=5, pattern=".*", activity_pattern=CONSULT_ACTIVITY, reason_pattern=CONSULT_REASON,
    category="Consultation visit rules", owner="Ghaf", facility_role=None,
    action="Consultation denied as a repeat / follow-up visit (whatever code the payer used). Check the patient's "
           "previous visits: if the diagnosis or clinician differs, or the follow-up period has passed, resubmit "
           "explaining this; if it is a true follow-up within the free period, close as not resubmitting.",
    facility_request=None, documents="Previous and current visit details",
    draft_comment="This consultation was for a new complaint / different diagnosis from the previous visit "
                  "(or outside the follow-up period), as documented. Kindly review and settle.")

DEFAULT_PLAYBOOK = [
    ("^MNEC-00[345]", "Medical necessity", "Facility", "Doctor",
     "Resubmit with the treating doctor's clinical justification once received.",
     "Treating doctor to write a short clinical justification: presenting symptoms, findings, "
     "and why this service was needed at this visit.",
     "Consultation notes, vitals, investigation results",
     "The service was medically indicated based on the presenting complaints and clinical findings "
     "documented in the attached notes. Kindly review and settle."),
    ("^MNEC-006", "Consultation visit rules", "Ghaf", None,
     "Check the patient's previous visits: if the diagnosis or clinician differs, resubmit explaining this; "
     "if it is a true follow-up within the free period, close as not resubmitting.",
     None, "Previous and current visit details",
     "This visit was for a new complaint / different diagnosis from the previous consultation, as documented. "
     "Kindly review and settle."),
    ("^MNEC|^PRCE-011", "Medical necessity", "Facility", "Doctor",
     "Resubmit with the doctor's justification for the service / frequency.",
     "Treating doctor to justify why the service (or its frequency) was clinically required.",
     "Clinical notes, previous results showing need",
     "The service was clinically required for the patient's condition as documented in the attached notes. "
     "Kindly review and settle."),
    ("^AUTH-00[45]", "Prior approval", "Facility", "Front office",
     "Resubmit quoting the valid approval number once the facility confirms it.",
     "Front office to share the valid approval number, or request an extension / retro-approval from the payer.",
     "Approval letter / e-authorization reference",
     "The service was rendered under a valid prior approval (reference attached). Kindly review and settle."),
    ("^AUTH|^CLAI-012", "Prior approval", "Facility", "Front office",
     "Resubmit with the prior approval reference once received.",
     "Front office to share the prior approval; if none was taken, request retro-approval with the clinical reason.",
     "Approval reference, clinical notes",
     "Prior approval reference is attached for this pre-approved service. Kindly review and settle."),
    ("^PRCE-006", "Consultation visit rules", "Ghaf", None,
     "Member had another consultation within the follow-up period. Check the patient's previous visits: if the "
     "diagnosis or clinician differs, or the follow-up period has passed, resubmit explaining this; if it is a true "
     "follow-up, close as not resubmitting (8–28 days: recode to the repeat code).",
     None, "Previous and current visit details",
     "This consultation was for a new complaint / different diagnosis from the previous visit (or outside the "
     "follow-up period), as documented. Kindly review and settle."),
    ("^PRCE-010", "Coding", "Ghaf", None,
     "CPT bundling: the two codes cannot be billed together. Remove the bundled (component) code, or — if the services "
     "were separate and distinct — resubmit with the appropriate modifier and a short justification.",
     None, "Procedure notes if billing both with a modifier",
     "The services were separate and distinct procedures, billed with the appropriate modifier as documented. "
     "Kindly review and settle."),
    # PRCE-001 claimed > agreed tariff · 002 inclusive · 003 recovery · 007 not priced — the facility holds the contracts
    ("^PRCE-00[1237]", "Contract & tariff", "Facility", "Billing / management",
     "Price / contract issue — the facility holds the payer contracts. Once the facility confirms the contracted price "
     "(or that the item is included / not covered), resubmit with the corrected net, or close as write-off.",
     "Check this service against the contract with this payer: confirm the agreed price (PRCE-001 / 007), whether it "
     "is included in another service (PRCE-002), or the reason for the recovery (PRCE-003). Correct the price list and "
     "tell Ghaf the net to resubmit, or confirm write-off.",
     "Contract / tariff reference",
     "The service is billed as per the agreed contract tariff. Kindly review and settle."),
    ("^PRCE", "Other", "Ghaf", None,
     "Other price-related denial (PRCE): review the payer comment and decide — correct and resubmit, or close as write-off.",
     None, "As required by the denial", "Kindly review the attached details and settle."),
    ("^CODE", "Coding", "Ghaf", None,
     "Correct the ICD / CPT coding (principal diagnosis, sequela codes, code pairs) and resubmit.",
     None, "Corrected coding",
     "Coding has been corrected as per the clinical documentation. Kindly review and settle."),
    ("^TIME|^DOCU", "Documents", "Facility", "Front office",
     "Resubmit with the documents once received from the facility.",
     "Send the requested documents (notes, reports, invoices) for this claim.",
     "Requested documents",
     "The requested documents are attached. Kindly review and settle."),
    ("^COPY|^CLAI-014", "Documents", "Facility", "Doctor",
     "Resubmit with the clinical indication / report once received.",
     "Doctor to provide the clinical indication or report for this activity.",
     "Clinical indication, report",
     "The clinical indication for this service is documented in the attached report. Kindly review and settle."),
    ("^DUPL", "Duplicate", "Ghaf", None,
     "Check if truly duplicated. If not, resubmit explaining the separate service; if duplicate, close as write-off.",
     None, "Proof of separate service",
     "This is a separate service rendered on the same day, not a duplicate, as documented. Kindly review and settle."),
    ("^BENX|^NCOV|^ELIG", "Coverage / eligibility", "Facility", "Front office",
     "If the facility confirms eligibility, resubmit with proof; otherwise mark Collect from patient.",
     "Front office to verify eligibility / benefit on the payer portal and share proof, "
     "or confirm the amount will be collected from the patient.",
     "Eligibility check screenshot, card copy",
     "The patient was eligible and the service is covered under the policy (eligibility proof attached). "
     "Kindly review and settle."),
    (".*", "Other", "Ghaf", None,
     "Review the denial reason and decide: correct and resubmit, or close as write-off.",
     None, "As required by the denial",
     "Kindly review the attached details and settle."),
]


def ensure_playbook(db: Session) -> List[ResubPlaybook]:
    rows = db.query(ResubPlaybook).order_by(ResubPlaybook.sort_order, ResubPlaybook.id).all()
    if rows:
        # one-time addition of the consultation rule to an existing playbook
        if not any(r.activity_pattern == CONSULT_ACTIVITY for r in rows):
            db.add(ResubPlaybook(**CONSULT_RULE, updated_by="system"))
            db.commit()
            rows = db.query(ResubPlaybook).order_by(ResubPlaybook.sort_order, ResubPlaybook.id).all()
        return rows
    for i, (pat, cat, owner, role, action, req, docs, comment) in enumerate(DEFAULT_PLAYBOOK):
        db.add(ResubPlaybook(sort_order=(i + 1) * 10, pattern=pat, category=cat, owner=owner,
                             facility_role=role, action=action, facility_request=req,
                             documents=docs, draft_comment=comment, updated_by="system"))
    db.add(ResubPlaybook(**CONSULT_RULE, updated_by="system"))
    db.commit()
    return db.query(ResubPlaybook).order_by(ResubPlaybook.sort_order, ResubPlaybook.id).all()


def _matcher(playbook: List[ResubPlaybook]):
    compiled = []
    for p in playbook:
        if not p.is_active:
            continue
        try:
            compiled.append((re.compile(p.pattern, re.I),
                             re.compile(p.activity_pattern, re.I) if p.activity_pattern else None,
                             re.compile(p.reason_pattern, re.I) if p.reason_pattern else None, p))
        except re.error:
            continue

    def match(code: str, activity_code: str = "", reason: str = "") -> Optional[ResubPlaybook]:
        """First rule whose denial-code, activity-code and reason patterns all match."""
        for rx, ax, tx, p in compiled:
            if rx.search(code) and (ax is None or ax.search(activity_code or "")) \
                    and (tx is None or tx.search(reason or "")):
                return p
        return None
    return match


def _valid_until(first_sub, fallback):
    """Claim validity: 24 months from the first submission (SN 03/2019 3.10.5, eClaimLink archiving)."""
    d = first_sub or fallback
    try:
        return d.replace(year=d.year + 2)
    except ValueError:   # 29 Feb
        return d.replace(year=d.year + 2, day=28)


def norm_code(c: Optional[str]) -> str:
    return (c or "").split("|")[0].split(",")[0].strip().upper() or "—"


# ── Payer recovery model ──────────────────────────────────────────────────────
_HIST_SQL = text("""
WITH den AS (
  SELECT r.facility_id, r.claim_id, r.remittance_date AS d1, d.activity_id,
         upper(trim(split_part(split_part(COALESCE(NULLIF(d.denial_code,''), r.denial_code),'|',1),',',1))) AS code,
         GREATEST(COALESCE(d.net,0) - COALESCE(d.paid_amount,0),0) AS denied
  FROM remittances r JOIN remittance_activity_details d ON d.remittance_id_fk = r.id
  WHERE r.facility_id = :f AND r.remittance_date >= :since
    AND COALESCE(d.net,0) - COALESCE(d.paid_amount,0) > 0.009
), later AS (
  SELECT den.*, (SELECT SUM(COALESCE(d2.paid_amount,0)) FROM remittances r2
                 JOIN remittance_activity_details d2 ON d2.remittance_id_fk = r2.id
                 WHERE r2.facility_id = den.facility_id AND r2.claim_id = den.claim_id
                   AND r2.remittance_date > den.d1 AND d2.activity_id = den.activity_id) AS paid_later
  FROM den
  WHERE EXISTS (SELECT 1 FROM claims x WHERE x.facility_id = den.facility_id AND x.claim_id = den.claim_id
                AND x.resubmission_key <> '' AND x.submission_date::date >= den.d1)
)
SELECT c.receiver_id, l.code, COUNT(*) AS n,
       SUM(l.denied) AS denied, SUM(LEAST(COALESCE(l.paid_later,0), l.denied)) AS recovered
FROM later l
LEFT JOIN LATERAL (SELECT receiver_id FROM claims c WHERE c.facility_id = l.facility_id AND c.claim_id = l.claim_id
                   ORDER BY c.id LIMIT 1) c ON true
GROUP BY 1, 2
""")

_model_cache: Dict[tuple, tuple] = {}


def recovery_model(db: Session, fid: int):
    key = (fid, date.today())
    if key in _model_cache:
        return _model_cache[key]
    rows = db.execute(_HIST_SQL, {"f": fid, "since": date.today() - timedelta(days=LOOKBACK_DAYS)}).fetchall()
    by_pair, by_code, tot = {}, defaultdict(lambda: [0, 0.0, 0.0]), [0, 0.0, 0.0]
    for r in rows:
        den, rec = float(r.denied or 0), float(r.recovered or 0)
        by_pair[(r.receiver_id, r.code)] = (r.n, den, rec)
        bc = by_code[r.code]; bc[0] += r.n; bc[1] += den; bc[2] += rec
        tot[0] += r.n; tot[1] += den; tot[2] += rec
    overall = tot[2] / tot[1] if tot[1] else 0.35

    def rate(receiver, code):
        # small samples are blended toward the code-level, then facility-level rate
        base = overall
        bc = by_code.get(code)
        if bc and bc[1]:
            base = (bc[2] + overall * 200) / (bc[1] + 200)
        p = by_pair.get((receiver, code))
        if p and p[1]:
            return (p[2] + base * 150) / (p[1] + 150)
        return base

    for k in [k for k in _model_cache if k[1] != date.today()]:
        del _model_cache[k]
    _model_cache[key] = (rate, overall)
    return rate, overall


# ── Worklist ─────────────────────────────────────────────────────────────────
_OPEN_SQL = text("""
WITH den AS (
  SELECT r.id AS rid, r.facility_id, r.claim_id, r.payer_id, r.remittance_date AS ra_date,
         r.created_at AS received_at, r.tpa_comments,
         d.activity_id, d.activity_code,
         COALESCE(NULLIF(d.denial_code,''), r.denial_code) AS denial_code,
         COALESCE(NULLIF(d.denial_description,''), r.denial_description) AS denial_desc,
         GREATEST(COALESCE(d.net,0) - COALESCE(d.paid_amount,0), 0) AS denied
  FROM remittances r
  JOIN remittance_activity_details d ON d.remittance_id_fk = r.id
  WHERE r.facility_id = :f AND r.remittance_date >= :since
    AND COALESCE(d.net,0) - COALESCE(d.paid_amount,0) > 0.009
)
SELECT den.*,
       c.encounter_start::date AS treatment_date, c.receiver_id, c.receiver_name, c.payer_name,
       c.clinician_name, c.member_id, c.payer_id AS claim_payer_id, c.submission_date::date AS first_sub,
       (SELECT count(*) FROM claim_activities a WHERE a.claim_id_fk = c.id) AS claim_activities,
       c.net_amount AS claim_net,
       (SELECT a.authorization_id FROM claim_activities a
         WHERE a.claim_id_fk = c.id AND a.activity_id = den.activity_id LIMIT 1) AS authorization_id,
       EXISTS (SELECT 1 FROM claims x WHERE x.facility_id = den.facility_id AND x.claim_id = den.claim_id
               AND x.resubmission_key <> '' AND x.submission_date::date >= den.ra_date) AS resub_after,
       EXISTS (SELECT 1 FROM claims x WHERE x.facility_id = den.facility_id AND x.claim_id = den.claim_id
               AND x.resubmission_key <> '' AND x.submission_date::date < den.ra_date) AS resub_before,
       (SELECT count(*) FROM claims x WHERE x.facility_id = den.facility_id AND x.claim_id = den.claim_id
               AND x.resubmission_key <> '' AND x.submission_date::date < den.ra_date) AS resubs_before
FROM den
LEFT JOIN LATERAL (SELECT * FROM claims c WHERE c.facility_id = den.facility_id AND c.claim_id = den.claim_id
                   AND c.resubmission_key = '' ORDER BY c.id LIMIT 1) c ON true
""")

# Reconciliation (the 2nd and last resubmission) has no 21-day clock (SN 03/2019 3.10.4): it is allowed only
# after the facility and the payer agree (3.10.1), within the 24-month claim validity (3.10.5). Eligible
# claims are kept from RECON_SINCE onward, not just the window.
RECON_SINCE = date(2026, 1, 1)
_RECON_SQL = text("SELECT * FROM (" + _OPEN_SQL.text + ") q WHERE q.resub_before AND NOT q.resub_after")


RECON_REMARKS = {
    "Medical necessity": "As the activity was rendered based on the clinical presentation, requesting your "
                         "consideration to settle on a B to B basis. Awaiting your confirmation to proceed with "
                         "Resub under Reconciliation.",
    "Contract & tariff": "The service was billed as per the agreed contract and tariff. Requesting your review and "
                         "settlement on a B to B basis. Awaiting your confirmation to proceed with Resub under "
                         "Reconciliation.",
    "Prior approval": "The service was rendered in good faith based on the eligibility and approval status at the "
                      "time of the visit. Requesting your consideration to settle on a B to B basis. Awaiting your "
                      "confirmation to proceed with Resub under Reconciliation.",
    "Consultation visit rules": "The consultation was rendered as clinically required for the presentation. "
                                "Requesting your consideration to settle on a B to B basis. Awaiting your "
                                "confirmation to proceed with Resub under Reconciliation.",
}
RECON_REMARKS["Coverage / eligibility"] = RECON_REMARKS["Prior approval"]
RECON_DEFAULT = ("Requesting your review of the denial and settlement on a B to B basis. Awaiting your "
                 "confirmation to proceed with Resub under Reconciliation.")

_names_cache: Dict[date, tuple] = {}


def party_names(db: Session):
    """Payer / receiver id -> most common name seen on any claim."""
    if date.today() in _names_cache:
        return _names_cache[date.today()]
    def lookup(col):
        return {r[0]: r[1] for r in db.execute(text(f"""
            SELECT DISTINCT ON (upper({col}_id)) upper({col}_id), {col}_name
            FROM (SELECT {col}_id, {col}_name, count(*) n FROM claims
                  WHERE nullif({col}_name,'') IS NOT NULL GROUP BY 1, 2) t
            ORDER BY upper({col}_id), n DESC"""))}
    _names_cache.clear()
    _names_cache[date.today()] = (lookup("payer"), lookup("receiver"))
    return _names_cache[date.today()]


def tracking_key(claim_id, activity_id, ra_date) -> str:
    return f"{claim_id}|{activity_id}|{ra_date}"


# ── Consultation repeat / follow-up check ────────────────────────────────────
# Same condition = same primary diagnosis (first 3 chars, Z-codes excluded) or a
# related diagnosis in the same ICD block (same letter + tens digit, e.g. J00–J09),
# seen by the same doctor or a doctor of the same specialty.
#   ≤7 days  → follow-up (free)              → close, do not resubmit
#   8–28 days→ repeat visit (billable)        → recode 9/10/11 → 9.02/10.02/11.02 at the repeat rate
#   otherwise→ new visit                      → resubmit; payer denial is wrong
# Single source of truth for the follow-up / repeat rule (used by presub_queries, scrub_rules, claim_scrubber)
CONSULT_BASES = ("9", "10", "11")
FOLLOWUP_SUFFIX, REPEAT_SUFFIX = ".01", ".02"     # .01 = free follow-up (net 0), .02 = repeat consultation
CONSULT_RE = re.compile(r"^(9|10|11)(\.\d+)?$")
FOLLOWUP_DAYS, REPEAT_DAYS = 7, 28


def consult_base(code: str) -> Optional[str]:
    """'10.02' -> '10'; None when not a consultation code."""
    c = (code or "").strip()
    return c.split(".")[0] if CONSULT_RE.match(c) else None


def visit_tier(gap_days: int) -> Optional[str]:
    """'followup' (1–7 days), 'repeat' (8–28 days) or None (new visit / same day)."""
    if gap_days is None or gap_days <= 0:
        return None
    if gap_days <= FOLLOWUP_DAYS:
        return "followup"
    if gap_days <= REPEAT_DAYS:
        return "repeat"
    return None


def expected_code(code: str, gap_days: int) -> Optional[str]:
    """The consultation code the tier requires, or None when the line already matches / no tier applies."""
    base = consult_base(code)
    tier = visit_tier(gap_days)
    if not base or not tier:
        return None
    want = base + (FOLLOWUP_SUFFIX if tier == "followup" else REPEAT_SUFFIX)
    return None if code.strip() == want else want


def same_condition(dx: str, prev_dx3s) -> bool:
    """Same 3-character diagnosis or the same ICD block (letter + first digit)."""
    dx = (dx or "")[:3].upper()
    if not dx:
        return False
    prev = [x.upper() for x in (prev_dx3s or []) if x]
    return dx in prev or any(_block(x) == _block(dx) for x in prev if _block(x))


def same_clinician(a, b) -> bool:
    return bool(norm_licence(a)) and norm_licence(a) == norm_licence(b)
# Apollo NAS (TPA004) / Neuron (TPA001) agreed repeat rates by claimed initial amount
APOLLO_NETWORK = {
    "10": [((120, 135, 150), 100), ((54, 60), 40), ((40.5, 45, 48), 32), ((36,), 28)],
    "9":  [((80, 90, 100), 75), ((36, 40.5, 45, 48, 50), 28)],
}
_spec_cache: Dict[str, str] = {}


ASSET_DIRS = ("~/ghaf-report-scripts/assets", "~/Desktop/report-scripts/assets")


def asset_path(filename: str) -> str:
    """Lookup files kept with the report scripts (outside the repo). The home-folder copy comes first because
    background services (launchd) cannot read the Desktop."""
    import os
    for d in ASSET_DIRS:
        p = os.path.join(os.path.expanduser(d), filename)
        if os.path.exists(p):
            return p
    return os.path.join(os.path.expanduser(ASSET_DIRS[0]), filename)


def _specialties() -> Dict[str, str]:
    """Clinician licence -> specialty, from the eClaimLink clinician list kept with the report scripts."""
    if _spec_cache.get("__loaded__"):
        return _spec_cache
    _spec_cache["__loaded__"] = "1"
    import csv, os
    for fn in ("eclaimlink_clinicians.csv", "clinician_names.csv"):
        try:
            with open(asset_path(fn), newline="") as f:
                for r in csv.DictReader(f):
                    sp = (r.get("specialty") or "").strip()
                    if sp:
                        _spec_cache[norm_licence(r["license"])] = sp
        except OSError:
            pass
    return _spec_cache


def _block(dx: str) -> str:
    m = re.match(r"([A-Z])(\d)", dx or "")
    return m.group(1) + m.group(2) if m else ""


_VISITS_SQL = text("""
SELECT c.id, c.claim_id, c.patient_id, coalesce(c.member_id,'') AS member_id, c.encounter_start::date AS d,
       a.activity_id, a.activity_code AS code, a.clinician_id AS doc, a.net,
       (SELECT array_agg(DISTINCT upper(left(x.diagnosis_code,3))) FROM claim_diagnoses x WHERE x.claim_id_fk = c.id) AS dx3s,
       (SELECT upper(x.diagnosis_code) FROM claim_diagnoses x WHERE x.claim_id_fk = c.id ORDER BY x.sequence_no LIMIT 1) AS pdx
FROM claims c JOIN claim_activities a ON a.claim_id_fk = c.id AND a.activity_code ~ '^(9|10|11)(\\.\\d+)?$'
WHERE c.facility_id = :f AND c.resubmission_key = '' AND c.encounter_start >= :d0 AND c.encounter_start < :d1
""")
_RATES_SQL = text("""
SELECT c.receiver_id, a.activity_code AS code, d.paid_amount AS paid, count(*) AS n
FROM remittance_activity_details d JOIN remittances r ON r.id = d.remittance_id_fk
JOIN claims c ON c.facility_id = r.facility_id AND c.claim_id = r.claim_id AND c.resubmission_key = ''
JOIN claim_activities a ON a.claim_id_fk = c.id AND a.activity_id = d.activity_id
WHERE r.facility_id = :f AND d.paid_amount > 0 AND a.activity_code IN ('9','10','11','9.02','10.02','11.02')
GROUP BY 1, 2, 3
""")


_ctx_cache: Dict[tuple, tuple] = {}


class ConsultContext:
    """Consultation visits and paid-rate history for one facility, loaded once per worklist build."""

    def __init__(self, db: Session, fid: int, d0: date, d1: date):
        self.fid = fid
        self.by_claim: Dict[tuple, object] = {}
        self.by_patient: Dict[str, list] = defaultdict(list)
        for v in db.execute(_VISITS_SQL, {"f": fid, "d0": d0, "d1": d1 + timedelta(days=1)}):
            self.by_claim[(v.claim_id, v.activity_id)] = v
            self.by_claim.setdefault((v.claim_id, None), v)
            if v.patient_id:
                self.by_patient["P:" + v.patient_id].append(v)
            if v.member_id:
                self.by_patient["M:" + v.member_id].append(v)
        self.paid = defaultdict(lambda: defaultdict(int))     # (receiver, code) -> {amount: n}
        for r in db.execute(_RATES_SQL, {"f": fid}):
            self.paid[(r.receiver_id, r.code)][round(float(r.paid), 2)] += r.n
        self.rate_cache: dict = {}

    def previous(self, cur) -> list:
        seen, out = set(), []
        for key in ("P:" + (cur.patient_id or ""), "M:" + (cur.member_id or "")):
            for v in self.by_patient.get(key, []):
                if v.id == cur.id or v.id in seen:
                    continue
                if cur.d - timedelta(days=REPEAT_DAYS) <= v.d <= cur.d:
                    seen.add(v.id); out.append(v)
        return sorted(out, key=lambda v: v.d, reverse=True)

    def repeat_rate(self, receiver: str, base: str, claimed: float):
        """(rate, basis, is_assumption) for the repeat code of `base` (9/10/11)."""
        if self.fid == 13 and receiver in ("TPA004", "TPA001") and base in APOLLO_NETWORK:
            for amounts, rate in APOLLO_NETWORK[base]:
                if any(abs(claimed - x) < 0.01 for x in amounts):
                    return rate, f"Apollo network rate for {base} @ {claimed:g}", False
        key = (receiver, base)
        if key not in self.rate_cache:
            mode = lambda d: max(d.items(), key=lambda kv: kv[1])[0] if d else None
            rep, ini = mode(self.paid.get((receiver, f"{base}.02"), {})), mode(self.paid.get((receiver, base), {}))
            if rep is not None:
                self.rate_cache[key] = (rep, f"usual amount paid by {receiver} for {base}.02", False)
            elif ini is not None:
                self.rate_cache[key] = (round(ini / 2, 2), f"ASSUMPTION – {receiver} never paid a {base}.02; "
                                                           f"50% of its usual {base} payment", True)
            else:
                self.rate_cache[key] = (28.0, f"ASSUMPTION – no {base} / {base}.02 payment history with "
                                              f"{receiver}; AED 28 default", True)
        return self.rate_cache[key]


def _ev(p, cur, gap):
    return {"prev_claim": p.claim_id if p else None, "prev_date": p.d.isoformat() if p else None,
            "prev_code": p.code if p else None, "prev_dx": p.pdx if p else None, "gap_days": gap, "dx": cur.pdx}


def consult_check(ctx: ConsultContext, item: dict, other_denied: List[str]) -> Optional[dict]:
    code = (item["activity_code"] or "").strip()
    if not CONSULT_RE.match(code):
        return None
    cur = ctx.by_claim.get((item["claim_id"], item["activity_id"])) or ctx.by_claim.get((item["claim_id"], None))
    if not cur:
        return None
    spec = _specialties()
    dx = (cur.pdx or "")[:3]
    my_spec = spec.get(norm_licence(cur.doc))
    base = code.split(".")[0]
    others = (" Other denied lines on this claim: " + ", ".join(other_denied) + " — review and resubmit together."
              if other_denied else "")
    close = lambda verdict, why, ev: {"verdict": verdict, "consult_action": "close", "evidence": ev,
                                      "action": f"Close — do not resubmit. {why}.", "draft_comment": None,
                                      "recode": None, "repeat_rate": None, "rate_basis": None}
    resub = lambda verdict, why, comment, ev: {"verdict": verdict, "consult_action": "resubmit", "evidence": ev,
                                               "action": f"Resubmit — {why}.{others}", "draft_comment": comment,
                                               "recode": None, "repeat_rate": None, "rate_basis": None}
    prev = ctx.previous(cur)
    unknown_spec = None
    for p in prev:
        gap = (cur.d - p.d).days
        if not dx or dx.startswith("Z"):
            continue
        same_dx = dx in (p.dx3s or [])
        related = not same_dx and any(_block(x) == _block(dx) for x in (p.dx3s or []) if _block(x))
        if not (same_dx or related):
            continue
        same_doc = same_clinician(cur.doc, p.doc)
        p_spec = spec.get(norm_licence(p.doc))
        same_spec = bool(my_spec) and my_spec == p_spec
        if not (same_doc or same_spec):
            if not (my_spec and p_spec) and unknown_spec is None:
                unknown_spec = (p, gap, same_dx)
            continue
        why = (f"{'same' if same_dx else 'related'} diagnosis ({p.pdx} → {cur.pdx}), "
               f"{'same doctor' if same_doc else 'same specialty (' + my_spec + ')'}, "
               f"{gap} days after {p.claim_id} on {p.d:%d/%m/%Y}")
        if gap <= FOLLOWUP_DAYS:
            return close("Follow-up (≤7 days) — not billable", f"Follow-up visit: {why}", _ev(p, cur, gap))
        if code.endswith(".02"):
            return resub("Repeat visit — already billed as repeat code",
                         f"{code} is the correct repeat code ({why}); the payer denial is wrong",
                         f"This visit is a repeat consultation {gap} days after the initial visit ({p.claim_id}, "
                         f"{p.d:%d/%m/%Y}) and is correctly billed under the repeat consultation code {code}. "
                         f"Kindly review and settle.", _ev(p, cur, gap))
        rate, basis, assumed = ctx.repeat_rate(item.get("receiver_id") or "", base, float(cur.net or 0))
        return {"verdict": "Repeat visit (8–28 days) — recode", "consult_action": "recode", "evidence": _ev(p, cur, gap),
                "action": f"Change activity code {code} → {base}.02 and the net to AED {rate:g} (repeat consultation "
                          f"rate{' – ASSUMPTION, confirm with contract' if assumed else ''}), then resubmit. "
                          f"Repeat visit: {why}.{others}",
                "draft_comment": f"Activity corrected to repeat consultation code {base}.02 at the repeat consultation "
                                 f"rate, as the visit is {gap} days after the initial consultation ({p.claim_id}, "
                                 f"{p.d:%d/%m/%Y}) for the same condition. Kindly review and settle.",
                "recode": f"{base}.02", "repeat_rate": rate, "rate_basis": basis, "rate_assumed": assumed}
    if unknown_spec:
        p, gap, same_dx = unknown_spec
        return {"verdict": "Same condition, different doctor — check specialty", "consult_action": "check",
                "evidence": _ev(p, cur, gap),
                "action": f"Check the department of both doctors ({p.doc} on {p.d:%d/%m/%Y} and {cur.doc}). Same "
                          f"{'diagnosis' if same_dx else 'related diagnosis'} ({p.pdx} → {cur.pdx}), {gap} days apart. "
                          f"If same specialty: {'close (follow-up ≤7 days)' if gap <= FOLLOWUP_DAYS else f'recode to {base}.02 at the repeat rate'}; "
                          f"if a different specialty: resubmit as a new specialist consultation.{others}",
                "draft_comment": None, "recode": None, "repeat_rate": None, "rate_basis": None}
    if prev:
        p = prev[0]; gap = (cur.d - p.d).days
        same_cond = dx and (dx in (p.dx3s or []) or any(_block(x) == _block(dx) for x in (p.dx3s or []) if _block(x)))
        if same_cond:   # same condition but a different specialty → a new specialist consultation
            sp = spec.get(norm_licence(p.doc))
            return resub("Different specialty — not a repeat",
                         f"not a repeat: previous visit {p.claim_id} on {p.d:%d/%m/%Y} was with {sp}, this visit is with {my_spec}",
                         f"This consultation is with a different specialty ({my_spec}) from the previous visit on "
                         f"{p.d:%d/%m/%Y} ({sp}); it is a new specialist consultation, not a repeat or follow-up. "
                         f"Kindly review and settle.", _ev(p, cur, gap))
        return resub("New condition — not a repeat",
                     f"not a repeat: previous consultation {p.claim_id} on {p.d:%d/%m/%Y} ({gap} days earlier) was "
                     f"for {p.pdx}, this visit is for an unrelated condition {cur.pdx}",
                     f"This is a new consultation for a different condition: the previous visit on {p.d:%d/%m/%Y} "
                     f"was for {p.pdx}, this visit is for {cur.pdx}. It is not a repeat or follow-up visit. "
                     f"Kindly review and settle.", _ev(p, cur, gap))
    return resub("No visit in previous 28 days — payer denial wrong",
                 "not a repeat: no consultation for this patient at the facility in the previous 28 days; "
                 "the payer comment is wrong",
                 "There is no consultation for this patient in the 28 days before this visit; it is a new "
                 "consultation, not a repeat or follow-up. Kindly review and settle.", _ev(None, cur, None))


def build_worklist(db: Session, fid: int, window: int = WINDOW_DAYS, recon_history: bool = True) -> dict:
    """Returns {'items': [...], 'overall_rate': float}. Items carry a 'bucket':
    open | closed_resubmitted | closed_team | expired."""
    today = date.today()
    match = _matcher(ensure_playbook(db))
    rate, overall = recovery_model(db, fid)
    payer_names, receiver_names = party_names(db)

    tracking = {tracking_key(t.claim_id, t.activity_id, t.ra_date): t
                for t in db.query(ResubTracking).filter(ResubTracking.facility_id == fid,
                                                        ResubTracking.ra_date >= min(RECON_SINCE, today - timedelta(days=window + 8)))}
    since = today - timedelta(days=window + 7)
    rows = db.execute(_OPEN_SQL, {"f": fid, "since": since}).fetchall()
    if recon_history and RECON_SINCE < since:   # full 2026 reconciliation list (screen, month-end pack)
        seen = {(r.claim_id, r.activity_id, r.ra_date) for r in rows}
        rows += [r for r in db.execute(_RECON_SQL, {"f": fid, "since": RECON_SINCE}).fetchall()
                 if r.ra_date < since and (r.claim_id, r.activity_id, r.ra_date) not in seen]
    new_cut = datetime.now(timezone.utc) - timedelta(hours=30)

    items = []
    for r in rows:
        code = norm_code(r.denial_code)
        pb = match(code, (r.activity_code or "").strip(), f"{r.denial_desc or ''} {r.tpa_comments or ''}")
        deadline = r.ra_date + timedelta(days=window)
        days_left = (deadline - today).days
        p = rate(r.receiver_id, code)
        denied = float(r.denied or 0)
        k = tracking_key(r.claim_id, r.activity_id, r.ra_date)
        t = tracking.get(k)
        status = t.status if t else "New"
        reconciliation = bool(r.resub_before)

        if r.resub_after:
            bucket, status = "closed_resubmitted", "Resubmitted"
        elif (r.resubs_before or 0) >= 2:
            # SN 03/2019 3.8.2 / 3.8.5: two resubmissions (incl. reconciliation) already used — no further resubmission
            bucket, status = "closed_team", "SN limit reached (2 resubmissions)"
        elif status in CLOSED_BY_TEAM:
            bucket = "closed_team"
        elif reconciliation and status == RECON_AGREED:
            # facility confirmed mutual agreement with the payer: Ghaf resubmits as type Reconciliation.
            # No SN time limit (3.10.4); the claim must still be within its 24-month validity (3.10.5).
            bucket = "open"
            deadline = _valid_until(r.first_sub, r.ra_date)
            days_left = (deadline - today).days
        elif reconciliation:
            bucket = "reconciliation"   # second denial: sent to the facility, who takes it up with the payer
            deadline = _valid_until(r.first_sub, r.ra_date)
            days_left = (deadline - today).days
        elif days_left < 0:
            bucket = "expired"
        else:
            bucket = "open"
        if bucket == "expired" and days_left < -7:
            continue

        owner = (t.owner_override if t and t.owner_override else (pb.owner if pb else "Ghaf"))
        exp = round(denied * p, 2)
        if bucket == "open":
            priority = ("P1" if days_left <= URGENT_DAYS and exp >= 20 else
                        "P2" if exp >= 100 or days_left <= URGENT_DAYS else "P3")
        else:
            priority = None
        playbook_owner = pb.owner if pb else "Ghaf"
        low_value = False
        if not (t and t.owner_override) and playbook_owner == "Facility":
            if reconciliation:
                # the facility takes reconciliation up with the payer; Ghaf resubmits once agreed
                owner = "Ghaf" if status == RECON_AGREED else "Facility"
            elif priority not in ("P1", "P2") or p * 100 < FACILITY_MIN_RECOVERY_PCT:
                owner, low_value = "Ghaf", True
        items.append({
            "key": k, "claim_id": r.claim_id, "activity_id": r.activity_id, "ra_date": r.ra_date.isoformat(),
            "received_at": r.received_at.isoformat() if r.received_at else None,
            "is_new": bool(r.received_at and r.received_at >= new_cut and t is None),
            "treatment_date": r.treatment_date.isoformat() if r.treatment_date else None,
            "deadline": deadline.isoformat(), "days_left": days_left, "priority": priority,
            "receiver_id": r.receiver_id,
            "receiver": " – ".join(x for x in (r.receiver_id, r.receiver_name
                                              or receiver_names.get((r.receiver_id or "").upper())) if x),
            "payer_id": r.claim_payer_id,
            "payer": " – ".join(x for x in (r.claim_payer_id, r.payer_name
                                           or payer_names.get((r.claim_payer_id or "").upper())) if x) or None,
            "authorization_id": r.authorization_id,
            "claim_activities": r.claim_activities, "claim_net": float(r.claim_net or 0), "clinician": r.clinician_name, "member_id": r.member_id,
            "activity_code": r.activity_code, "denial_code": code,
            "denial_reason": (r.denial_desc or r.tpa_comments or "")[:400],
            "_full_reason": " ".join(x for x in (r.denial_desc, r.tpa_comments) if x),
            "denied": round(denied, 2), "recovery_pct": round(p * 100), "expected_recovery": exp,
            "category": pb.category if pb else "Other", "owner": owner,
            "facility_role": pb.facility_role if pb else None,
            "route": "Reconciliation" if reconciliation else "Resubmission",
            "recon_remark": (RECON_REMARKS.get(pb.category if pb else "Other", RECON_DEFAULT)
                             if reconciliation else None),
            "valid_until": deadline.isoformat() if reconciliation else None,
            "action": (("Facility confirmed agreement with the payer — resubmit this claim under resubmission type "
                        "'Reconciliation'. No SN time limit (3.10.4); the claim must be within its 24-month validity.")
                       if reconciliation and status == RECON_AGREED else
                       ("Denied again after resubmission — reconciliation. Ghaf sends it to the facility in the "
                        "month-end reconciliation pack; the facility takes it up with the payer and, once mutually "
                        "agreed, informs Ghaf to resubmit under type 'Reconciliation'.")
                       if reconciliation else LOW_VALUE_ACTION if low_value else (pb.action if pb else "")),
            "facility_request": pb.facility_request if (pb and owner == "Facility") else None,
            "documents": pb.documents if pb else None,
            "draft_comment": pb.draft_comment if pb else None,
            "status": status, "bucket": bucket, "low_value": low_value,
            "assigned_to": t.assigned_to if t else None, "remarks": t.remarks if t else None,
            "facility_reply": t.facility_reply if t else None,
            "updated_by": t.updated_by if t else None,
            "updated_at": t.updated_at.isoformat() if t and t.updated_at else None,
        })

    # category from the payer's own reason for each activity (claim-level reasons name the codes per part)
    from .denial_category import classify
    for x in items:
        cat, txt, elsewhere = classify(x.pop("_full_reason", None) or x["denial_reason"], x["activity_code"] or "", x["category"], x["denial_code"] or "")
        x["line_reason"] = txt
        x["cited_elsewhere"] = elsewhere
        if cat != x["category"]:
            x["category_original"], x["category"] = x["category"], cat
            x["category_note"] = ("Payer reason names other activities, not this one." if elsewhere
                                  else f"From the payer reason for this activity: {txt[:160]}")
            t = CATEGORY_TEMPLATE.get(cat)
            if t and x["bucket"] != "reconciliation" and not x.get("consult"):
                x["action"], x["facility_request"], x["documents"], x["draft_comment"] = t
                x["facility_role"] = None if t[1] is None else x.get("facility_role")

    # consultation repeat / follow-up verdicts (open items only)
    denied_by_claim = defaultdict(list)
    for x in items:
        if x["bucket"] == "open":
            denied_by_claim[x["claim_id"]].append(x)
    targets = [x for x in items if x["bucket"] in ("open", "reconciliation") and x["category"] == "Consultation visit rules"
               and CONSULT_RE.match((x["activity_code"] or "").strip())]
    ctx = None
    if targets:
        tds = [date.fromisoformat(x["treatment_date"]) for x in targets if x["treatment_date"]]
        if tds:
            d0, d1 = min(tds) - timedelta(days=REPEAT_DAYS), max(tds)
            cached = _ctx_cache.get((fid, date.today()))
            if cached and cached[0] <= d0 and cached[1] >= d1:
                ctx = cached[2]
            else:
                ctx = ConsultContext(db, fid, d0, d1)
                for k in [k for k in _ctx_cache if k[1] != date.today()]:
                    del _ctx_cache[k]
                _ctx_cache[(fid, date.today())] = (d0, d1, ctx)
    for x in items:
        x.setdefault("consult", None)
    for x in targets if ctx else []:
        others = [f"{o['activity_code']} ({o['denial_code']})" for o in denied_by_claim[x["claim_id"]]
                  if o["key"] != x["key"]]
        chk = consult_check(ctx, x, others)
        if not chk:
            continue
        x["consult"] = chk
        if x["bucket"] == "reconciliation":
            continue          # recon_review decides the outcome and the remark
        x["action"] = chk["action"]
        x["draft_comment"] = chk["draft_comment"]
        if chk["consult_action"] == "close":
            x["expected_recovery"] = 0.0
            x["priority"] = "P3"
        elif chk["consult_action"] == "recode":
            x["expected_recovery"] = round(min(x["denied"], chk["repeat_rate"]), 2)

    from . import recon_review
    recon_review.review(db, fid, items)
    recon_review.fix_open_clai012(items)

    order = {"P1": 0, "P2": 1, "P3": 2, None: 3}
    items.sort(key=lambda x: (order[x["priority"]], x["days_left"], -x["expected_recovery"]))
    return {"items": items, "overall_rate": round(overall * 100), "window_days": window}


def summarize(items: List[dict]) -> dict:
    def agg(rs):
        return {"count": len(rs), "denied": round(sum(x["denied"] for x in rs), 2),
                "expected": round(sum(x["expected_recovery"] for x in rs), 2)}
    open_ = [x for x in items if x["bucket"] == "open"]
    return {
        "new": agg([x for x in open_ if x["is_new"]]),
        "open": agg(open_),
        "urgent": agg([x for x in open_ if x["days_left"] <= URGENT_DAYS]),
        "ghaf": agg([x for x in open_ if x["owner"] == "Ghaf"]),
        "facility": agg([x for x in open_ if x["owner"] == "Facility"]),
        "resubmitted_7d": agg([x for x in items if x["bucket"] == "closed_resubmitted"]),
        "expired_7d": agg([x for x in items if x["bucket"] == "expired"]),
        "reconciliation": agg([x for x in items if x["bucket"] == "reconciliation"]),
    }


def save_tracking(db: Session, fid: int, updates: List[dict], user_email: str) -> int:
    n = 0
    for u in updates:
        claim_id, activity_id, ra_date = u["claim_id"], u["activity_id"], date.fromisoformat(u["ra_date"])
        t = (db.query(ResubTracking)
             .filter_by(facility_id=fid, claim_id=claim_id, activity_id=activity_id, ra_date=ra_date).first())
        if not t:
            t = ResubTracking(facility_id=fid, claim_id=claim_id, activity_id=activity_id, ra_date=ra_date)
            db.add(t)
        for f in ("status", "owner_override", "assigned_to", "remarks", "facility_reply"):
            if f in u:
                v = u[f]
                if f == "status" and v not in STATUSES:
                    raise ValueError(f"Unknown status: {v}")
                if f == "owner_override" and v not in (None, "", "Ghaf", "Facility"):
                    raise ValueError(f"Unknown owner: {v}")
                setattr(t, f, v or None if f != "status" else v)
        t.updated_by = user_email
        n += 1
    db.commit()
    return n


# ── Excel export (Ghaf team / facility) ───────────────────────────────────────
# action, facility request, documents, draft comment for categories decided from the payer reason
CATEGORY_TEMPLATE: Dict[str, tuple] = {}
for _p in DEFAULT_PLAYBOOK:
    CATEGORY_TEMPLATE.setdefault(_p[1], (_p[4], _p[5], _p[6], _p[7]))
CATEGORY_TEMPLATE.update({
    "Clinician licence": ("Clinician licence denial: facility to update and activate the doctor in JetNAS / the payer "
                          "portal, then resubmit with the licence copy.",
                          "Update and activate the treating doctor in JetNAS / the payer portal and share the DHA licence "
                          "copy valid on the date of service.", "DHA licence copy, portal activation confirmation",
                          "The clinician licence is active during the service date; the licence copy is attached. Kindly "
                          "review and settle."),
    "GP referral": ("GP referral denial: facility to share the GP referral for this visit; resubmit with it. If none "
                    "exists, close as write-off.", "Share the GP referral letter for this visit.", "GP referral letter",
                    "The patient was referred by a GP for this condition; the referral is attached. Kindly review and settle."),
    "Late submission": ("Late submission / time-barred: payer is correct — close as write-off.", None, None, None),
    "Duplicate — already paid": ("Already paid in the earlier submission — close; nothing further due.", None, None, None),
    "Patient share": ("Patient share / deductible: correct the patient share or collect from the patient.", None,
                      "Patient share calculation", None),
    "Coding — panel composition": ("Panel billed without all required components: payer pays components only — close "
                                   "and bill components in future.", None, None, None),
    "Resubmission type error": ("Resubmission not compliant with the resubmission type (e.g. resubmitted amount above the "
                                "denied amount on an internal complaint). Correct the type / amount and resubmit.",
                                None, "Corrected resubmission", "Resubmitted with the correct resubmission type and the "
                                "denied amount only. Kindly review and settle."),
    "Recalled by provider": ("Claim recalled / cancelled on the facility's request — no action unless it was recalled "
                             "by mistake.", None, None, None),
    "Wrong receiver": ("Submitted to the wrong receiver — resubmit the claim to the receiver responsible for this payer.",
                       None, None, "Claim submitted to the correct receiver for this payer. Kindly review and settle."),
    "Medical necessity — alternative service": (
        "MNEC-006 — alternative service: the payer says another service (e.g. oral instead of injection) should have "
        "been used. Doctor to justify the choice of service; resubmit with it, or close.",
        "Treating doctor to state why this service was chosen over the alternative (e.g. why injection, not oral).",
        "Clinical notes", "The service chosen was clinically required (oral route not suitable / urgent relief "
        "needed), as documented in the attached notes. Kindly review and settle."),
    "Medical necessity — frequency": ("Test denied as too frequent: check the previous test date and result; resubmit "
                                      "with the reason to repeat (change in condition / treatment monitoring), or close.",
                                      "Treating doctor to state why the test was repeated (previous result, change in "
                                      "condition or treatment).", "Previous and current results, clinical notes",
                                      "The test was repeated for a documented clinical reason (previous result / change in "
                                      "condition), as per the attached notes. Kindly review and settle."),
})


def _category_block(ws, r0: int, rows: List[dict], title: str, WB, TEAL, Font) -> int:
    """Per-category summary (lines, denied, recoverability split) at row r0; returns the last row written."""
    ws.cell(r0, 1, title).font = Font(bold=True, color="1E6B75")
    hdr = ["Category", "Lines", "Denied (AED)", "High", "Medium", "Low (less recoverable)", "Write-off / collect (AED)"]
    for j, h in enumerate(hdr, 1):
        c = ws.cell(r0 + 1, j, h); c.font = WB; c.fill = TEAL
    agg = defaultdict(lambda: [0, 0.0, 0, 0, 0, 0.0])
    for x in rows:
        a = agg[x.get("category") or "Other"]
        a[0] += 1; a[1] += x["denied"]
        lvl = x.get("recon_recoverability")
        if lvl == "High": a[2] += 1
        elif lvl == "Medium": a[3] += 1
        elif lvl == "Low": a[4] += 1
        if x.get("recon_outcome") in ("Write-off", "Collect from patient"): a[5] += x["denied"]
    r = r0 + 1
    for cat, a in sorted(agg.items(), key=lambda kv: -kv[1][1]):
        r += 1
        for j, v in enumerate([cat, a[0], round(a[1], 2), a[2], a[3], a[4], round(a[5], 2)], 1):
            c = ws.cell(r, j, v)
            if j in (3, 7): c.number_format = "#,##0.00"
    r += 1
    tot = [sum(a[i] for a in agg.values()) for i in range(6)]
    for j, v in enumerate(["Total", tot[0], round(tot[1], 2), tot[2], tot[3], tot[4], round(tot[5], 2)], 1):
        c = ws.cell(r, j, v); c.font = Font(bold=True)
        if j in (3, 7): c.number_format = "#,##0.00"
    return r


def to_workbook(facility_name: str, data: dict, audience: str = "ghaf"):
    """audience='ghaf'     → full worklist for the Ghaf resubmission team
       audience='facility' → only items needing facility input, phrased as requests"""
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    TEAL = PatternFill("solid", fgColor="1E6B75"); GREEN = PatternFill("solid", fgColor="8BC34A")
    URG = PatternFill("solid", fgColor="FDE2C4"); NEWF = PatternFill("solid", fgColor="E3F1F3")
    DONE = PatternFill("solid", fgColor="E8F5E9"); GREY = PatternFill("solid", fgColor="EEEEEE")
    THIN = Border(bottom=Side(style="thin", color="DDDDDD"))
    WB = Font(bold=True, color="FFFFFF")
    WIDE = {"Denial Reason", "Action", "Rate Basis", "Suggested Remark to Payer",
            "Outcome with Payer (Agreed / Not agreed, date)", "What We Need From You", "Documents to Attach",
            "Draft Resubmission Comment", "Remarks", "Facility Reply"}

    items = data["items"]
    # denied activities per claim (same RA) vs activities claimed
    per_claim = defaultdict(list)
    for x in items:
        per_claim[(x["claim_id"], x["ra_date"], x["bucket"])].append(x)
    items = [x | {"denied_of": f"{len(per_claim[(x['claim_id'], x['ra_date'], x['bucket'])])} of "
                              f"{x.get('claim_activities') or '?'}"} for x in items]
    open_ = [x for x in items if x["bucket"] == "open"]
    wb = openpyxl.Workbook()

    if audience == "reconciliation":
        cols = [("Claim ID", "claim_id"), ("Activity ID", "activity_id"), ("Denied / Claimed Activities", "denied_of"),
                ("Treatment Date", "treatment_date"), ("RA Date", "ra_date"), ("Receiver", "receiver"), ("Payer", "payer"), ("Member ID", "member_id"),
                ("Auth ID", "authorization_id"), ("Clinician", "clinician"), ("Activity Code", "activity_code"),
                ("Denial Code", "denial_code"), ("Denial Reason", "denial_reason"), ("Denied (AED)", "denied"),
                ("Category", "category"), ("Claim Valid Until (24 months)", "valid_until"),
                ("Suggested Remark to Payer", "recon_remark"),
                ("Outcome with Payer (Agreed / Not agreed, date)", "facility_reply")]
        recon = [x for x in items if x["bucket"] == "reconciliation"]
        by_rcv = defaultdict(list)
        for x in recon:
            by_rcv[x["receiver"] or "—"].append(x)
        sheets = [("All Reconciliation", sorted(recon, key=lambda x: (x["receiver"] or "", x["claim_id"])), lambda x: None)]
        for rcv, rs in sorted(by_rcv.items(), key=lambda kv: -sum(x["denied"] for x in kv[1])):
            sheets.append((re.sub(r"[\\/*?:\[\]]", "", rcv)[:31], rs, lambda x: None))
        team_from = len(cols) - 1
        intro = (f"{facility_name} — claims eligible for reconciliation (denied again after resubmission). "
                 f"Please take these up with the payer. Once mutually agreed, record the outcome and date in the "
                 f"last column and inform Ghaf — we will then resubmit under type 'Reconciliation'. "
                 f"As per DHA SN 03/2019, reconciliation needs agreement with the payer (3.10.1) and has no 21-day "
                 f"limit (3.10.4); claims stay valid for 24 months from submission (3.10.5).")
    elif audience == "facility":
        cols = [("Priority", "priority"), ("Days Left", "days_left"), ("Reply By", "reply_by"),
                ("Claim ID", "claim_id"), ("Activity ID", "activity_id"), ("Denied / Claimed Activities", "denied_of"),
                ("Treatment Date", "treatment_date"), ("Member ID", "member_id"), ("Clinician", "clinician"), ("Receiver", "receiver"), ("Payer", "payer"),
                ("Auth ID", "authorization_id"),
                ("Activity Code", "activity_code"), ("Denial Code", "denial_code"), ("Denial Reason", "denial_reason"),
                ("Denied (AED)", "denied"), ("Who", "facility_role"), ("What We Need From You", "facility_request"),
                ("Documents to Attach", "documents"), ("Facility Reply", "facility_reply")]
        rows = [x | {"reply_by": (date.fromisoformat(x["deadline"]) - timedelta(days=3)).isoformat()}
                for x in open_ if x["owner"] == "Facility" and x["status"] not in ("Facility replied", "Ready to resubmit")]
        sheets = [("Needs Your Input", rows, lambda x: URG if x["days_left"] <= URGENT_DAYS else None)]
        team_from = len(cols) - 1
        intro = (f"{facility_name} — claims needing your input for resubmission. "
                 f"Please reply by the 'Reply By' date so the Ghaf team can resubmit before the payer deadline.")
    else:
        cols = [("Priority", "priority"), ("Days Left", "days_left"), ("Deadline", "deadline"),
                ("Owner", "owner"), ("Claim ID", "claim_id"), ("Activity ID", "activity_id"),
                ("Denied / Claimed Activities", "denied_of"), ("RA Date", "ra_date"),
                ("Treatment Date", "treatment_date"), ("Receiver", "receiver"), ("Payer", "payer"),
                ("Auth ID", "authorization_id"),
                ("Clinician", "clinician"), ("Member ID", "member_id"), ("Activity Code", "activity_code"),
                ("Denial Code", "denial_code"), ("Denial Reason", "denial_reason"), ("Denied (AED)", "denied"),
                ("Payer Pays on Resub %", "recovery_pct"), ("Expected Recovery (AED)", "expected_recovery"),
                ("Category", "category"), ("Visit Check", "c_verdict"), ("Previous Claim", "c_prev"),
                ("Previous Visit", "c_prev_date"), ("Gap (days)", "c_gap"), ("Previous Dx", "c_prev_dx"), ("This Dx", "c_dx"),
                ("Recode To", "c_recode"), ("Repeat Rate (AED)", "c_rate"), ("Rate Basis", "c_basis"),
                ("Route", "route"), ("Action", "action"),
                ("What We Need From Facility", "facility_request"), ("Documents to Attach", "documents"),
                ("Draft Resubmission Comment", "draft_comment"),
                ("Status", "status"), ("Assigned To", "assigned_to"), ("Remarks", "remarks"),
                ("Facility Reply", "facility_reply")]
        team_from = len(cols) - 4
        closed = [x for x in items if x["bucket"] != "open"]
        sheets = [
            ("New Today", [x for x in open_ if x["is_new"]], lambda x: URG if x["days_left"] <= URGENT_DAYS else NEWF),
            ("Action List", open_, lambda x: URG if x["days_left"] <= URGENT_DAYS else (NEWF if x["is_new"] else None)),
            ("Needs Facility", [x for x in open_ if x["owner"] == "Facility"],
             lambda x: URG if x["days_left"] <= URGENT_DAYS else None),
            ("Closed", closed, lambda x: DONE if x["bucket"] != "expired" else GREY),
        ]
        intro = (f"{facility_name} — Resubmission Worklist (Ghaf team). Window {data['window_days']} days from RA date. "
                 f"Facility resubmission recovery rate: {data['overall_rate']}%. Update status in the Ghaf App.")

    sm = wb.active; sm.title = "Summary"
    sm["A1"] = intro; sm["A1"].font = Font(bold=True, size=12, color="1E6B75")
    sm["A2"] = f"Generated {datetime.now():%d %b %Y %H:%M}"; sm["A2"].font = Font(italic=True, color="666666")
    s = summarize(items)
    labels = ([("New denials (last night's RAs)", "new"), ("Open — window running", "open"),
               (f"Urgent — {URGENT_DAYS} days or less", "urgent"), ("Ghaf team", "ghaf"),
               ("Needs facility input", "facility"), ("Resubmitted (last 7 days)", "resubmitted_7d"),
               ("Expired without resubmission (last 7 days)", "expired_7d")]
              if audience == "ghaf" else [("Needs your input", None)] if audience == "facility" else [])
    for j, h in enumerate(["", "Activities", "Denied (AED)"], 1):
        c = sm.cell(4, j, h); c.font = WB; c.fill = TEAL
    for i, (lbl, k) in enumerate(labels):
        agg = s[k] if k else {"count": len(sheets[0][1]), "denied": round(sum(x["denied"] for x in sheets[0][1]), 2)}
        sm.cell(5 + i, 1, lbl); sm.cell(5 + i, 2, agg["count"])
        sm.cell(5 + i, 3, agg["denied"]).number_format = "#,##0.00"
    if audience == "reconciliation":
        rr = 5
        for rcv, rs in sorted(by_rcv.items(), key=lambda kv: -sum(x["denied"] for x in kv[1])):
            sm.cell(rr, 1, rcv); sm.cell(rr, 2, len(rs))
            sm.cell(rr, 3, round(sum(x["denied"] for x in rs), 2)).number_format = "#,##0.00"; rr += 1
        sm.cell(rr, 1, "Total").font = Font(bold=True); sm.cell(rr, 2, len(recon)).font = Font(bold=True)
        c = sm.cell(rr, 3, round(sum(x["denied"] for x in recon), 2)); c.font = Font(bold=True); c.number_format = "#,##0.00"
        sm.cell(4, 1).value = "Receiver"
        rr = _category_block(sm, rr + 2, recon, "By category — all receivers", WB, TEAL, Font)
    for col, w in zip("ABC", [48, 12, 16]): sm.column_dimensions[col].width = w

    for title, rows, fill_rule in sheets:
        ws = wb.create_sheet(title)
        head = 1
        if audience == "reconciliation" and title != "All Reconciliation":
            head = _category_block(ws, 1, rows, f"{title} — by category", WB, TEAL, Font) + 2
        if head > 1:
            for j, (h, _) in enumerate(cols, 1):
                ws.cell(head, j, h)
        else:
            ws.append([h for h, _ in cols])
        for i, c in enumerate(ws[head]):
            c.font = WB; c.fill = GREEN if i >= team_from else TEAL
            c.alignment = Alignment(wrap_text=True, vertical="center")
        ws.row_dimensions[head].height = 32
        for x in rows:
            c = x.get("consult") or {}
            ev = c.get("evidence") or {}
            x = x | {"c_verdict": c.get("verdict"), "c_prev": ev.get("prev_claim"), "c_prev_date": ev.get("prev_date"),
                     "c_gap": ev.get("gap_days"), "c_prev_dx": ev.get("prev_dx"), "c_dx": ev.get("dx"),
                     "c_recode": c.get("recode"), "c_rate": c.get("repeat_rate"), "c_basis": c.get("rate_basis")}
            vals = []
            for _, k in cols:
                v = x.get(k)
                if k in ("deadline", "ra_date", "treatment_date", "reply_by", "c_prev_date") and v:
                    v = date.fromisoformat(v)
                vals.append(v)
            ws.append(vals)
            f = fill_rule(x)
            if f:
                for c in ws[ws.max_row][:team_from]: c.fill = f
        for row in ws.iter_rows(min_row=head + 1):
            for c in row:
                c.border = THIN
                c.alignment = Alignment(vertical="top", wrap_text=cols[c.column - 1][0] in WIDE)
                if isinstance(c.value, date): c.number_format = "DD/MM/YYYY"
        ws.freeze_panes = f"E{head + 1}"
        ws.auto_filter.ref = f"A{head}:{get_column_letter(len(cols))}{max(ws.max_row, head)}"
        for i, (h, _) in enumerate(cols, 1):
            ws.column_dimensions[get_column_letter(i)].width = 50 if h in WIDE else max(10, min(24, len(h) + 3))
    if audience == "ghaf":
        ws = wb.create_sheet("By Claim", 1)
        hdr = ["Priority", "Days Left", "Deadline", "Claim ID", "RA Date", "Treatment Date", "Receiver", "Payer",
               "Activities Claimed", "Activities Denied", "Claim Net (AED)", "Denied (AED)", "Expected Recovery (AED)",
               "Denial Codes", "Categories", "Owner(s)", "Visit Check", "Status"]
        ws.append(hdr)
        for c in ws[1]:
            c.font = WB; c.fill = TEAL; c.alignment = Alignment(wrap_text=True, vertical="center")
        order = {"P1": 0, "P2": 1, "P3": 2}
        groups = defaultdict(list)
        for x in open_:
            groups[(x["claim_id"], x["ra_date"])].append(x)
        rows = []
        for (cid, ra), ls in groups.items():
            pr = min((l["priority"] for l in ls if l["priority"]), key=lambda p: order[p], default=None)
            dl = min(l["days_left"] for l in ls)
            j = lambda vals: ", ".join(dict.fromkeys(v for v in vals if v))
            st = set(l["status"] for l in ls)
            rows.append([pr, dl, date.fromisoformat(ls[0]["deadline"]), cid, date.fromisoformat(ra),
                         date.fromisoformat(ls[0]["treatment_date"]) if ls[0]["treatment_date"] else None,
                         ls[0]["receiver"], ls[0]["payer"], ls[0].get("claim_activities"), len(ls),
                         ls[0].get("claim_net"), round(sum(l["denied"] for l in ls), 2),
                         round(sum(l["expected_recovery"] for l in ls), 2), j(l["denial_code"] for l in ls),
                         j(l["category"] for l in ls), j(l["owner"] for l in ls),
                         j((l.get("consult") or {}).get("verdict") for l in ls),
                         st.pop() if len(st) == 1 else "mixed"])
        rows.sort(key=lambda r: (order.get(r[0], 3), r[1], -r[12]))
        for r in rows:
            ws.append(r)
            if r[1] is not None and r[1] <= URGENT_DAYS:
                for c in ws[ws.max_row]: c.fill = URG
        for row in ws.iter_rows(min_row=2):
            for c in row:
                c.border = THIN; c.alignment = Alignment(vertical="top", wrap_text=c.column in (14, 15, 17))
                if isinstance(c.value, date): c.number_format = "DD/MM/YYYY"
                if c.column in (11, 12, 13): c.number_format = "#,##0.00"
        for i, w in enumerate([9, 9, 12, 30, 12, 13, 30, 30, 10, 10, 12, 12, 13, 22, 26, 14, 30, 16], 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "E2"; ws.auto_filter.ref = ws.dimensions
    return wb
