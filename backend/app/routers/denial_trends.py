"""
denial_trends.py — monthly denial trends per payer + scrubber rule coverage ("rule gaps").

Complements the Denial Intelligence page (routers/calibration.py), which shows all-time top
codes and rule precision for one facility. This router adds:

  GET /api/denial-trends/monthly?facility_id&payer&months=6&top=15
      Per month × payer × denial code: lines, claims, denied amount, change vs previous month.
  GET /api/denial-trends/rule-coverage?facility_id&payer&months=6
      Per denial code: volume, denied amount, matching scrubber rules / presub queries /
      resub playbook entries — or "no rule" (a rule gap).

Read only. Month = RA remittance_date month. Payer = original claim's receiver (TPA), else claim payer,
else RA payer_id (RA payer_id is mostly 'UNKNOWN'). Denied amount = (net, else gross) − paid, floored at 0.
Test facility GHAF-001 is always excluded.
"""
from __future__ import annotations

import re
import time
from collections import defaultdict
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..services.payer_names import payer_name
from ..services.denial_codes import denial_description
from ..database import get_db
from ..models import User
from ..security import resolve_facility, check_facility, allowed_facility_ids, is_clinic_user, get_current_user

router = APIRouter(prefix="/api/denial-trends", tags=["denial-trends"])

_CACHE: dict = {}
TTL = 900  # 15 min — the aggregation scans every RA line in the window (~1–3 s cold)

# ---------------------------------------------------------------------------
# Scrubber rule catalogue: denial code (exact or prefix) → rules that try to prevent it.
# Sources: services/claim_scrubber/scrubber.py (finding_to_denial + uae-rules-2025.json
# denial_impact), services/scrub_rules.py (_ALL_RULES), services/presub_queries.py and
# services/dental_rules.py (_q query types). Keep in sync when rules are added.
# ---------------------------------------------------------------------------
_S, _R, _P = "Scrubber (XML)", "Scrub rule", "Presub query"
RULE_MAP: list[tuple[str, str, str, str]] = [
    # (code or prefix*, source, rule id, note)
    ("CODE-001", _S, "DIAGNOSIS_MISSING / PRINCIPAL_DX_MISSING", "Missing / invalid diagnosis"),
    ("CODE-006", _S, "CPT_ICD_LINK_MISSING / CPT_CODE_FORMAT", "CPT–ICD linkage"),
    ("CODE-016", _S, "MODIFIER / FREQUENCY / MUE / NAS repeat rules", "Frequency / MUE / modifier"),
    ("MNEC-007", _S, "BUNDLING / MUTUALLY_EXCLUSIVE / NAS bundle rules", "Bundling / component codes"),
    ("MNEC-007", _P, "TEST_PAIR", "Test pair — necessity of both"),
    ("MNEC-003", _P, "DX_FIT / DX_DOC / DENIAL_RISK", "Diagnosis does not support service"),
    ("MNEC-004", _P, "DX_FIT / DX_DOC / DENIAL_RISK", "Diagnosis does not support service"),
    ("MNEC-005", _P, "DENIAL_RISK / ADMIN_NECESSITY", "Payer pattern — not clinically indicated"),
    ("MNEC-006", _P, "ALT_SERVICE / ADMIN_NECESSITY", "Alternative service (IV vs oral)"),
    ("MNEC-006", _P, "FOLLOWUP / REPEAT / CONSULT_SAMEDAY", "Follow-up / repeat visit rule"),
    ("AUTH-001", _R, "MISSING_AUTH", "Approval missing"),
    ("AUTH-001", _P, "APPROVAL_MISSING / APPROVAL_DX / APPROVAL_DENTAL / APPROVAL_MATERNITY", "Approval missing"),
    ("AUTH-002", _P, "APPROVAL_PARTIAL / APPROVAL_DX", "Approval not covering all services"),
    ("AUTH-002", _R, "MISSING_AUTH", "Approval missing"),
    ("DUPL-001", _R, "DUPLICATE", "Duplicate claim / line"),
    ("DUPL-001", _P, "DUPLICATE_LINES", "Duplicate lines"),
    ("DUPL-002", _R, "DUPLICATE", "Duplicate claim"),
    ("CLAI-012", _P, "LICENCE", "Clinician licence watch-list"),
    ("NCOV*", _R, "EXCLUDE_1 / EXCLUDE_2 (exclusion_codes)", "Excluded diagnosis / activity"),
    ("NCOV*", _P, "DX_EXCLUDED / OPTICAL_BENEFIT", "Not covered under policy"),
    ("PRCE-001", _R, "FREE_FOLLOWUP", "Free follow-up billed"),
    ("PRCE-001", _P, "FOLLOWUP_NET", "Follow-up net amount"),
    ("CODE*", _P, "DRUG_PARENT / DRUG_SEQUENCE / EXCLUDES1 / MATERNITY_CODING / DENTAL_RULE", "Coding corrections"),
]


def _rules_for(code: str) -> list[dict]:
    out = []
    for key, src, rule, note in RULE_MAP:
        hit = code.startswith(key[:-1]) if key.endswith("*") else code == key
        if hit:
            out.append({"source": src, "rule": rule, "note": note, "exact": not key.endswith("*")})
    return out


def _scope(user: User, facility_id: Optional[int]) -> Optional[int]:
    return resolve_facility(None, user, facility_id)


def _month_start(months_back: int) -> date:
    t = date.today()
    y, m = t.year, t.month - months_back
    while m <= 0:
        m += 12; y -= 1
    return date(y, m, 1)


# A line is a NEW denial when it is on the claim's first remittance (no resubmission before the RA);
# otherwise it is a RE-DENIAL of a resubmission. Trends use new denials only (the current pattern);
# re-denials are analysed on the Resubmission outcomes tab.
_REDENIED = ("EXISTS (SELECT 1 FROM claims x WHERE x.facility_id = r.facility_id AND x.claim_id = r.claim_id "
             "AND x.resubmission_key IN ('internal complaint', 'correction', 'reconciliation') "
             "AND x.submission_date::date < r.remittance_date::date)")
STAGES = ("new", "redenied", "all")


def _stage_sql(stage: str) -> str:
    return {"new": f"AND NOT {_REDENIED}", "redenied": f"AND {_REDENIED}"}.get(stage, "")


