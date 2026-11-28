"""
recon_review.py — review of reconciliation-eligible denials (denied again after the 1st resubmission).

For every reconciliation line it:
  1. checks the category against the payer's own reason text (the denial code alone is often wrong —
     e.g. CLAI-012 used for an expired clinician licence, AUTH-001 used for a lipid-panel rule);
  2. decides how recoverable the line is: if the payer's reason holds (true follow-up, earlier test
     already paid inside the interval, no diagnosis on the claim supports the test, late submission,
     already paid, patient share) it is marked Low and routed to write-off / collect, with no
     reconciliation remark;
  3. writes a specific reconciliation remark for the recoverable lines: the service, date, diagnosis
     and the reason it was rendered, ending with the B to B settlement request;
  4. attaches the claim's scrub findings.
Rule-based only; nothing leaves the server.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import mnec_evidence
from .presub_queries import REPEAT_TESTS

B2B = ("Requesting your consideration to settle on a B to B basis. Awaiting your confirmation to proceed with "
       "Resub under Reconciliation.")
DEFAULT_REPEAT_DAYS = 90          # repeat interval for a lab test with no specific rule
LOW_FACTOR = 0.1
# payers whose portal needs the doctor updated and activated (receiver id -> (name, portal))
PORTALS = {"TPA004": ("NAS", "JetNAS"), "TPA001": ("Neuron", "JetNAS")}   # NAS and Neuron share JetNAS                  # expected recovery kept for Low lines

WRITE_OFF = "Write-off"
COLLECT = "Collect from patient"
RECON = "Reconciliation request"
CHECK = "Check, then decide"

_FACTS_SQL = text("""
SELECT DISTINCT ON (c.claim_id) c.id, c.claim_id, c.member_id, c.encounter_start, c.clinician_id, c.clinician_name,
       (SELECT array_agg(x.diagnosis_code || '|' || COALESCE(x.diagnosis_description, '')
                         ORDER BY (lower(x.diagnosis_type) IN ('primary', 'principal')) DESC, x.sequence_no, x.id)
        FROM claim_diagnoses x WHERE x.claim_id_fk = c.id) AS dx
FROM claims c WHERE c.facility_id = :f AND c.claim_id = ANY(:ids)
ORDER BY c.claim_id, c.submission_date DESC NULLS LAST, c.id DESC""")
_ACT_SQL = text("""
SELECT DISTINCT ON (c.claim_id, a.activity_id) c.claim_id, a.activity_id, a.activity_description
FROM claims c JOIN claim_activities a ON a.claim_id_fk = c.id
WHERE c.facility_id = :f AND c.claim_id = ANY(:ids)
ORDER BY c.claim_id, a.activity_id, c.id DESC""")
_SCRUB_SQL = text("""
SELECT f.claim_id, array_agg(DISTINCT f.rule_id) AS rules
FROM scrub_xml_findings f JOIN claims c ON c.raw_xml_id = f.raw_xml_id AND c.claim_id = f.claim_id AND c.facility_id = :f
WHERE f.claim_id = ANY(:ids) AND f.rule_id <> 'ALL_CLEAR' GROUP BY 1""")
_PREV_SQL = text("""
SELECT r.claim_id, d.activity_id, d.denial_code, r.remittance_date
FROM remittances r JOIN remittance_activity_details d ON d.remittance_id_fk = r.id
WHERE r.facility_id = :f AND r.claim_id = ANY(:ids) AND COALESCE(d.denial_code, '') <> ''
ORDER BY r.remittance_date""")
_SCRUBBED_SQL = text("""
SELECT DISTINCT c.claim_id FROM claims c JOIN scrub_xml_findings f ON f.raw_xml_id = c.raw_xml_id
WHERE c.facility_id = :f AND c.claim_id = ANY(:ids)""")
# earlier PAID billing of the same code for the same member (latest RA per activity)
_PAID_SQL = text("""
SELECT c.member_id, upper(a.activity_code) AS code, c.claim_id, c.encounter_start
FROM claims c JOIN claim_activities a ON a.claim_id_fk = c.id
JOIN LATERAL (
    SELECT d.paid_amount FROM remittances r JOIN remittance_activity_details d ON d.remittance_id_fk = r.id
    WHERE r.facility_id = c.facility_id AND r.claim_id = c.claim_id AND d.activity_id = a.activity_id
    ORDER BY r.remittance_date DESC NULLS LAST, r.id DESC LIMIT 1) ra ON ra.paid_amount > 0