_AGG_SQL = """
SELECT to_char(r.remittance_date, 'YYYY-MM') AS month,
       upper(coalesce(nullif(c.receiver_id, ''), nullif(c.payer_id, ''), r.payer_id)) AS payer,
       max(coalesce(nullif(c.receiver_name, ''), c.payer_name)) AS payer_name,
       d.denial_code                         AS code,
       max(d.denial_description)             AS descr,
       count(*)                              AS lines,
       count(DISTINCT r.id)                  AS claims,
       sum(greatest(coalesce(d.net, d.gross, 0) - d.paid_amount, 0)) AS amount
FROM remittances r
JOIN remittance_activity_details d ON d.remittance_id_fk = r.id
LEFT JOIN claims c ON c.facility_id = r.facility_id AND c.claim_id = r.claim_id AND c.resubmission_key = ''
WHERE r.remittance_date >= :since
  AND r.facility_id NOT IN (SELECT id FROM facilities WHERE facility_code = 'GHAF-001')
  AND d.denial_code IS NOT NULL AND d.denial_code <> ''
  AND r.id IN (SELECT DISTINCT ON (x.facility_id, x.claim_id, x.ra_file_id, x.net_amount, x.paid_amount, x.denial_amount) x.id
               FROM remittances x WHERE x.remittance_date >= :since
               ORDER BY x.facility_id, x.claim_id, x.ra_file_id, x.net_amount, x.paid_amount, x.denial_amount, x.id DESC)
  {fac} {payer} {stage}
GROUP BY 1, 2, 4
"""

# Stage "all" = FINAL denied (same definition as the Dashboard / Claim Cycle): latest submission per claim; RA rows
# de-duplicated on (facility, claim, RA file, net, paid, denied); claim denied = net - min(max(sum paid, 0), net) for
# claims with an RA. Each claim's denied is spread over its service lines (line net minus that line's payments across
# the de-duplicated RAs, coded with the line's latest denial code) and scaled to add up to the claim's denied.
# Month = the claim's latest RA month. Stages "new"/"redenied" stay per-RA FIRST-PASS denials (labelled as such).
_FINAL_SQL = """
WITH c AS (
  SELECT DISTINCT ON (c.facility_id, c.claim_id) c.id, c.facility_id, c.claim_id, coalesce(c.net_amount, 0) net,
         upper(coalesce(nullif(c.receiver_id, ''), nullif(c.payer_id, ''))) payer,
         coalesce(nullif(c.receiver_name, ''), c.payer_name) payer_name
  FROM claims c
  WHERE c.facility_id NOT IN (SELECT id FROM facilities WHERE facility_code = 'GHAF-001') {cfac} {cpayer}
  ORDER BY c.facility_id, c.claim_id, c.submission_date DESC NULLS LAST, c.id DESC),
ra AS (
  SELECT DISTINCT ON (r.facility_id, r.claim_id, r.ra_file_id, r.net_amount, r.paid_amount, r.denial_amount)
         r.id, r.facility_id, r.claim_id, coalesce(r.paid_amount, 0) paid, r.remittance_date
  FROM remittances r JOIN c USING (facility_id, claim_id)
  ORDER BY r.facility_id, r.claim_id, r.ra_file_id, r.net_amount, r.paid_amount, r.denial_amount, r.id DESC),
cd AS (
  SELECT c.id cid, c.facility_id, c.claim_id, c.payer, c.payer_name, max(ra.remittance_date) last_ra,
         c.net - least(greatest(sum(ra.paid), 0), c.net) denied
  FROM c JOIN ra USING (facility_id, claim_id) GROUP BY 1, 2, 3, 4, 5, c.net
  HAVING c.net - least(greatest(sum(ra.paid), 0), c.net) > 0.01),
cdw AS (SELECT * FROM cd WHERE last_ra >= :since {cut}),
ln AS (
  SELECT cdw.cid, ad.activity_id, max(coalesce(ad.net, ad.gross, 0)) net, sum(coalesce(ad.paid_amount, 0)) paid,
         (array_agg(nullif(ad.denial_code, '') ORDER BY ra.remittance_date DESC, ra.id DESC)
            FILTER (WHERE nullif(ad.denial_code, '') IS NOT NULL))[1] code,
         (array_agg(ad.denial_description ORDER BY ra.remittance_date DESC, ra.id DESC)
            FILTER (WHERE nullif(ad.denial_code, '') IS NOT NULL))[1] descr
  FROM cdw JOIN ra USING (facility_id, claim_id)
  JOIN remittance_activity_details ad ON ad.remittance_id_fk = ra.id
  GROUP BY 1, 2),
lu AS (SELECT cid, code, descr, greatest(net - paid, 0) un FROM ln WHERE code IS NOT NULL),
s AS (SELECT cid, sum(un) tot, count(*) n FROM lu GROUP BY 1)
SELECT to_char(cdw.last_ra, 'YYYY-MM') AS month, cdw.payer, max(cdw.payer_name) payer_name, lu.code,
       max(lu.descr) descr, count(*) lines, count(DISTINCT cdw.cid) claims,
       sum(CASE WHEN s.tot > 0.01 THEN lu.un * cdw.denied / s.tot ELSE cdw.denied / s.n END) amount
FROM cdw JOIN lu USING (cid) JOIN s USING (cid)
GROUP BY 1, 2, 4
"""


def _agg_sql(stage: str, fid, payer, cut: bool = False) -> str:
    if stage == "all":
        return _FINAL_SQL.format(cfac="AND c.facility_id = :fid" if fid else "",
                                 cpayer="AND upper(coalesce(nullif(c.receiver_id, ''), nullif(c.payer_id, ''))) = upper(:payer)" if payer else "",
                                 cut="AND last_ra <= :cut" if cut else "")
    sql = _AGG_SQL
    if cut:
        sql = sql.replace("WHERE r.remittance_date >= :since",
                          "WHERE r.remittance_date >= :since AND r.remittance_date <= :cut")
    return sql.format(fac="AND r.facility_id = :fid" if fid else "", stage=_stage_sql(stage),
                      payer="AND upper(coalesce(nullif(c.receiver_id, ''), nullif(c.payer_id, ''), r.payer_id)) = upper(:payer)" if payer else "")


AMOUNT_BASIS = {"all": "final denied (net - paid, latest submission, de-duplicated RAs)",
                "new": "first RA (first-pass denial), per line",
                "redenied": "re-denial of a resubmission (per RA), per line"}


def _aggregate(db: Session, fid: Optional[int], payer: Optional[str], months: int, stage: str = "new") -> list[dict]:
    key = ("agg", fid, payer, months, stage, date.today())
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    sql = _agg_sql(stage, fid, payer)
    # one extra month so the first displayed month has a "previous" to compare with
    rows = [dict(r) for r in db.execute(text(sql), {"since": _month_start(months), "fid": fid,
                                                     "payer": payer}).mappings()]
    for r in rows:
        r["amount"] = float(r["amount"] or 0)
    _CACHE[key] = (time.time(), rows)
    return rows


def _pct(cur: float, prev: float) -> Optional[float]:
    return None if not prev else round(100.0 * (cur - prev) / prev, 1)


@router.get("/monthly")
def monthly(
    facility_id: Optional[int] = None,
    payer: Optional[str] = None,
    stage: str = Query("new", pattern="^(new|redenied|all)$"),
    months: int = Query(6, ge=1, le=24),
    top: int = Query(15, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Top denial codes per payer per month with change vs the previous month."""
    fid = _scope(user, facility_id)
    rows = _aggregate(db, fid, payer, months, stage)
    all_months = sorted({r["month"] for r in rows})
    shown = all_months[-months:]
    prev_of = {m: (all_months[i - 1] if i else None) for i, m in enumerate(all_months)}

    cell = {(r["month"], r["payer"], r["code"]): r for r in rows}
    # The current month is partial: compare it with the SAME days of the previous month
    # (1st..yesterday), and with the code's usual pace over the 3 full months before.
    today = date.today()
    cur_m = today.strftime("%Y-%m")
    mtd_prev, usual = {}, defaultdict(list)
    if cur_m in shown and today.day > 1:
        pm_start = _month_start(1)
        pm_cut = pm_start.replace(day=min(today.day - 1, 28 if pm_start.month == 2 else 30))
        sql = _agg_sql(stage, fid, payer, cut=True)
        for r in db.execute(text(sql), {"since": pm_start, "cut": pm_cut, "fid": fid, "payer": payer}).mappings():
            mtd_prev[(r["payer"], r["code"])] = (int(r["lines"]), float(r["amount"] or 0))
    full = [m for m in all_months if m < cur_m][-3:]
    for r in rows:
        if r["month"] in full:
            usual[(r["payer"], r["code"])].append(r["lines"])
    out = []
    for m in shown:
        pm = prev_of[m]
        month_rows = sorted((r for r in rows if r["month"] == m), key=lambda r: -r["lines"])[:top * 5]
        for r in month_rows:
            k = (r["payer"], r["code"])
            if m == cur_m and today.day > 1:
                pl, pa = mtd_prev.get(k, (0, 0.0))
                frac = (today.day - 1) / 30.4
            else:
                p = cell.get((pm, r["payer"], r["code"])) if pm else None
                pl, pa = (p["lines"], p["amount"]) if p else (0, 0.0)
                frac = 1.0
            u = usual.get(k, [])
            usual_lines = (sum(u) / len(full)) * frac if full and m == cur_m else (
                sum(u) / len(full) if full and u else None)
            vs_usual = _pct(r["lines"], usual_lines) if usual_lines else None
            attention = (r["lines"] >= 20 and usual_lines is not None and r["lines"] >= 1.5 * usual_lines) or (
                r["lines"] >= 20 and not usual_lines)
            out.append({
                "month": m, "payer": r["payer"], "denial_code": r["code"], "description": denial_description(r["code"], r["descr"]),
                "payer_name": payer_name(r["payer"], r["payer_name"]), "lines": r["lines"], "claims": r["claims"], "amount": round(r["amount"], 2),
                "prev_lines": pl, "prev_amount": round(pa, 2),
                "change_lines": r["lines"] - pl, "change_pct": _pct(r["lines"], pl),
                "has_rule": bool(_rules_for(r["code"])),
                "usual_lines": round(usual_lines, 1) if usual_lines else None, "vs_usual_pct": vs_usual,
                "attention": bool(attention),
                "compare": "same days of previous month" if m == cur_m and today.day > 1 else "previous month",
            })

    # monthly totals (chart) and per-payer totals
    totals = defaultdict(lambda: {"lines": 0, "amount": 0.0})
    payers = defaultdict(lambda: {"lines": 0, "amount": 0.0, "name": None})
    for r in rows:
        if r["month"] in shown:
            totals[r["month"]]["lines"] += r["lines"]; totals[r["month"]]["amount"] += r["amount"]
            payers[r["payer"]]["lines"] += r["lines"]; payers[r["payer"]]["amount"] += r["amount"]
            payers[r["payer"]]["name"] = payers[r["payer"]]["name"] or payer_name(r["payer"], r["payer_name"])
    return {
        "months": shown, "partial_month": date.today().strftime("%Y-%m"), "amount_basis": AMOUNT_BASIS.get(stage),
        "rows": out,
        "totals": [{"month": m, "lines": totals[m]["lines"], "amount": round(totals[m]["amount"], 2)}
                   for m in shown],
        "payers": sorted(({"payer": k, **v} for k, v in payers.items()), key=lambda x: -x["lines"]),
    }


@router.get("/rule-coverage")
def rule_coverage(
    facility_id: Optional[int] = None,
    payer: Optional[str] = None,
    stage: str = Query("new", pattern="^(new|redenied|all)$"),
    months: int = Query(6, ge=1, le=24),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Per denial code: volume, amount, trend, and which scrubber rules / playbook entries cover it."""
    fid = _scope(user, facility_id)
    rows = _aggregate(db, fid, payer, months, stage)
    all_months = sorted({r["month"] for r in rows})
    shown = set(all_months[-months:])
    # trend compares the last two COMPLETE months (the current month is partial)
    cur = date.today().strftime("%Y-%m")
    complete = [m for m in all_months if m < cur]
    last = complete[-1] if complete else None
    prev = complete[-2] if len(complete) > 1 else None

    agg = defaultdict(lambda: {"lines": 0, "amount": 0.0, "payers": defaultdict(int), "descr": None,
                               "last": 0, "prev": 0})
    for r in rows:
        a = agg[r["code"]]
        if r["month"] in shown:
            a["lines"] += r["lines"]; a["amount"] += r["amount"]; a["payers"][r["payer"]] += r["lines"]
            a["descr"] = a["descr"] or r["descr"]
        if r["month"] == last:
            a["last"] += r["lines"]
        if r["month"] == prev:
            a["prev"] += r["lines"]

    playbook = [dict(p) for p in db.execute(text(
        "SELECT pattern, category, owner, action FROM resub_playbook WHERE is_active "
        "AND pattern <> '.*' AND coalesce(activity_pattern, '') = '' AND coalesce(reason_pattern, '') = '' "
        "ORDER BY sort_order"
    )).mappings()]
    compiled = []
    for p in playbook:
        try:
            compiled.append((re.compile(p["pattern"], re.I), p))
        except re.error:
            pass

    total = sum(a["lines"] for a in agg.values()) or 1
    out = []
    for code, a in agg.items():
        if not a["lines"]:
            continue
        rules = _rules_for(code)
        pb = next((p for rx, p in compiled if rx.search(code)), None)
        top_payers = sorted(a["payers"].items(), key=lambda x: -x[1])[:3]
        out.append({
            "denial_code": code, "description": denial_description(code, a["descr"]),
            "lines": a["lines"], "share_pct": round(100.0 * a["lines"] / total, 1),
            "amount": round(a["amount"], 2),
            "last_month": a["last"], "prev_month": a["prev"], "change_pct": _pct(a["last"], a["prev"]),
            "top_payers": [{"payer": k, "name": payer_name(k), "lines": v} for k, v in top_payers],
            "rules": rules,
            "coverage": "exact" if any(r["exact"] for r in rules) else ("family" if rules else "none"),
            "playbook": ({"category": pb["category"], "owner": pb["owner"], "action": pb["action"]}
                         if pb else None),
        })
    out.sort(key=lambda x: -x["amount"])
    covered = sum(x["lines"] for x in out if x["coverage"] != "none")
    return {
        "months": sorted(shown), "trend_months": [prev, last],
        "summary": {"codes": len(out), "lines": sum(x["lines"] for x in out),
                    "covered_lines_pct": round(100.0 * covered / total, 1),
                    "gap_codes": sum(1 for x in out if x["coverage"] == "none")},
        "codes": out,
    }


@router.get("/export.xlsx")
def export_xlsx(
    facility_id: Optional[int] = None,
    payer: Optional[str] = None,
    stage: str = Query("new", pattern="^(new|redenied|all)$"),
    months: int = Query(6, ge=1, le=24),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Excel: denials by RA month x payer x code, plus scrubber rule coverage."""
    import io
    from fastapi.responses import StreamingResponse
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    m = monthly(facility_id=facility_id, payer=payer, stage=stage, months=months, top=100, db=db, user=user)
    cov = rule_coverage(facility_id=facility_id, payer=payer, stage=stage, months=months, db=db, user=user)
    wb = Workbook()

    def sheet(ws, header, rows, money=()):
        ws.append(header)
        for c in ws[1]:
            c.font, c.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="0B4F6C")
        for r in rows:
            ws.append(r)
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for i, h in enumerate(header, 1):
            w = max([len(str(h))] + [len(str(r[i - 1] or "")) for r in rows[:300]]) + 2
            ws.column_dimensions[get_column_letter(i)].width = min(w, 60)
            if i in money:
                for col in ws.iter_cols(min_col=i, max_col=i, min_row=2):
                    for c in col:
                        c.number_format = "#,##0.00"

    ws = wb.active
    ws.title = "By RA month"
    sheet(ws, ["RA month", "Receiver / TPA", "Receiver name", "Denial code", "Description", "Denied lines",
               "Claims", "Denied AED", "Prev month lines", "Change %", "Scrubber rule"],
          [[r["month"], r["payer"], r.get("payer_name"), r["denial_code"], r["description"], r["lines"],
            r["claims"], r["amount"], r.get("prev_lines"), r.get("change_pct"), "yes" if r.get("has_rule") else "no rule"]
           for r in m["rows"]], money=(8,))
    sheet(wb.create_sheet("Rule coverage"),
          ["Denial code", "Description", "Denied lines", "Share %", "Denied AED", "Last month", "Prev month",
           "Change %", "Top payers", "Coverage", "Scrubber rules", "Resub playbook"],
          [[c["denial_code"], c["description"], c["lines"], c.get("share_pct"), c["amount"], c.get("last_month"),
            c.get("prev_month"), c.get("change_pct"),
            ", ".join(f'{payer_name(p["payer"]) or p["payer"]} ({p["lines"]})' for p in c.get("top_payers", [])),
            c.get("coverage"), "; ".join(str(x.get("rule") or x.get("id") or x) if isinstance(x, dict) else str(x)
                                         for x in c.get("rules", []) or []),
            (c.get("playbook") or {}).get("title") if isinstance(c.get("playbook"), dict) else c.get("playbook")]
           for c in cov["codes"]], money=(5,))
    info = wb.create_sheet("About")
    for line in ["Months are RA (remittance) months: the date the payer sent the remittance advice, "
                 "not the treatment (encounter) month.",
                 "Denied AED: stage new = first RA (first-pass) net minus paid per activity; stage all = final denied (net - paid, latest submission, de-duplicated RAs). Descriptions: DHA eClaimLink denial code list.",
                 f"Facility: {facility_id or 'all'} · Payer: {payer or 'all'} · Window: {months} months · "
                 f"Generated {time.strftime('%Y-%m-%d %H:%M')}"]:
        info.append([line])
    info.column_dimensions["A"].width = 110
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    name = f"denial_trends_{facility_id or 'all'}_{payer or 'all'}_{date.today():%Y%m%d}.xlsx"
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------------------------------------------------------------------------
# Drill-down: what is really behind one denial code
# ---------------------------------------------------------------------------
_DRILL_SQL = """
SELECT r.id rid, r.facility_id, f.facility_name, r.claim_id, r.remittance_date, r.tpa_comments,
       upper(coalesce(nullif(c.receiver_id, ''), nullif(c.payer_id, ''), r.payer_id)) payer,
       c.payer_id ins, c.member_id, c.encounter_start::date enc,
       d.activity_id, d.activity_code, d.activity_type,
       greatest(coalesce(d.net, d.gross, 0) - coalesce(d.paid_amount, 0), 0) amt,
       a.activity_description adesc,
       coalesce(nullif(a.clinician_id, ''), c.clinician_id) clin,
       c.clinician_name clin_name,
       (SELECT dx.diagnosis_code || coalesce(' ' || dx.diagnosis_description, '')
          FROM claim_diagnoses dx WHERE dx.claim_id_fk = c.id
          ORDER BY (dx.diagnosis_type = 'Primary') DESC, dx.sequence_no NULLS LAST LIMIT 1) dx
FROM remittances r
JOIN remittance_activity_details d ON d.remittance_id_fk = r.id
JOIN facilities f ON f.id = r.facility_id AND f.facility_code <> 'GHAF-001'
LEFT JOIN claims c ON c.facility_id = r.facility_id AND c.claim_id = r.claim_id AND c.resubmission_key = ''
LEFT JOIN claim_activities a ON a.claim_id_fk = c.id AND a.activity_id = d.activity_id
WHERE r.remittance_date >= :since AND r.remittance_date < :until
  AND d.denial_code = :code
  {fac} {payer} {stage}
LIMIT 50000
"""


_RATE_SQL = """
SELECT d.activity_code act,
       split_part((SELECT dx.diagnosis_code FROM claim_diagnoses dx WHERE dx.claim_id_fk = c.id
                   ORDER BY (dx.diagnosis_type = 'Primary') DESC, dx.sequence_no NULLS LAST LIMIT 1), ' ', 1) dx,
       count(*) n
FROM remittances r
JOIN remittance_activity_details d ON d.remittance_id_fk = r.id
JOIN facilities f ON f.id = r.facility_id AND f.facility_code <> 'GHAF-001'
LEFT JOIN claims c ON c.facility_id = r.facility_id AND c.claim_id = r.claim_id AND c.resubmission_key = ''
WHERE r.remittance_date >= :since AND r.remittance_date < :until
  AND d.activity_code = ANY(:acts)
  {fac} {payer} {stage}
GROUP BY 1, 2
"""


def _patterns(db, rows, fid, payer, month, months, top_n=12, stage="new"):
    """Service x principal diagnosis pairs behind a denial code, with how often the pair is denied when
    remitted (same RA window, payer and facility), the most used TPA remark and the main clinician."""
    from collections import Counter, defaultdict
    if not rows:
        return []
    g = defaultdict(list)
    for r in rows:
        dxc = (r["dx"] or "—").split(" ", 1)[0]
        g[(r["activity_code"] or "—", dxc)].append(r)
    total = len(rows)
    top = sorted(g.items(), key=lambda kv: -sum(x["amt"] for x in kv[1]))[:top_n]
    since, until = _month_range(month, months)
    sql = _RATE_SQL.format(
        fac="AND r.facility_id = :fid" if fid else "", stage=_stage_sql(stage),
        payer="AND upper(coalesce(nullif(c.receiver_id, ''), nullif(c.payer_id, ''), r.payer_id)) = upper(:payer)" if payer else "")
    billed = {(x.act, x.dx): x.n for x in db.execute(text(sql), {
        "since": since, "until": until, "fid": fid, "payer": payer,
        "acts": list({k[0] for k, _ in top})})}
    out = []
    for (act, dxc), rs in top:
        themes = Counter(t for x in rs for t in x["themes"])
        clin = Counter(x["clin"] for x in rs).most_common(1)
        ex_theme = themes.most_common(1)
        example = None
        if ex_theme:
            example = next((x["tpa_comments"] for x in rs if ex_theme[0][0] in x["themes"]), None)
        n_b = billed.get((act, dxc))
        out.append({
            "activity_code": act, "activity": rs[0]["adesc"], "diagnosis_code": dxc,
            "diagnosis": (rs[0]["dx"] or "").split(" ", 1)[1] if " " in (rs[0]["dx"] or "") else "",
            "lines": len(rs), "claims": len({x["rid"] for x in rs}),
            "amount": round(sum(x["amt"] for x in rs), 2), "share_pct": round(100 * len(rs) / total, 1),
            "remitted_lines": n_b, "denial_rate_pct": round(100 * len(rs) / n_b, 1) if n_b else None,
            "remark": (example or "")[:200] if example else None,
            "remark_lines": ex_theme[0][1] if ex_theme else 0,
            "clinician": clin[0][0] if clin else None,
            "clinician_share_pct": round(100 * clin[0][1] / len(rs)) if clin else None,
        })
    return out

_ID_TOKEN = re.compile(r"\b(?=[A-Za-z0-9/._-]*\d)[A-Za-z0-9/._-]{3,}\b")


def comment_theme(txt: Optional[str]) -> Optional[str]:
    """Normalise a TPA comment into a theme: drop numbering, IDs, dates and amounts."""
    if not txt:
        return None
    t = re.sub(r"^\s*\d+[.)]\s*", "", txt.strip())
    t = _ID_TOKEN.sub("#", t)
    t = re.sub(r"[#\s,;:.-]*#[#\s,;:.-]*", " # ", t)
    t = re.sub(r"[{}\[\]()]", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" #.-").lower()
    t = re.sub(r"^(tag|ref|reference|claim|auth|approval) ?#?$", "", t).strip()
    return t[:160] if len(t) >= 4 else None


def _month_range(month: Optional[str], months: int):
    if month:
        y, m = map(int, month.split("-"))
        start = date(y, m, 1)
        end = date(y + (m == 12), m % 12 + 1, 1)
        return start, end
    return _month_start(months - 1), date.today().replace(day=28) + __import__("datetime").timedelta(days=10)


def _drill_rows(db, fid, payer, code, month, months, stage="new"):
    since, until = _month_range(month, months)
    key = ("drill", fid, payer, code, since, until, stage, date.today())
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    sql = _DRILL_SQL.format(
        fac="AND r.facility_id = :fid" if fid else "", stage=_stage_sql(stage),
        payer="AND upper(coalesce(nullif(c.receiver_id, ''), nullif(c.payer_id, ''), r.payer_id)) = upper(:payer)" if payer else "")
    rows = [dict(r) for r in db.execute(text(sql), {"since": since, "until": until, "code": code,
                                                     "fid": fid, "payer": payer}).mappings()]
    for r in rows:
        r["amt"] = float(r["amt"] or 0)
        # several reasons are often joined with "~" — keep each as its own theme
        parts = [comment_theme(p) for p in re.split(r"~|\n|\s\d+\.\s", r["tpa_comments"] or "")]
        r["themes"] = list(dict.fromkeys(p for p in parts if p))
        r["theme"] = r["themes"][0] if r["themes"] else None
    _CACHE[key] = (time.time(), rows)
    return rows


def _top(rows, keyf, n=10):
    agg = {}
    for r in rows:
        k = keyf(r)
        if not k:
            continue
        a = agg.setdefault(k, {"lines": 0, "amount": 0.0, "claims": set(), "example": r})
        a["lines"] += 1; a["amount"] += r["amt"]; a["claims"].add(r["rid"])
    tot = len(rows) or 1
    out = [{"key": k, "lines": v["lines"], "claims": len(v["claims"]), "amount": round(v["amount"], 2),
            "share_pct": round(100 * v["lines"] / tot, 1), "example": v["example"]} for k, v in agg.items()]
    out.sort(key=lambda x: (-x["lines"], -x["amount"]))
    return out[:n]


@router.get("/drilldown")
def drilldown(
    code: str,
    facility_id: Optional[int] = None,
    payer: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}$"),
    stage: str = Query("new", pattern="^(new|redenied|all)$"),
    months: int = Query(6, ge=1, le=24),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """One denial code: TPA comment themes, services, clinicians, diagnoses, payers and sample claims."""
    fid = _scope(user, facility_id)
    rows = _drill_rows(db, fid, payer, code, month, months, stage)
    with_comment = sum(1 for r in rows if (r["tpa_comments"] or "").strip())
    ref_only = sum(1 for r in rows if (r["tpa_comments"] or "").strip() and not r["themes"])
    exploded = [dict(r, theme=t) for r in rows for t in r["themes"]]
    themes = [{"theme": t["key"], "lines": t["lines"], "claims": t["claims"], "amount": t["amount"],
               "share_pct": round(100 * t["lines"] / (len(rows) or 1), 1),
               "example": (t["example"]["tpa_comments"] or "")[:300]}
              for t in _top(exploded, lambda r: r["theme"], 12)]
    acts = [{"activity_code": t["key"], "description": t["example"]["adesc"], "type": t["example"]["activity_type"],
             "lines": t["lines"], "claims": t["claims"], "amount": t["amount"], "share_pct": t["share_pct"]}
            for t in _top(rows, lambda r: r["activity_code"], 10)]
    clins = [{"clinician_id": t["key"], "name": t["example"]["clin_name"], "lines": t["lines"],
              "claims": t["claims"], "amount": t["amount"], "share_pct": t["share_pct"]}
             for t in _top(rows, lambda r: r["clin"], 10)]
    dxs = [{"diagnosis": t["key"], "lines": t["lines"], "claims": t["claims"], "amount": t["amount"],
            "share_pct": t["share_pct"]} for t in _top(rows, lambda r: r["dx"], 10)]
    pays = [{"payer": t["key"], "payer_name": payer_name(t["key"]), "lines": t["lines"], "claims": t["claims"],
             "amount": t["amount"], "share_pct": t["share_pct"]} for t in _top(rows, lambda r: r["payer"], 8)]
    facs = [{"facility": t["key"], "lines": t["lines"], "amount": t["amount"], "share_pct": t["share_pct"]}
            for t in _top(rows, lambda r: r["facility_name"], 8)]
    samples = sorted(rows, key=lambda r: -r["amt"])[:25]
    return {
        "code": code, "description": denial_description(code), "month": month, "months": months,
        "lines": len(rows), "claims": len({r["rid"] for r in rows}),
        "amount": round(sum(r["amt"] for r in rows), 2),
        "comment_coverage_pct": round(100 * with_comment / len(rows), 1) if rows else None,
        "reference_only_pct": round(100 * ref_only / len(rows), 1) if rows else None,
        "truncated": len(rows) >= 50000,
        "themes": themes, "activities": acts, "clinicians": clins, "diagnoses": dxs,
        "patterns": _patterns(db, rows, fid, payer, month, months, stage=stage), "stage": stage,
        "payers": pays, "facilities": facs,
        "samples": [{"facility_name": r["facility_name"], "claim_id": r["claim_id"],
                     "ra_date": r["remittance_date"].isoformat(), "payer": r["payer"], "insurer": r["ins"],
                     "activity_code": r["activity_code"], "activity": r["adesc"], "clinician": r["clin"],
                     "diagnosis": r["dx"], "amount": round(r["amt"], 2), "comment": r["tpa_comments"]}
                    for r in samples],
    }


@router.get("/drilldown.xlsx")
def drilldown_xlsx(
    code: str,
    facility_id: Optional[int] = None,
    payer: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}$"),
    stage: str = Query("new", pattern="^(new|redenied|all)$"),
    months: int = Query(6, ge=1, le=24),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Every denied line for the code, with comment theme, service, clinician and diagnosis."""
    import io
    from fastapi.responses import StreamingResponse
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    fid = _scope(user, facility_id)
    rows = _drill_rows(db, fid, payer, code, month, months, stage)
    d = drilldown(code=code, facility_id=facility_id, payer=payer, month=month, stage=stage, months=months, db=db, user=user)
    wb = Workbook()

    def sheet(ws, header, data):
        ws.append(header)
        for c in ws[1]:
            c.font, c.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="0B4F6C")
        for r in data:
            ws.append(r)
        ws.freeze_panes = "A2"; ws.auto_filter.ref = ws.dimensions
        for i, h in enumerate(header, 1):
            w = max([len(str(h))] + [len(str(r[i - 1] or "")) for r in data[:300]]) + 2
            ws.column_dimensions[get_column_letter(i)].width = min(w, 60)

    ws = wb.active; ws.title = "Comment themes"
    sheet(ws, ["TPA comment theme", "Lines", "Claims", "Denied AED", "Share %", "Example comment"],
          [[t["theme"], t["lines"], t["claims"], t["amount"], t["share_pct"], t["example"]] for t in d["themes"]])
    sheet(wb.create_sheet("Services"), ["Activity code", "Description", "Type", "Lines", "Claims", "Denied AED", "Share %"],
          [[a["activity_code"], a["description"], a["type"], a["lines"], a["claims"], a["amount"], a["share_pct"]] for a in d["activities"]])
    sheet(wb.create_sheet("Clinicians & diagnoses"), ["Kind", "Clinician / diagnosis", "Name", "Lines", "Claims", "Denied AED", "Share %"],
          [["Clinician", c["clinician_id"], c["name"], c["lines"], c["claims"], c["amount"], c["share_pct"]] for c in d["clinicians"]]
          + [["Diagnosis", x["diagnosis"], "", x["lines"], x["claims"], x["amount"], x["share_pct"]] for x in d["diagnoses"]])
    sheet(wb.create_sheet("All denied lines"),
          ["Facility", "Claim ID", "RA date", "Receiver / TPA", "Insurer", "Member ID", "Encounter", "Activity ID",
           "Activity code", "Activity", "Clinician", "Principal diagnosis", "Denied AED", "Comment theme", "TPA comment"],
          [[r["facility_name"], r["claim_id"], r["remittance_date"].isoformat(), r["payer"], r["ins"], r["member_id"],
            r["enc"].isoformat() if r["enc"] else None, r["activity_id"], r["activity_code"], r["adesc"], r["clin"],
            r["dx"], round(r["amt"], 2), " | ".join(r["themes"]), r["tpa_comments"]] for r in rows])
    about = wb.create_sheet("About")
    for line in [f"Denial code {code}: {d['description'] or ''}",
                 f"RA month {month or f'last {months} months'} · facility {facility_id or 'all'} · TPA {payer or 'all'}",
                 f"{d['lines']:,} denied lines; {d['comment_coverage_pct']}% have a TPA comment (SN 03/2019 6.1.3 makes it mandatory).",
                 "Comment theme = TPA comment with numbering, IDs, dates and amounts removed, so the same reason groups together."]:
        about.append([line])
    about.column_dimensions["A"].width = 110
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    name = f"denial_{code}_{facility_id or 'all'}_{payer or 'all'}_{month or 'window'}.xlsx"
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------------------------------------------------------------------------------------------------
# Resubmission outcomes: what happened to lines the team resubmitted (paid / partly paid / re-denied /
# awaiting the resubmission RA), per payer and ORIGINAL denial code. Re-denied lines go to reconciliation.
_OUTCOME_SQL = """
WITH rs AS (
  SELECT DISTINCT ON (c.facility_id, c.claim_id) c.facility_id, c.claim_id, c.submission_date::date rsd
  FROM claims c
  WHERE c.resubmission_key IN ('internal complaint', 'correction')
    AND c.submission_date >= :since AND c.submission_date < :until
    AND c.facility_id NOT IN (SELECT id FROM facilities WHERE facility_code = 'GHAF-001') {fac_c}
  ORDER BY c.facility_id, c.claim_id, c.submission_date),
pre AS (
  SELECT DISTINCT ON (r.facility_id, r.claim_id) r.id, r.facility_id, r.claim_id
  FROM remittances r JOIN rs USING (facility_id, claim_id)
  WHERE r.remittance_date::date <= rs.rsd
  ORDER BY r.facility_id, r.claim_id, r.remittance_date DESC, r.id DESC),
post AS (
  SELECT DISTINCT ON (r.facility_id, r.claim_id) r.id, r.facility_id, r.claim_id, r.tpa_comments
  FROM remittances r JOIN rs USING (facility_id, claim_id)
  WHERE r.remittance_date::date >= rs.rsd
  ORDER BY r.facility_id, r.claim_id, r.remittance_date, r.id),
c0 AS (
  SELECT DISTINCT ON (c.facility_id, c.claim_id) c.facility_id, c.claim_id,
         upper(coalesce(nullif(c.receiver_id, ''), nullif(c.payer_id, ''))) payer,
         coalesce(nullif(c.receiver_name, ''), c.payer_name) payer_name
  FROM claims c JOIN rs USING (facility_id, claim_id)
  WHERE c.resubmission_key = ''
  ORDER BY c.facility_id, c.claim_id, c.id)
SELECT c0.payer, max(c0.payer_name) payer_name, upper(d0.denial_code) code,
       count(*) lines, count(DISTINCT pre.claim_id) claims,
       sum(greatest(coalesce(d0.net, 0) - coalesce(d0.paid_amount, 0), 0)) denied0,
       count(*) FILTER (WHERE post.id IS NULL) pending,
       count(*) FILTER (WHERE post.id IS NOT NULL AND greatest(coalesce(d1.net, 0) - coalesce(d1.paid_amount, 0), 0) <= 0.01) paid_full,
       count(*) FILTER (WHERE post.id IS NOT NULL AND coalesce(d1.paid_amount, 0) > 0
                        AND greatest(coalesce(d1.net, 0) - coalesce(d1.paid_amount, 0), 0) > 0.01) paid_part,
       count(*) FILTER (WHERE post.id IS NOT NULL AND coalesce(d1.paid_amount, 0) <= 0
                        AND greatest(coalesce(d1.net, 0) - coalesce(d1.paid_amount, 0), 0) > 0.01) redenied,
       sum(CASE WHEN post.id IS NOT NULL THEN least(coalesce(d1.paid_amount, 0),
               greatest(coalesce(d0.net, 0) - coalesce(d0.paid_amount, 0), 0)) ELSE 0 END) recovered,
       sum(CASE WHEN post.id IS NOT NULL THEN greatest(coalesce(d1.net, 0) - coalesce(d1.paid_amount, 0), 0) ELSE 0 END) redenied_amt,
       array_agg(post.tpa_comments) FILTER (WHERE post.id IS NOT NULL AND coalesce(d1.paid_amount, 0) <= 0
                        AND coalesce(post.tpa_comments, '') <> '') remarks
FROM pre
JOIN remittance_activity_details d0 ON d0.remittance_id_fk = pre.id
JOIN c0 ON c0.facility_id = pre.facility_id AND c0.claim_id = pre.claim_id
LEFT JOIN post ON post.facility_id = pre.facility_id AND post.claim_id = pre.claim_id
LEFT JOIN remittance_activity_details d1 ON d1.remittance_id_fk = post.id AND d1.activity_id = d0.activity_id
WHERE coalesce(d0.denial_code, '') <> '' AND coalesce(d0.net, 0) - coalesce(d0.paid_amount, 0) > 0.01
  {payer}
GROUP BY 1, 3
"""


@router.get("/resub-outcomes")
def resub_outcomes(
    facility_id: Optional[int] = None,
    payer: Optional[str] = None,
    months: int = Query(3, ge=1, le=12),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Outcome of resubmitted lines by payer x original denial code (resubmissions sent in the last N months)."""
    from collections import Counter
    fid = _scope(user, facility_id)
    since, until = _month_start(months - 1), date.today() + __import__("datetime").timedelta(days=1)
    key = ("outcomes", fid, payer, months, date.today())
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    sql = _OUTCOME_SQL.format(fac_c="AND c.facility_id = :fid" if fid else "",
                              payer="AND c0.payer = upper(:payer)" if payer else "")
    out = []
    for r in db.execute(text(sql), {"since": since, "until": until, "fid": fid, "payer": payer}).mappings():
        answered = r["paid_full"] + r["paid_part"] + r["redenied"]
        themes = Counter()
        for t in (r["remarks"] or []):
            for p in re.split(r"~|\n|\s\d+\.\s", t or ""):
                th = comment_theme(p)
                if th and not th.startswith("activity id"): themes[th] += 1
        out.append({
            "payer": r["payer"], "payer_name": payer_name(r["payer"], r["payer_name"]),
            "denial_code": r["code"], "description": denial_description(r["code"]),
            "lines": r["lines"], "claims": r["claims"], "denied_amount": round(float(r["denied0"] or 0), 2),
            "pending": r["pending"], "paid_full": r["paid_full"], "paid_part": r["paid_part"], "redenied": r["redenied"],
            "success_pct": round(100 * (r["paid_full"] + r["paid_part"]) / answered, 1) if answered else None,
            "redenied_pct": round(100 * r["redenied"] / answered, 1) if answered else None,
            "recovered": round(float(r["recovered"] or 0), 2), "redenied_amount": round(float(r["redenied_amt"] or 0), 2),
            "top_redenial_remark": themes.most_common(1)[0][0] if themes else None,
        })
    out.sort(key=lambda x: -x["denied_amount"])
    tot = lambda k: sum(x[k] for x in out)
    res = {"months": months, "since": since.isoformat(), "rows": out,
           "totals": {k: round(tot(k), 2) for k in ("lines", "denied_amount", "pending", "paid_full", "paid_part",
                                                    "redenied", "recovered", "redenied_amount")}}
    _CACHE[key] = (time.time(), res)
    return res


# ── categories from the payer's own reason per activity (services/denial_category) ─────────────────────────
_CAT_SQL = _AGG_SQL.replace(
    "       max(d.denial_description)             AS descr,",
    "       upper(coalesce(d.activity_code, '')) AS act,\n       coalesce(nullif(d.denial_description, ''), nullif(r.denial_description, ''), r.tpa_comments, '') AS reason,"
).replace("GROUP BY 1, 2, 4", "GROUP BY 1, 2, 4, 5, 6")


@router.get("/categories")
def categories(
    facility_id: Optional[int] = None,
    payer: Optional[str] = None,
    stage: str = Query("new", pattern="^(new|redenied|all)$"),
    months: int = Query(6, ge=1, le=24),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Denied lines by category, decided from the payer's reason for each activity (not the denial code alone)."""
    from ..services.denial_category import classify
    from ..services.resub_worklist import _matcher, ensure_playbook
    fid = _scope(user, facility_id)
    key = ("cat", fid, payer, months, stage, date.today())
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    sql = _CAT_SQL.format(fac="AND r.facility_id = :fid" if fid else "",
                          stage=_stage_sql(stage if stage != "all" else "x"),
                          payer="AND upper(coalesce(nullif(c.receiver_id, ''), nullif(c.payer_id, ''), r.payer_id)) = upper(:payer)" if payer else "")
    rows = db.execute(text(sql), {"since": _month_start(months), "fid": fid, "payer": payer}).mappings().all()
    match = _matcher(ensure_playbook(db))
    shown = sorted({r["month"] for r in rows})[-months:]
    memo: dict = {}
    by_cat = defaultdict(lambda: {"lines": 0, "amount": 0.0, "months": defaultdict(int), "codes": defaultdict(int)})
    by_code = defaultdict(lambda: defaultdict(int))
    for r in rows:
        if r["month"] not in shown:
            continue
        k = (r["code"], r["act"], r["reason"])
        if k not in memo:
            pb = match(r["code"] or "", r["act"] or "", r["reason"] or "")
            memo[k] = classify(r["reason"], r["act"], pb.category if pb else "Other", r["code"] or "")[0]
        cat = memo[k]
        b = by_cat[cat]
        b["lines"] += r["lines"]; b["amount"] += float(r["amount"] or 0)
        b["months"][r["month"]] += r["lines"]; b["codes"][r["code"]] += r["lines"]
        by_code[r["code"]][cat] += r["lines"]
    total = sum(b["lines"] for b in by_cat.values()) or 1
    out = {
        "months": shown, "basis": AMOUNT_BASIS.get(stage if stage != "all" else "new"),
        "categories": sorted(({"category": c, "lines": b["lines"], "share_pct": round(100.0 * b["lines"] / total, 1),
                               "amount": round(b["amount"], 2), "by_month": [b["months"].get(m, 0) for m in shown],
                               "top_codes": sorted(b["codes"].items(), key=lambda x: -x[1])[:4]}
                              for c, b in by_cat.items()), key=lambda x: -x["amount"]),
        "by_code": {code: sorted(v.items(), key=lambda x: -x[1]) for code, v in by_code.items()},
    }
    _CACHE[key] = (time.time(), out)
    return out