WHERE c.facility_id = :f AND c.member_id = ANY(:members) AND upper(a.activity_code) = ANY(:codes)
  AND c.encounter_start >= :d0""")


# Licence evidence: claims by the same clinician within ±7 days of the date of service that a payer PAID with no
# licence denial on them. Paid by another payer in the same days = the DHA licence was active; the denying payer's
# own credentialing (clinician not registered with that payer) is the likely cause.
_LIC_SQL = text("""
SELECT c.receiver_id, count(DISTINCT c.claim_id) AS n
FROM claims c JOIN remittances r ON r.facility_id = c.facility_id AND r.claim_id = c.claim_id AND r.paid_amount > 0
WHERE c.facility_id = :f AND upper(c.clinician_id) = upper(:clin)
  AND c.encounter_start BETWEEN :d - interval '7 days' AND :d + interval '7 days'
  AND NOT EXISTS (SELECT 1 FROM remittances r2 JOIN remittance_activity_details d2 ON d2.remittance_id_fk = r2.id
                  WHERE r2.facility_id = c.facility_id AND r2.claim_id = c.claim_id
                    AND (COALESCE(d2.denial_description, '') || ' ' || COALESCE(r2.denial_description, '')) ILIKE '%licen%')
GROUP BY 1""")


def _icd(code: str) -> str:
    from .claim_scrubber.dx_support import describe
    return describe(code)


def _dxs(code: str) -> str:
    d = _icd(code)
    return f"{code} {d}" if d else code


_CITED = re.compile(r"\{([^{}]*)\}")
_CODE = re.compile(r"\b(\d{4,5}|[A-Z]\d{4}|\d{1,2}(?:\.\d{2})?)\b")


def cited_codes(reason: str) -> List[str]:
    """Activity codes the payer lists in braces, e.g. '... too frequently{80061, 84450}' -> ['80061', '84450']."""
    out: List[str] = []
    for grp in _CITED.findall(reason or ""):
        out += [c.upper() for c in _CODE.findall(grp)]
    return out


def reason_cites_other(reason: str, act: str) -> List[str]:
    """Codes cited in the reason when THIS activity is not among them (claim-level reason copied to every line)."""
    codes = cited_codes(reason)
    return codes if codes and (act or "").upper() not in codes else []


def _has(reason: str, *words: str) -> bool:
    return any(w in reason for w in words)


def _cat(item, new: str, note: str) -> None:
    if item["category"] != new:
        item["category_original"] = item["category"]
        item["category"] = new
        item["category_note"] = note


def _verdict(item, level: str, outcome: str, why: str, remark: Optional[str]) -> None:
    item["recon_recoverability"] = level
    item["recon_outcome"] = outcome
    item["recon_why"] = why
    item["recon_remark"] = remark
    if outcome != RECON:
        item["action"] = f"{outcome} — {why}"
    if level == "Low":
        item["expected_recovery"] = round(item["expected_recovery"] * LOW_FACTOR, 2)


def review(db: Session, fid: int, items: List[dict]) -> None:
    """Annotate reconciliation items in place."""
    rec = [x for x in items if x["bucket"] == "reconciliation"]
    if not rec:
        return
    ids = sorted({x["claim_id"] for x in rec})
    facts = {r.claim_id: r for r in db.execute(_FACTS_SQL, {"f": fid, "ids": ids})}
    desc = {(r.claim_id, r.activity_id): r.activity_description for r in db.execute(_ACT_SQL, {"f": fid, "ids": ids})}
    scrub = {r.claim_id: list(r.rules or []) for r in db.execute(_SCRUB_SQL, {"f": fid, "ids": ids})}
    scrubbed = {r.claim_id for r in db.execute(_SCRUBBED_SQL, {"f": fid, "ids": ids})}

    prev_den: Dict[tuple, list] = defaultdict(list)
    for r in db.execute(_PREV_SQL, {"f": fid, "ids": ids}):
        prev_den[(r.claim_id, r.activity_id)].append((r.remittance_date, r.denial_code))
    for x in rec:
        earlier = [c for dte, c in prev_den.get((x["claim_id"], x["activity_id"]), [])
                   if dte and dte.isoformat() < x["ra_date"]]
        x["prev_denial"] = earlier[-1] if earlier else None

    members = sorted({f.member_id for f in facts.values() if f.member_id})
    codes = sorted({(x["activity_code"] or "").strip().upper() for x in rec})
    starts = [f.encounter_start for f in facts.values() if f.encounter_start]
    paid = defaultdict(list)
    if members and codes and starts:
        for r in db.execute(_PAID_SQL, {"f": fid, "members": members, "codes": codes,
                                        "d0": min(starts) - timedelta(days=365)}):
            paid[(r.member_id, r.code)].append((r.encounter_start, r.claim_id))

    lic_cache: Dict[tuple, dict] = {}
    for x in rec:
        f = facts.get(x["claim_id"])
        x["scrubbed"] = x["claim_id"] in scrubbed
        x["scrub_rules"] = scrub.get(x["claim_id"], [])
        lic_paid = None
        if f and f.clinician_id and f.encounter_start and "licen" in (x.get("denial_reason") or "").lower():
            key = (f.clinician_id.upper(), f.encounter_start.date())
            if key not in lic_cache:
                lic_cache[key] = {r.receiver_id: r.n for r in
                                  db.execute(_LIC_SQL, {"f": fid, "clin": f.clinician_id, "d": f.encounter_start})}
            lic_paid = lic_cache[key]
        _review_one(x, f, desc.get((x["claim_id"], x["activity_id"])) or "", paid, lic_paid)


def _review_one(x: dict, f, adesc: str, paid, lic_paid: Optional[dict] = None) -> None:
    code = x["denial_code"] or ""
    reason = (x.get("line_reason") if x.get("line_reason") is not None else x.get("denial_reason") or "").lower()
    cat = x["category"]
    act = (x["activity_code"] or "").strip().upper()
    date = f.encounter_start.strftime("%d/%m/%Y") if f and f.encounter_start else "[date]"
    svc = f"{act} ({adesc})" if adesc else act
    dx = [d.split("|", 1) for d in (f.dx or [])] if f else []
    dx_codes = [d[0] for d in dx]
    pdx = f"{dx[0][0]} ({dx[0][1]})" if dx and dx[0][1] else (dx[0][0] if dx else "[diagnosis]")
    clin = (f.clinician_name or f.clinician_id) if f else None
    pdx = _dxs(dx[0][0]) if dx else "[diagnosis]"
    sup = mnec_evidence._match(act, dx_codes)   # (indication, guideline, dx code) supporting this service
    if sup:
        role = "principal" if dx and sup[2] == dx[0][0] else "secondary"
        for_dx = f"{sup[0]} ({role} diagnosis {_dxs(sup[2])})"
    else:
        for_dx = f"principal diagnosis {pdx}"

    x["supporting_dx"] = ({"code": sup[2], "description": _icd(sup[2]), "indication": sup[0], "guideline": sup[1],
                           "role": role} if sup else None)

    # ── payer reason names other activities, not this one ──
    other = reason_cites_other(x.get("denial_reason") or "", act) if x.get("cited_elsewhere") else []
    if x.get("cited_elsewhere"):
        cs = ", ".join(dict.fromkeys(other)) or "other activities"
        if x["category"] == "Consultation visit rules":
            _cat(x, "Medical necessity", f"The payer reason cites {cs} (test frequency / justification), not this "
                                         f"consultation; it is not a consultation-rule denial.")
        x["consult"] = None
        return _verdict(x, "Medium", RECON,
                        f"The denial reason refers to {cs}, not {act}: no reason was given for this activity.",
                        f"The service {svc} on {date} for {for_dx} is not cited in the denial reason, which refers "
                        f"only to {cs}; no reason has been given for denying {act}. {B2B}")

    # ── consultation visit rules: reconciliation only when the payer is wrong ──
    chk = x.get("consult")
    if x["category"] == "Consultation visit rules" and chk:
        if chk["consult_action"] == "close":
            return _verdict(x, "Low", WRITE_OFF,
                            f"True follow-up — {chk['verdict']}. Not billable under DHA GC 07/2021 (free follow-up "
                            f"within 7 days, same condition, same practitioner). Write off; no reconciliation.", None)
        if chk["consult_action"] == "recode":
            ev = chk["evidence"]
            return _verdict(x, "Medium", RECON,
                            "Repeat visit (8–28 days): payable only at the repeat consultation rate.",
                            f"The consultation on {date} is a repeat visit {ev['gap_days']} days after the initial "
                            f"consultation ({ev['prev_claim']}, {ev['prev_date']}) for {pdx}; it is billable as a repeat "
                            f"consultation ({chk['recode']}) at the repeat rate. {B2B}")
        if chk["consult_action"] == "resubmit":
            return _verdict(x, "High", RECON, f"Payer rule does not apply — {chk['verdict']}.",
                            (chk["draft_comment"] or "").replace(" Kindly review and settle.", "") + f" {B2B}")
        return _verdict(x, "Medium", CHECK, chk["action"], None)

    # ── late submission ──
    if cat == "Late submission" or (code.startswith("TIME") and not reason.strip()):
        _cat(x, "Late submission", "Payer reason is late submission, not missing documents.")
        return _verdict(x, "Low", WRITE_OFF,
                        "Late submission penalty / time limit applied" + (" and the initial claim is already settled"
                                                                          if _has(reason, "settled") else "")
                        + ". Payer is correct; write off.", None)

    # ── duplicate already paid ──
    if cat == "Duplicate — already paid":
        _cat(x, "Duplicate — already paid", "Payer states the item was paid in the earlier submission.")
        return _verdict(x, "Low", WRITE_OFF, "Already paid in the previous submission; nothing further due.", None)

    # ── clinician licence ──
    if cat == "Clinician licence":
        _cat(x, "Clinician licence", "Payer reason is the clinician licence, not prior approval.")
        who = f"{clin} (licence {f.clinician_id})" if f and clin and clin != f.clinician_id else (clin or "the clinician")
        rcv = (x.get("receiver_id") or "").upper()
        portal = PORTALS.get(rcv)
        same = sum(v for k, v in (lic_paid or {}).items() if k in PORTALS)   # NAS + Neuron (one portal)
        if portal:
            name, portal_name = portal
            if same:
                return _verdict(x, "High", RECON,
                                f"{same} other NAS / Neuron claim(s) by the same clinician within 7 days of {date} were "
                                f"paid without a licence denial — clinician active in JetNAS.",
                                f"The clinician licence is active during the service date ({date}). The treating "
                                f"clinician {who} was active in the JetNAS portal, and the same clinician was paid for "
                                f"other NAS / Neuron members during the same period. The service {svc} was rendered for "
                                f"{for_dx}. {B2B}")
            return _verdict(x, "Medium", RECON,
                            f"No NAS / Neuron claim by this clinician was paid within 7 days of {date}. Facility to "
                            f"confirm the clinician is now updated and activated in JetNAS.",
                            f"The clinician licence is active during the service date ({date}). The treating "
                            f"clinician {who} is added in JetNAS also; the clinician's active status in JetNAS on the "
                            f"date of service is unknown, as there was a technical issue in our facility during that "
                            f"period. The service {svc} was rendered for {for_dx}. {B2B}")
        if lic_paid:
            ev = ", ".join(f"{k} {v}" for k, v in sorted(lic_paid.items()))
            return _verdict(x, "Medium", RECON,
                            f"Claims by the same clinician within 7 days were paid without a licence denial ({ev}). "
                            f"Facility to attach the licence copy and update the payer registration.",
                            f"The clinician licence is active during the service date ({date}); the licence copy is "
                            f"attached. The service {svc} was rendered for {for_dx}. {B2B}")
        return _verdict(x, "Low", CHECK,
                        f"No claim by this clinician was paid within 7 days of {date}: the licence may really have "
                        f"lapsed. Facility to confirm the licence validity on that date before reconciling; otherwise "
                        f"write off.", None)

    # ── GP referral ──
    if cat == "GP referral":
        _cat(x, "GP referral", "Payer reason is a missing GP referral.")
        return _verdict(x, "Medium", RECON, "Recoverable if a GP referral exists for this visit.",
                        f"The patient was referred by a GP for {pdx}; the referral is attached. The service {svc} on "
                        f"{date} was rendered on that referral. {B2B}")

    # ── panel composition (e.g. lipid panel must include ...) ──
    if cat == "Coding — panel composition":
        _cat(x, "Coding — panel composition", "Payer reason is the panel definition (coding), not approval or price.")
        return _verdict(x, "Low", WRITE_OFF,
                        "Panel code billed without all required components; payer pays the components only. "
                        "Write off and bill the components in future.", None)

    # ── patient share ──
    if cat == "Patient share" or (code.startswith("COPY") and not reason.strip()):
        _cat(x, "Patient share", "Payer reason is the patient share / deductible.")
        return _verdict(x, "Low", COLLECT, "Patient share / deductible not applied; collect from the patient.", None)

    # ── frequency (repeated too frequently / too soon) ──
    if cat == "Medical necessity — frequency" or (code == "MNEC-005" and not reason.strip()):
        _cat(x, "Medical necessity — frequency", "Payer reason is the frequency of the test.")
        days = REPEAT_TESTS.get(act, (None, DEFAULT_REPEAT_DAYS))[1]
        prev = sorted([p for p in paid.get((f.member_id if f else None, act), [])
                       if f and f.encounter_start and p[0] < f.encounter_start], reverse=True)
        if prev:
            gap = (f.encounter_start - prev[0][0]).days
            when = prev[0][0].strftime("%d/%m/%Y")
            if gap <= days:
                return _verdict(x, "Low", WRITE_OFF,
                                f"Payer is correct: {act} already paid on {when} ({gap} days earlier, inside the "
                                f"{days}-day interval). Reconcile only if the notes show a change in condition.", None)
            return _verdict(x, "High", RECON, f"Last paid {act} was {gap} days earlier, outside the {days}-day interval.",
                            f"The test {svc} on {date} for {for_dx} is not a repeat inside the usual interval: the "
                            f"previous paid test was on {when} ({gap} days earlier). {B2B}")
        if not sup:
            return _verdict(x, "Medium", RECON,
                            f"No earlier paid {act} in the last 12 months, but no diagnosis on the claim "
                            f"({', '.join(dx_codes) or 'none'}) supports {act} — add the indication from the notes.",
                            f"The test {svc} on {date} for [indication from the notes — diagnosis on the claim: {pdx}] "
                            f"is not a repeat: no previous {act} was paid for this member in the 12 months before. {B2B}")
        return _verdict(x, "High", RECON, f"No earlier paid {act} for this member in the last 12 months.",
                        f"The test {svc} on {date} for {for_dx} is not a repeat: no previous {act} was paid for this "
                        f"member in the 12 months before. {B2B}")

    # ── medical necessity (not indicated / not justified for the diagnoses) ──
    if cat in ("Medical necessity", "Medical necessity — alternative service") or ((code.startswith("MNEC") or code == "PRCE-011") and not reason.strip()) or _has(reason, "not justified", "not clinically indicated",
                                                                "not medically justified"):
        if cat != "Medical necessity — alternative service":
            _cat(x, "Medical necessity", "Payer reason is medical necessity.")
        m = mnec_evidence._match(act, dx_codes)
        if not m:
            return _verdict(x, "Low", WRITE_OFF,
                            f"Payer is likely correct: no diagnosis on the claim ({', '.join(dx_codes) or 'none'}) "
                            f"supports {act}. Reconcile only if the case sheet documents an indication.", None)
        return _verdict(x, "Medium", RECON, f"Supported: {m[0]} ({m[2]}) — {m[1]}.",
                        f"The service {svc} was rendered on {date} for {for_dx}, as recommended by the {m[1]}; "
                        f"the clinical notes are attached. {B2B}")

    # ── CLAI-012 with no reason: contractual non-compliance, not prior approval ──
    if code == "CLAI-012" and not reason.strip():
        _cat(x, "Contractual (CLAI-012, no reason given)",
             "CLAI-012 is 'submission not compliant with the contractual agreement'; the payer gave no reason, so it "
             "is not a prior-approval denial.")
        prev = x.get("prev_denial")
        why = ("Payer gave no reason with CLAI-012" + (f"; the earlier denial was {prev}" if prev else "")
               + ". Ask the payer for the specific contract clause before reconciling.")
        return _verdict(x, "Medium", CHECK, why,
                        f"The service {svc} on {date} for {for_dx} was rendered and billed as per the contract; the "
                        f"denial CLAI-012 was received without a reason. Kindly specify the contractual clause "
                        f"or settle. {B2B}")

    # ── prior approval ──
    if cat == "Prior approval":
        _cat(x, "Prior approval", "Payer reason is prior approval.")
        if not x.get("authorization_id"):
            return _verdict(x, "Low", WRITE_OFF if _has(reason, "not taken") else CHECK,
                            "No approval on the claim" + (" and the payer says none was taken" if _has(reason, "not taken")
                                                          else "") + ". Reconcile only with a retro-approval.", None)
        return _verdict(x, "Medium", RECON, f"Approval {x['authorization_id']} is on the claim.",
                        f"The service {svc} on {date} for {for_dx} was rendered under approval {x['authorization_id']}, "
                        f"valid on the date of service. {B2B}")

    # ── coverage / eligibility ──
    if cat == "Coverage / eligibility":
        return _verdict(x, "Low", CHECK, "Coverage / eligibility denial: recoverable only with eligibility proof from "
                        "the date of service; otherwise collect from the patient.",
                        f"The patient was eligible on {date} and {svc} for {for_dx} is covered under the policy; the "
                        f"eligibility proof is attached. {B2B}")

    # ── coding (bundling, Excludes-1, MUE, diagnosis-procedure mismatch) ──
    if cat == "Coding":
        return _verdict(x, "Low", CHECK, "Coding denial: correct the coding if the payer is right (bundled / "
                        "mismatched codes are not recoverable as billed); reconcile only if the coding was correct.",
                        f"The service {svc} on {date} was coded as per the clinical documentation for {for_dx}. {B2B}")

    # ── duplicate (not already paid) ──
    if cat == "Duplicate":
        return _verdict(x, "Medium", RECON, "Recoverable if this was a separate service, not a duplicate.",
                        f"The service {svc} on {date} was a separate service for {for_dx}, not a duplicate, as "
                        f"documented in the attached notes. {B2B}")

    if cat == "Recalled by provider":
        return _verdict(x, "Low", WRITE_OFF, "Claim recalled / cancelled on the facility's request.", None)
    if cat == "Inpatient overlap":
        return _verdict(x, "Low", CHECK, "Outpatient service during an inpatient admission — included in the IP claim "
                        "unless unrelated; reconcile only if the service was unrelated to the admission.", None)
    if cat == "Resubmission type error":
        return _verdict(x, "Medium", RECON, "Resubmission type / amount error — correct it in the reconciliation.",
                        f"The service {svc} on {date} for {for_dx} is resubmitted for the denied amount only, under the "
                        f"correct resubmission type. {B2B}")
    if cat == "Wrong receiver":
        return _verdict(x, "Medium", CHECK, "Submitted to the wrong receiver — submit to the responsible receiver.", None)

    # ── contract & tariff ──
    if x["category"] == "Contract & tariff":
        return _verdict(x, "Medium", RECON, "Billed at the contracted tariff — confirm the price with the contract.",
                        f"The service {svc} on {date} was billed at the agreed contract tariff. {B2B}")

    _verdict(x, "Medium", CHECK, "Review the payer reason before reconciling.",
             f"The service {svc} was rendered on {date} for {for_dx}, as documented in the attached notes. {B2B}")


def fix_open_clai012(items: List[dict]) -> None:
    """CLAI-012 on first-resubmission (open) items: not prior approval. Licence reason -> clinician licence;
    no reason -> contractual, ask the payer for the clause."""
    for x in items:
        if x["bucket"] == "reconciliation" or (x["denial_code"] or "") != "CLAI-012" \
                or x["category"] == "Consultation visit rules":
            continue
        reason = (x.get("denial_reason") or "").lower()
        if "licen" in reason:
            portal = PORTALS.get((x.get("receiver_id") or "").upper())
            _cat(x, "Clinician licence", "Payer reason is the clinician licence, not prior approval.")
            x["action"] = ("Clinician licence denial: facility to update and activate the doctor in "
                           f"{portal[1] if portal else 'the payer portal'}, then resubmit with the licence copy.")
            x["facility_request"] = (f"Update and activate the treating doctor in {portal[1] if portal else 'the payer portal'}"
                                     " and share the DHA licence copy valid on the date of service.")
            x["documents"] = "DHA licence copy, portal activation confirmation"
            x["draft_comment"] = ("The clinician licence is active during the service date. The treating clinician "
                                  f"is added in {portal[1] if portal else 'the payer portal'}; the licence copy is "
                                  "attached. Kindly review and settle.")
        elif "referral" in reason:
            _cat(x, "GP referral", "Payer reason is a missing GP referral, not prior approval.")
            x["action"] = ("GP referral denial: facility to share the GP referral for this visit; resubmit with it. "
                           "If none exists, close as write-off.")
            x["facility_request"] = "Share the GP referral letter for this visit (dated before the specialist visit)."
            x["documents"] = "GP referral letter"
            x["draft_comment"] = ("The patient was referred by a GP for this condition; the referral is attached. "
                                  "Kindly review and settle.")
        elif not reason.strip() or reason.strip() == "-":
            _cat(x, "Contractual (CLAI-012, no reason given)",
                 "CLAI-012 is 'submission not compliant with the contractual agreement'; no reason was given.")
            x["owner"] = "Ghaf"
            x["facility_request"] = None
            x["action"] = ("CLAI-012 received without a reason. Check the payer portal / TPA comments for the clause; "
                           "if none, resubmit asking the payer to specify the contractual clause.")
            x["documents"] = "Clinical notes"
            x["draft_comment"] = ("The service was rendered and billed as per the contract; the denial CLAI-012 was "
                                  "received without a reason. Kindly specify the contractual clause or settle.")
        elif x["category"] == "Prior approval" and not any(w in reason for w in ("approval", "authori")):
            _cat(x, "Contractual (CLAI-012)", "CLAI-012 is contractual non-compliance; the reason is not about approval.")
