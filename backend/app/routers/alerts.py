"""
Facility alerts: one list of business problems per facility.

Defaults are learned from each facility's own history (not one fixed number for all)
and anchored on DHA eClaimLink Standards Notice SN 03/2019 (Claim Cycle Standardisation):
  * Submission by the 15th of the month after the service month (3.7.1)
  * First Remittance Advice + payment within 45 days of submission (3.9.1)
  * Resubmission RA within 30 days (3.9.2); resubmit within 21 days of RA (3.8.1)

Alert types
  denial_spike   denial rate (denied / net, first RA) over the last 30 days is well above
                 the facility's own previous 3 months (baseline + max(5 pts, 2 x monthly SD))
  payment_rate   share of claims that got their first RA within 45 days (SN 3.9.1), and
                 paid / net, both against the facility's own baseline
  overdue        claims with no RA more than 45 days after submission (payer breach of SN 3.9.1)
                 above the facility's normal share
  clinician_rate / clinician_consult / clinician_tests / clinician_unspec  a clinician vs same-specialty peers
                 at the facility: MNEC denial rate, consultation-rule denials, lab tests per visit (MNEC),
                 unspecified principal diagnosis share (90 days, new denials)
  clinician_pattern  a clinician's service x principal diagnosis combination denied far more often than the
                 same combination from the facility's other clinicians (new denials, last 90 days)
  submission_gap no first-submission batch when expected: monthly-batch facilities 3 days after their usual
                 batch day; others after their longest normal gap + 3 days (batch days = >= 5% of the month)
  sn_deadline    service month not yet submitted at the usual volume near / after the 15th
  credentials    DHPO / Riayati fetch failing with 401 / login errors
  Denial spikes / above-usual count only NEW denials (first RA); re-denials after a resubmission are left to
                 the reconciliation worklists. Denial spikes / above-usual now count only NEW denials (first RA); each
                 denial alert carries a split: new lines -> resubmit, re-denied lines -> reconciliation.
"""
import statistics
import time
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User
from ..security import resolve_facility, check_facility, allowed_facility_ids, is_clinic_user, get_current_user

router = APIRouter(prefix="/api/alerts", tags=["alerts"])

import threading

_CACHE: dict = {}
_LOCK = threading.Lock()
_TTL = 3600


def _sd(xs):
    return statistics.pstdev(xs) if len(xs) > 1 else 0.0


from functools import lru_cache as _lru


@_lru(maxsize=1)
def _specialties() -> dict:
    """License -> eClaimLink specialty, from data/clinicians.xlsx (DHA_HRD + MOH sheets)."""
    import os
    import openpyxl
    p = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "data", "clinicians.xlsx"))
    out = {}
    try:
        wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
        for ws in wb.worksheets:
            for row in ws.iter_rows(min_row=2, values_only=True):
                if row and row[0] and len(row) > 2 and row[2]:
                    out[str(row[0]).strip().upper()] = " ".join(str(row[2]).replace("\xa0", " ").split())
        wb.close()
    except Exception:
        pass
    return out


def _dd_desc(code):
    try:
        from ..services.denial_codes import denial_description
        return denial_description(code) or ""
    except Exception:
        return ""


def _blank_split():
    return {"new_lines": 0, "new_amt": 0.0, "new_first_ra": None, "re_lines": 0, "re_amt": 0.0, "re_first_ra": None}


def _add_split(sp, redenied, n, amt, first_ra):
    k = "re" if redenied else "new"
    sp[f"{k}_lines"] += int(n); sp[f"{k}_amt"] += float(amt or 0)
    if first_ra and (sp[f"{k}_first_ra"] is None or first_ra < sp[f"{k}_first_ra"]):
        sp[f"{k}_first_ra"] = first_ra


def _finish_split(per_fac: dict, fids) -> dict:
    """Sum the per-facility splits; deadlines are first RA + 21 days (SN 03/2019 3.8.1)."""
    out = _blank_split()
    for f in fids:
        sp = per_fac.get(f)
        if not sp: continue
        for k in ("new", "re"):
            _add_split(out, k == "re", sp[f"{k}_lines"], sp[f"{k}_amt"], sp[f"{k}_first_ra"])
    d = out.pop("new_first_ra")
    out["new_due"] = (d + timedelta(days=21)).isoformat() if d else None   # resubmit within 21 days (3.8.1)
    out.pop("re_first_ra")
    out["re_due"] = None   # reconciliation: no 21-day limit (SN 03/2019 3.10.4), only after insurer agreement
    out["new_amt"] = round(out["new_amt"], 2); out["re_amt"] = round(out["re_amt"], 2)
    return out


def _month_start(d: date, back: int = 0) -> date:
    y, m = d.year, d.month - back
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, 1)


def _stopped_facilities() -> set:
    """Facility ids in data/stopped_facilities.json (service with Ghaf stopped; data kept, no alerts)."""
    import json
    from pathlib import Path
    f = Path(__file__).resolve().parents[2] / "data" / "stopped_facilities.json"
    try:
        return {int(k) for k in json.loads(f.read_text()) if k.isdigit()}
    except Exception:
        return set()


def _compute(db: Session, today: date) -> dict:  # today can be set in the past to back-test
    facs = {r.id: r.facility_name for r in db.execute(text(
        "SELECT id, facility_name FROM facilities WHERE is_active AND facility_code <> 'GHAF-001'"))
        if r.id not in _stopped_facilities()}   # service stopped: data kept, no alerts
    if not facs:
        return {"items": [], "baselines": []}
    ids = list(facs)
    alerts, base = [], {f: {"facility_id": f, "facility_name": n} for f, n in facs.items()}

    def add(fid, kind, severity, title, detail, link, value=None, threshold=None):
        alerts.append({"facility_id": fid, "facility_name": facs[fid], "type": kind,
                       "severity": severity, "title": title, "detail": detail, "link": link,
                       "value": value, "threshold": threshold})

    # ---- 1. Denial spike (by RA date, first RA per claim) -----------------------------
    m0 = _month_start(today)           # current month start
    start = _month_start(today, 6)     # 6 months of history for the usual rate
    rows = db.execute(text("""
        WITH r AS (
          SELECT DISTINCT ON (facility_id, claim_id) facility_id, remittance_date d, net_amount net,
                 paid_amount paid
          FROM remittances
          WHERE remittance_date >= :start AND facility_id = ANY(:ids) AND net_amount > 0
          ORDER BY facility_id, claim_id, remittance_date, id)
        SELECT facility_id,
               CASE WHEN d >= :recent THEN 'recent' ELSE to_char(d, 'YYYY-MM') END bucket,
               sum(net) net, sum(greatest(net - coalesce(paid, 0), 0)) denied, count(*) n
        FROM r GROUP BY 1, 2
    """), {"start": start, "ids": ids, "recent": today - timedelta(days=30)}).all()
    by = {}
    for r in rows:
        by.setdefault(r.facility_id, {})[r.bucket] = (float(r.denied) / float(r.net) * 100, int(r.n))
    for fid, b in by.items():
        hist = [v[0] for k, v in b.items() if k != "recent" and v[1] >= 30
                and k < m0.strftime("%Y-%m")]
        if len(hist) < 2 or "recent" not in b or b["recent"][1] < 30:
            continue
        mean = statistics.mean(hist)
        # usual + 5 points or usual x 1.25, whichever is larger (a 2-SD band on a few jumpy months
        # pushed some thresholds to 70-80% and the alert never fired)
        thr = max(mean + 5.0, mean * 1.25)
        cur = b["recent"][0]
        base[fid].update(denial_rate_baseline=round(mean, 1), denial_rate_threshold=round(thr, 1),
                         denial_rate_last30=round(cur, 1))
        if cur > thr:
            add(fid, "denial_spike", "high" if cur > thr + 5 else "medium",
                f"Denial rate {cur:.1f}% in the last 30 days",
                f"Usual {mean:.1f}% (previous 3 months); alert above {thr:.1f}%.",
                "/denial-trends", round(cur, 1), round(thr, 1))

    # ---- 1b. Early warning: denial code spike per receiver (TPA) x code, week by week ---------
    # Last 7 RA days vs the 8 weeks before. Catches a payer rule change in its first week
    # (e.g. NAS / TPA004 MNEC-003, week of 5 Oct 2026: 2,448 lines vs ~900/week, all facilities).
    wk_end = today - timedelta(days=1)
    wk_start = wk_end - timedelta(days=6)
    rows = db.execute(text("""
        WITH r AS (
          SELECT id, facility_id, claim_id, remittance_date, EXISTS (SELECT 1 FROM claims x WHERE x.facility_id = remittances.facility_id
                         AND x.claim_id = remittances.claim_id
                         AND x.resubmission_key IN ('internal complaint', 'correction', 'reconciliation')
                         AND x.submission_date::date < remittances.remittance_date::date) AS redenied
          FROM remittances
          WHERE remittance_date BETWEEN :b0 AND :e AND facility_id = ANY(:ids)),
        c AS (
          SELECT DISTINCT ON (c.facility_id, c.claim_id) c.facility_id, c.claim_id,
                 coalesce(nullif(c.receiver_id, ''), c.payer_id) rec
          FROM claims c JOIN (SELECT DISTINCT facility_id, claim_id FROM r) k
            ON k.facility_id = c.facility_id AND k.claim_id = c.claim_id
          ORDER BY c.facility_id, c.claim_id, c.submission_date)
        SELECT c.rec, d.denial_code code, r.facility_id,
               CASE WHEN r.remittance_date >= :w0 THEN -1
                    ELSE ((:w0 - r.remittance_date) - 1) / 7 END wk,
               r.redenied, min(r.remittance_date) first_ra,
               count(*) n, sum(greatest(coalesce(d.net, 0) - coalesce(d.paid_amount, 0), 0)) amt
        FROM remittance_activity_details d
        JOIN r ON r.id = d.remittance_id_fk
        JOIN c ON c.facility_id = r.facility_id AND c.claim_id = r.claim_id
        WHERE coalesce(d.denial_code, '') <> ''
        GROUP BY 1, 2, 3, 4, 5
    """), {"b0": wk_start - timedelta(days=56), "e": wk_end, "w0": wk_start, "ids": ids}).all()
    from ..services.payer_names import payer_name
    agg = {}   # (rec, code) -> {"cur": n, "amt": x, "hist": [8], "facs": {fid: n}, "base_facs": set}
    for r in rows:
        a = agg.setdefault((r.rec, r.code), {"cur": 0, "amt": 0.0, "hist": [0] * 8, "facs": {}, "fac_hist": {},
                                             "split": {}})
        if r.wk == -1:   # this week's lines, split by stage per facility (shown on the card)
            sp = a["split"].setdefault(r.facility_id, _blank_split())
            _add_split(sp, r.redenied, r.n, r.amt, r.first_ra)
        if r.redenied:
            continue     # re-denials after a resubmission don't count towards the spike
        if r.wk == -1:
            a["cur"] += r.n; a["amt"] += float(r.amt or 0)
            a["facs"][r.facility_id] = a["facs"].get(r.facility_id, 0) + r.n
        elif 0 <= r.wk < 8:
            a["hist"][r.wk] += r.n
            fh = a["fac_hist"].setdefault(r.facility_id, [0] * 8)
            fh[r.wk] += r.n
    rec_facs = {}
    for (rec, _), a in agg.items():
        rec_facs.setdefault(rec, set()).update(a["facs"]); rec_facs[rec].update(a["fac_hist"])
    for (rec, code), a in agg.items():
        mean, sd = statistics.mean(a["hist"]), _sd(a["hist"])
        cur = a["cur"]
        new_code = mean < 2 and cur >= 30
        spike = cur >= 50 and cur >= 2 * mean and cur >= mean + 3 * sd
        if not (spike or new_code) or a["amt"] < 2000:
            continue
        hit = [f for f, n in a["facs"].items()
               if n >= max(10, 2 * statistics.mean(a["fac_hist"].get(f, [0] * 8)))]
        wide = len(hit) >= max(2, 0.5 * len(rec_facs.get(rec, ())))
        rname = payer_name(rec) or rec
        ratio = f"{cur / mean:.1f}x usual" if mean >= 2 else "new this week"
        title = f"{rname}: {code} denials {cur:,} lines in 7 days ({ratio})"
        detail = (f"Usual {mean:,.0f}/week over the previous 8 weeks; AED {a['amt']:,.0f} denied this week. "
                  + (f"Seen across {len(hit)} facilities — likely a payer-side rule or adjudication change: "
                     "raise with the TPA." if wide else
                     "Limited to this facility — check coding / documentation and the scrubber rule."))
        targets = hit if (hit and not wide) else [None]
        for fid in targets:
            t_ = title
            if fid:
                fm = statistics.mean(a["fac_hist"].get(fid, [0] * 8))
                fn = a["facs"][fid]
                t_ = (f"{rname}: {code} denials {fn:,} lines in 7 days ("
                      + (f"{fn / fm:.1f}x usual" if fm >= 2 else "new this week") + ")")
            alerts.append({"facility_id": fid,
                           "facility_name": facs[fid] if fid else f"All facilities ({len(hit)})",
                           "facilities": hit, "type": "denial_code_spike", "code": code, "payer": rec,
                           "split": _finish_split(a["split"], [fid] if fid else hit),
                           "severity": "high" if (wide or cur >= 3 * max(mean, 1)) else "medium",
                           "title": t_, "detail": detail,
                           "link": f"/denial-trends?payer={rec}", "value": cur, "threshold": round(2 * mean)})

    # ---- 1c. Denial code above its usual level this month (per facility x TPA x code) --------
    # Month-to-date vs the same code's average of the 3 full months before, pro-rated by days.
    # Catches the slower build-up the weekly check can miss.
    if today.day >= 5:
        rows = db.execute(text("""
            WITH r AS (
              SELECT id, facility_id, claim_id, remittance_date, EXISTS (SELECT 1 FROM claims x WHERE x.facility_id = remittances.facility_id
                         AND x.claim_id = remittances.claim_id
                         AND x.resubmission_key IN ('internal complaint', 'correction', 'reconciliation')
                         AND x.submission_date::date < remittances.remittance_date::date) AS redenied
              FROM remittances
              WHERE remittance_date >= :s AND remittance_date < :t AND facility_id = ANY(:ids)),
            c AS (
              SELECT DISTINCT ON (c.facility_id, c.claim_id) c.facility_id, c.claim_id,
                     coalesce(nullif(c.receiver_id, ''), c.payer_id) rec
              FROM claims c JOIN (SELECT DISTINCT facility_id, claim_id FROM r) k
                ON k.facility_id = c.facility_id AND k.claim_id = c.claim_id
              ORDER BY c.facility_id, c.claim_id, c.submission_date)
            SELECT r.facility_id, c.rec, d.denial_code code, (r.remittance_date >= :m0) cur, r.redenied,
                   min(r.remittance_date) first_ra,
                   count(*) n, sum(greatest(coalesce(d.net, 0) - coalesce(d.paid_amount, 0), 0)) amt
            FROM remittance_activity_details d
            JOIN r ON r.id = d.remittance_id_fk
            JOIN c ON c.facility_id = r.facility_id AND c.claim_id = r.claim_id
            WHERE coalesce(d.denial_code, '') <> ''
            GROUP BY 1, 2, 3, 4, 5
        """), {"s": _month_start(today, 3), "t": today, "m0": m0, "ids": ids}).all()
        from ..services.payer_names import payer_name as _pn
        from ..services.denial_codes import denial_description as _dd
        cur, base3, msplit = {}, {}, {}
        for r in rows:
            k = (r.facility_id, r.rec, r.code)
            if r.cur:
                _add_split(msplit.setdefault(k, _blank_split()), r.redenied, r.n, r.amt, r.first_ra)
            if r.redenied:
                continue   # only new denials (first RA) are compared with the usual level
            if r.cur:
                cur[k] = (r.n, float(r.amt or 0))
            else:
                base3[k] = base3.get(k, 0) + r.n
        frac = (today - m0).days / 30.4
        weekly_hit = {(a["facility_id"], a["title"].split(":")[1].split()[0] if ":" in a["title"] else "")
                      for a in alerts if a["type"] == "denial_code_spike"}
        cands = []
        for (fid, rec, code), (n, amt) in cur.items():
            usual = base3.get((fid, rec, code), 0) / 3 * frac
            if n < 50 or amt < 3000 or usual < 5 or n < 2 * usual:
                continue
            if (fid, code) in weekly_hit:
                continue   # already reported by the weekly check
            cands.append((amt, fid, rec, code, n, usual))
        cands.sort(reverse=True)
        MAX_ABOVE = 10
        for amt, fid, rec, code, n, usual in cands[:MAX_ABOVE]:
            pct = round(100 * (n - usual) / usual)
            add(fid, "denial_above_usual", "high" if n >= 2.5 * usual else "medium",
                f"{_pn(rec) or rec}: {code} {n:,} new denial lines this month (+{pct}% vs usual)",
                f"{_dd(code) or ''}. Usual ≈ {usual:,.0f} lines by this date (3-month average); "
                f"AED {amt:,.0f} denied so far. Open 'Why?' on Denial trends for the TPA reasons.",
                f"/denial-trends?payer={rec}&month={today:%Y-%m}", n, round(2 * usual))
            alerts[-1].update(code=code, payer=rec, split=_finish_split({fid: msplit.get((fid, rec, code), _blank_split())}, [fid]))
        if len(cands) > MAX_ABOVE:
            alerts.append({"facility_id": None, "facility_name": "All facilities", "facilities": [],
                           "type": "denial_above_usual", "severity": "medium",
                           "title": f"{len(cands) - MAX_ABOVE} more denial codes above usual this month",
                           "detail": "Smaller amounts, not listed. See 'Above-usual first' on Denial trends.",
                           "link": f"/denial-trends?month={today:%Y-%m}", "value": None, "threshold": None})

    # (Reconciliation of re-denied claims is not a facility alert: it belongs to the resubmission /
    #  reconciliation worklists. Spikes above count only NEW denials, so re-denials don't inflate them.)

    # ---- 1e/1f. Clinician patterns (NEW denials, last 90 days, MNEC = medical-necessity denials) ---------
    # Clinician-driven denials are medical-necessity (MNEC) denials, so the clinician checks count MNEC only,
    # except the consultation-rule check, which looks at consultation-code denials (billing rules).
    #   clinician_pattern  service x principal Dx denied (MNEC) far more than the facility's other clinicians
    #   clinician_rate     MNEC denial rate vs same-specialty peers at the facility
    #   clinician_consult  consultation lines denied under consultation rules (PRCE / CLAI / MNEC) vs peers
    #   clinician_tests    lab tests per visit vs peers, with MNEC denials (+ the most denied CPT)
    #   clinician_unspec   share of visits with an unspecified principal Dx vs peers, with MNEC/CODE denials
    # Each clinician alert also carries: payer spread (one payer or all), MNEC rate by RA month, and the
    # clinician's Documentation review findings.
    _CL_CTE = """
        WITH r AS (
          SELECT r.id, r.facility_id, r.claim_id, r.remittance_date FROM remittances r
          WHERE r.remittance_date >= :s AND r.facility_id = ANY(:ids)
            AND NOT EXISTS (SELECT 1 FROM claims x WHERE x.facility_id = r.facility_id AND x.claim_id = r.claim_id
                            AND x.resubmission_key <> '' AND x.submission_date::date < r.remittance_date::date)),
        c AS (
          SELECT DISTINCT ON (c.facility_id, c.claim_id) c.id, c.facility_id, c.claim_id, c.clinician_id,
                 c.clinician_name, upper(coalesce(nullif(c.receiver_id, ''), c.payer_id)) rec
          FROM claims c JOIN (SELECT DISTINCT facility_id, claim_id FROM r) k USING (facility_id, claim_id)
          WHERE c.resubmission_key = '' ORDER BY c.facility_id, c.claim_id, c.id),
        dx AS (
          SELECT DISTINCT ON (d.claim_id_fk) d.claim_id_fk, d.diagnosis_code FROM claim_diagnoses d
          JOIN c ON c.id = d.claim_id_fk
          ORDER BY d.claim_id_fk, (d.diagnosis_type ILIKE ANY (ARRAY['principal','primary'])) DESC, d.sequence_no NULLS LAST)
    """
    # build the clinician base (90-day first-submission RAs, claims, principal Dx) once instead of in each query
    _P0 = {"s": today - timedelta(days=90), "ids": ids}
    db.execute(text("DROP TABLE IF EXISTS _cl_r, _cl_c, _cl_dx"))
    for _t in ("r", "c", "dx"):
        db.execute(text(f"CREATE TEMP TABLE _cl_{_t} AS " + _CL_CTE + f" SELECT * FROM {_t}"), _P0)
    db.execute(text("CREATE INDEX ON _cl_r (facility_id, claim_id); CREATE INDEX ON _cl_r (id);"
                    " CREATE INDEX ON _cl_c (facility_id, claim_id); CREATE INDEX ON _cl_c (id);"
                    " CREATE INDEX ON _cl_dx (claim_id_fk); ANALYZE _cl_r; ANALYZE _cl_c; ANALYZE _cl_dx"))
    _CL_CTE = "WITH r AS (SELECT * FROM _cl_r), c AS (SELECT * FROM _cl_c), dx AS (SELECT * FROM _cl_dx) "
    _MN ="ad.denial_code ILIKE 'MNEC%' AND coalesce(ad.net, 0) - coalesce(ad.paid_amount, 0) > 0.01"
    _DEN = "coalesce(ad.denial_code, '') <> '' AND coalesce(ad.net, 0) - coalesce(ad.paid_amount, 0) > 0.01"
    _AMT = "greatest(coalesce(ad.net, 0) - coalesce(ad.paid_amount, 0), 0)"
    _P = {"s": today - timedelta(days=90), "ids": ids}
    rows = db.execute(text(_CL_CTE + f"""
        SELECT r.facility_id, coalesce(nullif(a.clinician_id, ''), c.clinician_id) clin, max(c.clinician_name) clin_name,
               ad.activity_code act, max(a.activity_description) act_desc, dx.diagnosis_code dx, c.rec,
               count(*) n,
               count(*) FILTER (WHERE {_DEN}) den,
               coalesce(sum({_AMT}) FILTER (WHERE {_DEN}), 0) amt,
               count(*) FILTER (WHERE {_MN}) mden,
               coalesce(sum({_AMT}) FILTER (WHERE {_MN}), 0) mamt,
               coalesce(sum({_AMT}) FILTER (WHERE ad.denial_code ILIKE 'CODE%'), 0) codeamt,
               mode() WITHIN GROUP (ORDER BY ad.denial_code) FILTER (WHERE {_MN}) mcode,
               mode() WITHIN GROUP (ORDER BY ad.denial_code) FILTER (WHERE {_DEN}) code
        FROM r JOIN remittance_activity_details ad ON ad.remittance_id_fk = r.id
        JOIN c ON c.facility_id = r.facility_id AND c.claim_id = r.claim_id
        LEFT JOIN claim_activities a ON a.claim_id_fk = c.id AND a.activity_id = ad.activity_id
        LEFT JOIN dx ON dx.claim_id_fk = c.id
        WHERE dx.diagnosis_code IS NOT NULL AND coalesce(ad.activity_code, '') <> ''
        GROUP BY r.facility_id, 2, ad.activity_code, dx.diagnosis_code, c.rec
    """), _P).all()
    trend_rows = db.execute(text(_CL_CTE + f"""
        SELECT r.facility_id, coalesce(nullif(a.clinician_id, ''), c.clinician_id) clin,
               to_char(r.remittance_date, 'YYYY-MM') m, count(*) n, count(*) FILTER (WHERE {_MN}) mden
        FROM r JOIN remittance_activity_details ad ON ad.remittance_id_fk = r.id
        JOIN c ON c.facility_id = r.facility_id AND c.claim_id = r.claim_id
        LEFT JOIN claim_activities a ON a.claim_id_fk = c.id AND a.activity_id = ad.activity_id
        GROUP BY 1, 2, 3
    """), _P).all()
    visit_rows = db.execute(text(_CL_CTE + """
        SELECT c.facility_id, c.clinician_id clin, count(DISTINCT c.id) claims,
               count(DISTINCT c.id) FILTER (WHERE dx.diagnosis_code ~ '^R' OR dx.diagnosis_code ~ '\\.9$'
                                            OR dx.diagnosis_code ~ '^[A-Z][0-9]{2}\\.?9$') unspec
        FROM c LEFT JOIN dx ON dx.claim_id_fk = c.id GROUP BY 1, 2
    """), _P).all()
    doc_rows = db.execute(text("""
        SELECT facility_id, upper(clinician_id) clin, count(*) n, mode() WITHIN GROUP (ORDER BY category) cat,
               max(month) last_month, coalesce(sum(amount_at_risk), 0) risk
        FROM doc_findings WHERE facility_id = ANY(:ids) AND month >= :m AND coalesce(clinician_id, '') <> ''
        GROUP BY 1, 2
    """), {"ids": ids, "m": _month_start(today, 4).strftime("%Y-%m")}).all()
    _CONS_CODES = ["9", "10", "11", "12", "13", "14", "9.01", "9.02", "10.01", "10.02", "11.01", "11.02",
                   "22", "23", "24", "25"]
    # consultation-rule denials counted per line (PRCE / CLAI / MNEC), with the gap to the patient's previous visit
    cons_rows = db.execute(text(_CL_CTE + f"""
        SELECT r.facility_id, coalesce(nullif(a.clinician_id, ''), c.clinician_id) clin, ad.denial_code code, c.rec,
               ad.activity_code act,
               coalesce(ad.denial_description, '') reason,
               count(*) den, coalesce(sum({_AMT}), 0) amt,
               count(*) FILTER (WHERE EXISTS (SELECT 1 FROM claims p WHERE p.facility_id = r.facility_id
                   AND p.member_id = cl.member_id AND coalesce(cl.member_id, '') <> '' AND p.resubmission_key = ''
                   AND p.encounter_start::date < cl.encounter_start::date
                   AND p.encounter_start::date >= cl.encounter_start::date - 30)) prior30
        FROM r JOIN remittance_activity_details ad ON ad.remittance_id_fk = r.id
        JOIN c ON c.facility_id = r.facility_id AND c.claim_id = r.claim_id
        JOIN claims cl ON cl.id = c.id
        LEFT JOIN claim_activities a ON a.claim_id_fk = c.id AND a.activity_id = ad.activity_id
        WHERE ad.activity_code = ANY(:cc) AND ad.denial_code ~* '^(PRCE|CLAI|MNEC)' AND {_DEN}
        GROUP BY 1, 2, 3, 4, 5, 6
    """), {**_P, "cc": _CONS_CODES}).all()
    # keep only lines whose payer reason is a visit rule (not lab frequency, referral, licence ... on the same claim)
    from ..services.denial_category import classify
    _cc = {}

    def _visit_rule(r):
        k = (r.reason or "", r.act or "", r.code or "")
        if k not in _cc:
            _cc[k] = classify(k[0], k[1], "Consultation visit rules", k[2])[0] == "Consultation visit rules"
        return _cc[k]
    cons_rows = [r for r in cons_rows if _visit_rule(r)]
    # lab lines: repeated for the same member within 180 days? (MNEC denial rate repeat vs first-time)
    rep_rows = db.execute(text(_CL_CTE + f"""
        SELECT r.facility_id, coalesce(nullif(a.clinician_id, ''), c.clinician_id) clin, ad.activity_code act,
               count(*) n, count(*) FILTER (WHERE {_MN}) den,
               count(*) FILTER (WHERE rp) rn, count(*) FILTER (WHERE rp AND {_MN}) rden
        FROM r JOIN remittance_activity_details ad ON ad.remittance_id_fk = r.id
        JOIN c ON c.facility_id = r.facility_id AND c.claim_id = r.claim_id
        JOIN claims cl ON cl.id = c.id
        LEFT JOIN claim_activities a ON a.claim_id_fk = c.id AND a.activity_id = ad.activity_id
        CROSS JOIN LATERAL (SELECT EXISTS (SELECT 1 FROM claims p JOIN claim_activities pa ON pa.claim_id_fk = p.id
                   WHERE p.facility_id = r.facility_id AND p.member_id = cl.member_id AND coalesce(cl.member_id, '') <> ''
                   AND p.resubmission_key = '' AND pa.activity_code = ad.activity_code
                   AND p.encounter_start::date < cl.encounter_start::date
                   AND p.encounter_start::date >= cl.encounter_start::date - 180) rp) z
        WHERE ad.activity_code ~ '^8[0-9]{{4}}$'
        GROUP BY 1, 2, 3
    """), _P).all()
    rep = {(r.facility_id, r.clin, r.act): r for r in rep_rows}
    from ..services.payer_names import payer_name as _pn3
    from collections import defaultdict as _dd3
    visits = {(r.facility_id, r.clin): r.claims for r in visit_rows}
    unspec = {(r.facility_id, r.clin): r.unspec for r in visit_rows}
    trend = _dd3(dict)
    for r in trend_rows:
        if r.clin:
            trend[(r.facility_id, r.clin)][r.m] = (r.n, r.mden)
    docs = {(r.facility_id, r.clin): r for r in doc_rows}

    def is_unspec(dxc):
        import re as _re
        d = (dxc or "").upper()
        return d.startswith("R") or d.endswith(".9") or bool(_re.fullmatch(r"[A-Z]\d{2}\.?9", d))

    def cname(clin, nm):
        if nm: return nm
        try:
            from .reports import _clinician_name
            n_ = _clinician_name(clin)
            return n_ if n_ and n_ != clin else ""
        except Exception:
            return ""

    def payer_note(pay: dict):
        tot = sum(pay.values())
        if tot <= 0: return None
        top = max(pay, key=pay.get); share = pay[top] / tot
        nm = _pn3(top) or top
        if share >= 0.8:
            return f"Mainly one payer: {nm} ({top}) — {share:.0%} of the MNEC amount"
        return f"Across {sum(1 for v in pay.values() if v > 0)} payers; largest {nm} ({top}) {share:.0%} of the MNEC amount"

    def facts_for(fid, clin, pay):
        out = []
        pn_ = payer_note(pay)
        if pn_: out.append(pn_)
        t = trend.get((fid, clin), {})
        ms = sorted(t)[-4:]
        if len(ms) >= 2:
            parts = [f"{date(int(m[:4]), int(m[5:]), 1):%b} {round(100 * t[m][1] / t[m][0]) if t[m][0] else 0}%" for m in ms]
            first = t[ms[0]][1] / t[ms[0]][0] if t[ms[0]][0] else 0
            last = t[ms[-1]][1] / t[ms[-1]][0] if t[ms[-1]][0] else 0
            dirn = "falling" if last < first - 0.05 else "rising" if last > first + 0.05 else "steady"
            out.append(f"MNEC rate by RA month: {' · '.join(parts)} ({dirn})")
        d = docs.get((fid, clin.upper()))
        if d:
            out.append(f"Documentation review: {d.n:,} findings up to {d.last_month}, mostly '{(d.cat or '').split('. ', 1)[-1]}'"
                       + (f" (AED {float(d.risk):,.0f} at risk)" if d.risk else ""))
        return out

    # -- "Likely cause" strip: rule-based reading of the facts above (shown separately from the facts) --
    def _share(pay):
        tot = sum(pay.values())
        if tot <= 0: return None, 0
        top = max(pay, key=pay.get)
        return top, pay[top] / tot

    def insight_for(kind, fid, clin, d, pay, cons=None):
        i = _insight(kind, fid, clin, d, pay, cons)
        doc = docs.get((fid, clin.upper()))
        if doc:
            i["doc_month"] = doc.last_month
        return i

    def _insight(kind, fid, clin, d, pay, cons=None):
        ev = []
        doc = docs.get((fid, clin.upper()))
        if kind == "clinician_consult":
            top, sh = _share(cons["cpay"])
            pr = cons["cprior"] / cons["cden"] if cons["cden"] else 0
            if sh >= 0.8:
                ev = [{"v": f"{sh:.0%}", "l": f"one payer ({_pn3(top) or top})"}]
                if pr >= 0.6:
                    ev.append({"v": f"{pr:.0%}", "l": "within 30 days of the last visit"})
                    return {"cause": "payer", "label": "Payer follow-up rule", "evidence": ev,
                            "act": "Clinic admin / billing: check the payer's follow-up window (.01 / .02)"}
                return {"cause": "payer", "label": "Payer rule", "evidence": ev, "act": "Clinic admin / billing"}
            return {"cause": "unclear", "label": "Unclear", "evidence": [{"v": f"{d['rate_pct']}% vs {d['peers_rate_pct']}%", "l": "denied vs peers"}],
                    "act": "Review a sample of claims"}
        if kind == "clinician_tests":
            tc = d["top_cpt"][0] if d["top_cpt"] else None
            r = rep.get((fid, clin, tc["code"])) if tc else None
            if r and r.rn >= 10 and r.den and r.n > r.rn:
                rr, fr = r.rden / r.rn, (r.den - r.rden) / (r.n - r.rn)
                if rr >= fr + 0.10 and r.rden / r.den >= 0.3:
                    return {"cause": "repeat", "label": "Repeat test ≤ 6 months",
                            "evidence": [{"v": f"{rr:.0%} vs {fr:.0%}", "l": f"{tc['code']} denied: repeat vs first test"},
                                         {"v": f"{r.rden}/{r.den}", "l": f"denied {tc['code']} were repeats"}],
                            "act": "Clinician + scrubber repeat-test check"}
            return {"cause": "habit", "label": "Ordering habit",
                    "evidence": [{"v": f"{d['tests_per_visit']} vs {d['peers_tests_per_visit']}", "l": "tests per visit vs peers"}]
                    + ([{"v": f"AED {tc['amount']:,.0f}", "l": f"{tc['code']} most denied"}] if tc else []),
                    "act": "Clinician (ordering practice)"}
        if kind == "clinician_unspec":
            ev = [{"v": f"{d['share_pct']}% vs {d['peers_share_pct']}%", "l": "unspecified dx vs peers"}]
            if doc: ev.append({"v": f"{doc.n:,}", "l": "documentation findings"})
            return {"cause": "doc", "label": "Documentation / coding", "evidence": ev, "act": "Coder + clinician feedback on specificity"}
        top, sh = _share(pay)
        if sh >= 0.8:
            return {"cause": "payer", "label": "Payer rule", "evidence": [{"v": f"{sh:.0%}", "l": f"one payer ({_pn3(top) or top})"}],
                    "act": "Clinic admin / payer"}
        if doc:
            return {"cause": "doc", "label": "Documentation", "evidence": [{"v": f"{doc.n:,}", "l": "documentation findings"},
                    {"v": f"{sh:.0%}", "l": "largest payer share"}], "act": "Clinician (case-sheet documentation)"}
        return {"cause": "unclear", "label": "Unclear", "evidence": [{"v": f"{sh:.0%}", "l": "largest payer share"}],
                "act": "Review a sample of claims"}

    # -- service x principal Dx patterns (MNEC) --
    pair_tot = _dd3(lambda: [0, 0])
    cl = {}
    for r in rows:
        pair_tot[(r.facility_id, r.act, r.dx)][0] += r.n
        pair_tot[(r.facility_id, r.act, r.dx)][1] += r.mden
        k = (r.facility_id, r.clin, r.act, r.dx)
        x = cl.setdefault(k, {"n": 0, "den": 0, "amt": 0.0, "name": r.clin_name, "act_desc": r.act_desc,
                              "payers": _dd3(float), "codes": _dd3(int)})
        x["n"] += r.n; x["den"] += r.mden; x["amt"] += float(r.mamt or 0)
        x["payers"][r.rec] += float(r.mamt or 0)
        if r.mcode: x["codes"][r.mcode] += r.mden
    by_clin = _dd3(list)
    for (fid, clin, act, dx), x in cl.items():
        if not clin or x["n"] < 15 or x["den"] < 10 or x["amt"] < 500:
            continue
        rate = x["den"] / x["n"]
        tn, td = pair_tot[(fid, act, dx)]
        on, od = tn - x["n"], td - x["den"]
        orate = od / on if on else None
        if on >= 10:
            if not (rate >= 0.5 and rate - orate >= 0.25):
                continue
        elif rate < 0.7:
            continue
        payer = max(x["payers"], key=x["payers"].get) if x["payers"] else None
        by_clin[(fid, clin)].append({
            "activity_code": act, "activity": x["act_desc"], "diagnosis_code": dx, "lines": x["n"], "denied": x["den"],
            "rate_pct": round(100 * rate), "others_rate_pct": round(100 * orate) if orate is not None else None,
            "others_lines": on, "amount": round(x["amt"], 2), "payer": payer, "payer_name": _pn3(payer) if payer else None,
            "denial_code": max(x["codes"], key=x["codes"].get) if x["codes"] else None, "name": x["name"],
            "_payers": dict(x["payers"])})
    per_fac = _dd3(int)
    for (fid, clin), pats in sorted(by_clin.items(), key=lambda kv: -sum(p["amount"] for p in kv[1])):
        if per_fac[fid] >= 3:
            continue
        per_fac[fid] += 1
        pats.sort(key=lambda p: -p["amount"])
        name = cname(clin, pats[0]["name"])
        tot = sum(p["amount"] for p in pats)
        top = pats[0]
        pay = _dd3(float)
        for p in pats:
            for k_, v_ in p.pop("_payers").items(): pay[k_] += v_
        add(fid, "clinician_pattern", "medium",
            f"{name.title() + ' ' if name else ''}({clin}): {len(pats)} service × diagnosis pattern{'s' if len(pats) > 1 else ''} "
            f"denied as not medically necessary far more than other clinicians",
            f"Top: {top['activity_code']} with {top['diagnosis_code']} — {top['denied']} of {top['lines']} lines denied MNEC "
            f"({top['rate_pct']}%" + (f" vs {top['others_rate_pct']}% for other clinicians" if top['others_rate_pct'] is not None else "")
            + f"). AED {tot:,.0f} across these patterns in 90 days (new denials only).",
            "/denial-trends", round(tot), None)
        alerts[-1].update(clinician=clin, clinician_name=name, patterns=pats[:6], facts=facts_for(fid, clin, pay),
                          insight=insight_for("clinician_pattern", fid, clin, None, pay))

    # -- peer comparisons within the same specialty at the facility --
    spec = _specialties()
    CONSULT = {"9", "10", "11", "12", "13", "14", "9.01", "9.02", "10.01", "10.02", "11.01", "11.02",
               "22", "23", "24", "25"}

    def is_lab(a):
        return bool(a) and len(a) == 5 and a.isdigit() and 80000 <= int(a) <= 89999
    agg = _dd3(lambda: {"n": 0, "mden": 0, "mamt": 0.0, "cn": 0, "cden": 0, "camt": 0.0, "ccodes": _dd3(int),
                        "lab": 0, "lmn": 0, "lmamt": 0.0, "labcodes": _dd3(lambda: [0, 0.0]), "name": None,
                        "pay": _dd3(float), "uamt": 0.0, "udx": _dd3(int),
                        "cpay": _dd3(float), "cprior": 0})
    for r in rows:
        if not r.clin:
            continue
        x = agg[(r.facility_id, r.clin)]
        act = (r.act or "").strip()
        x["n"] += r.n; x["mden"] += r.mden; x["mamt"] += float(r.mamt or 0); x["name"] = x["name"] or r.clin_name
        x["pay"][r.rec] += float(r.mamt or 0)
        if act in CONSULT:
            x["cn"] += r.n
            # consultation-rule denials only (price/visit rules, contract/referral, medical necessity) — not
            # approval, co-pay, eligibility or coding denials
        if is_lab(act):
            x["lab"] += r.n; x["lmn"] += r.mden; x["lmamt"] += float(r.mamt or 0)
            if r.mden:
                x["labcodes"][(act, r.act_desc or "")][0] += r.mden
                x["labcodes"][(act, r.act_desc or "")][1] += float(r.mamt or 0)
        if is_unspec(r.dx):
            x["uamt"] += float(r.mamt or 0) + float(r.codeamt or 0)
            x["udx"][r.dx] += r.n

    for r in cons_rows:
        if not r.clin:
            continue
        x = agg[(r.facility_id, r.clin)]
        x["cden"] += r.den; x["camt"] += float(r.amt or 0); x["ccodes"][r.code] += r.den
        x["cpay"][r.rec] += float(r.amt or 0); x["cprior"] += r.prior30

    def spec_of(clin):
        k = clin.upper().strip()
        for cand in (k, "MOH" + k, "SHA" + k, k[3:] if k.startswith("SHA") else None):
            if cand and cand in spec:
                return spec[cand]
        return "Unknown specialty"
    groups = _dd3(list)
    for (fid, clin) in agg:
        groups[(fid, spec_of(clin))].append(clin)

    found = {"clinician_rate": [], "clinician_consult": [], "clinician_tests": [], "clinician_unspec": []}
    for (fid, sp), clins in groups.items():
        if len(clins) < 3 or sp == "Unknown specialty":
            continue
        for clin in clins:
            x = agg[(fid, clin)]
            peers = [agg[(fid, p)] for p in clins if p != clin]
            # a) MNEC denial rate
            pn, pd = sum(p["n"] for p in peers), sum(p["mden"] for p in peers)
            if x["n"] >= 200 and pn >= 200 and x["mamt"] >= 1000:
                rate, prate = x["mden"] / x["n"], pd / pn
                if rate >= prate + 0.10 and rate >= 1.5 * prate:
                    found["clinician_rate"].append((x["mamt"], fid, clin, sp, {
                        "lines": x["n"], "denied": x["mden"], "rate_pct": round(100 * rate),
                        "peers_rate_pct": round(100 * prate), "peers": len(peers), "amount": round(x["mamt"], 2)}))
            # b) consultation rules (consultation-code denials, any code)
            pcn, pcd = sum(p["cn"] for p in peers), sum(p["cden"] for p in peers)
            if x["cn"] >= 30 and x["cden"] >= 10 and pcn >= 30 and x["camt"] >= 300:
                rate, prate = x["cden"] / x["cn"], pcd / pcn
                if rate >= prate + 0.15 and rate >= 2 * prate:
                    codes = sorted(x["ccodes"].items(), key=lambda kv: -kv[1])[:3]
                    found["clinician_consult"].append((x["camt"], fid, clin, sp, {
                        "lines": x["cn"], "denied": x["cden"], "rate_pct": round(100 * rate), "peers_rate_pct": round(100 * prate),
                        "peers": len(peers), "amount": round(x["camt"], 2),
                        "codes": [{"code": k, "lines": v, "description": _dd_desc(k)} for k, v in codes]}))
            # c) lab tests per visit
            v = visits.get((fid, clin), 0)
            pv = sum(visits.get((fid, p), 0) for p in clins if p != clin)
            plab = sum(p["lab"] for p in peers)
            if v >= 50 and pv >= 50 and x["lab"] >= 50:
                tpv, ptpv = x["lab"] / v, plab / pv
                mn_rate = x["lmn"] / x["lab"]
                if tpv >= 1.5 * ptpv and tpv >= ptpv + 0.5 and mn_rate >= 0.15 and x["lmamt"] >= 500:
                    top_cpt = sorted(x["labcodes"].items(), key=lambda kv: -kv[1][1])[:3]
                    found["clinician_tests"].append((x["lmamt"], fid, clin, sp, {
                        "visits": v, "tests": x["lab"], "tests_per_visit": round(tpv, 1), "peers_tests_per_visit": round(ptpv, 1),
                        "peers": len(peers), "mnec_denied": x["lmn"], "mnec_rate_pct": round(100 * mn_rate),
                        "amount": round(x["lmamt"], 2),
                        "top_cpt": [{"code": k[0], "description": k[1], "denied": d_[0], "amount": round(d_[1], 2)}
                                    for k, d_ in top_cpt]}))
            # d) unspecified principal diagnosis
            u, pu = unspec.get((fid, clin), 0), sum(unspec.get((fid, p), 0) for p in clins if p != clin)
            if v >= 100 and pv >= 100 and x["uamt"] >= 500:
                us, pus = u / v, pu / pv
                if us >= pus + 0.20 and us >= 1.5 * pus:
                    topdx = sorted(x["udx"].items(), key=lambda kv: -kv[1])[:3]
                    found["clinician_unspec"].append((x["uamt"], fid, clin, sp, {
                        "visits": v, "unspecified": u, "share_pct": round(100 * us), "peers_share_pct": round(100 * pus),
                        "peers": len(peers), "amount": round(x["uamt"], 2), "top_dx": [k for k, _ in topdx]}))
    for kind, lst in found.items():
        per = _dd3(int)
        for amt, fid, clin, sp, d in sorted(lst, key=lambda t: -t[0]):
            if per[fid] >= 3:
                continue
            per[fid] += 1
            nm = cname(clin, agg[(fid, clin)]["name"])
            sp = sp.split(" - ")[-1].strip() or sp
            who = f"{nm.title() + ' ' if nm else ''}({clin}, {sp})"
            if kind == "clinician_rate":
                title = f"{who}: {d['rate_pct']}% of lines denied as not medically necessary vs {d['peers_rate_pct']}% for other {sp} clinicians"
                detail = (f"{d['denied']:,} of {d['lines']:,} lines denied with MNEC codes, AED {d['amount']:,.0f}, compared with "
                          f"{d['peers']} other clinicians of the same specialty at this facility (new denials, 90 days).")
            elif kind == "clinician_consult":
                cs = ", ".join(f"{c['code']} ({c['lines']})" for c in d["codes"])
                title = f"{who}: consultation lines denied {d['rate_pct']}% vs {d['peers_rate_pct']}% for other {sp} clinicians"
                detail = (f"{d['denied']:,} of {d['lines']:,} consultation lines denied (AED {d['amount']:,.0f}); "
                          f"main codes {cs}. Compared with {d['peers']} same-specialty clinicians (new denials, 90 days).")
            elif kind == "clinician_tests":
                tc = d["top_cpt"][0] if d["top_cpt"] else None
                title = (f"{who}: {d['tests_per_visit']} lab tests per visit vs {d['peers_tests_per_visit']} for other {sp} clinicians, "
                         f"{d['mnec_rate_pct']}% denied as not medically necessary")
                detail = (f"{d['tests']:,} lab tests over {d['visits']:,} visits; {d['mnec_denied']:,} denied with MNEC codes "
                          f"(AED {d['amount']:,.0f})."
                          + (f" Most denied test: {tc['code']} {tc['description']} — {tc['denied']} lines, AED {tc['amount']:,.0f}."
                             if tc else "")
                          + f" Compared with {d['peers']} same-specialty clinicians (new denials, 90 days).")
            else:
                title = (f"{who}: {d['share_pct']}% of visits coded with an unspecified principal diagnosis vs "
                         f"{d['peers_share_pct']}% for other {sp} clinicians")
                detail = (f"{d['unspecified']:,} of {d['visits']:,} visits (R-codes or '.9' unspecified codes, e.g. "
                          f"{', '.join(d['top_dx'])}); AED {d['amount']:,.0f} denied with MNEC / CODE codes on those visits "
                          f"(new denials, 90 days).")
            add(fid, kind, "medium", title, detail, "/denial-trends", round(amt), None)
            alerts[-1].update(clinician=clin, clinician_name=nm, specialty=sp, stats=d,
                              facts=facts_for(fid, clin, agg[(fid, clin)]["pay"]),
                              insight=insight_for(kind, fid, clin, d, agg[(fid, clin)]["pay"], agg[(fid, clin)]))

    # ---- 1g. Final denial rate above 10% (the Dashboard's measure) ----------------------------------
    # per claim (latest submission): denied = net not paid after ALL RAs, resubmission recoveries included,
    # / net of remitted claims, by encounter month. Judged per facility on its latest SETTLED encounter month:
    # >= 95% of claims remitted and <= 20% of the unpaid amount still in progress (a resubmission awaiting its
    # RA, or no resubmission yet but still inside the 21-day window, SN 3.8.1). ~11-17% stays "awaiting a
    # resubmission RA" even 6+ months on (payer never answers), so 0% is never reached.
    d0g, d1g = _month_start(today, 11), _month_start(today, 1)
    fr = db.execute(text("""
        WITH c AS (
          SELECT DISTINCT ON (facility_id, claim_id) facility_id, claim_id, encounter_start, coalesce(net_amount, 0) net,
                 upper(coalesce(nullif(receiver_id, ''), payer_id)) rec
          FROM claims WHERE encounter_start >= :d0 AND encounter_start < :d1 AND facility_id = ANY(:ids)
          ORDER BY facility_id, claim_id, submission_date DESC NULLS LAST, id DESC),
        ra AS (
          SELECT DISTINCT ON (r.facility_id, r.claim_id, r.ra_file_id, r.net_amount, r.paid_amount, r.denial_amount)
                 r.facility_id, r.claim_id, coalesce(r.paid_amount, 0) paid, r.remittance_date::date d
          FROM remittances r JOIN c USING (facility_id, claim_id)),
        p AS (SELECT facility_id, claim_id, sum(paid) paid, min(d) first_ra, max(d) last_ra FROM ra GROUP BY 1, 2),
        rs AS (SELECT x.facility_id, x.claim_id, max(x.submission_date::date) last_resub
               FROM claims x JOIN c USING (facility_id, claim_id) WHERE x.resubmission_key <> '' GROUP BY 1, 2),
        x AS (SELECT c.*, p.first_ra, p.last_ra, rs.last_resub,
                     c.net - least(greatest(coalesce(p.paid, 0), 0), c.net) unpaid
              FROM c LEFT JOIN p USING (facility_id, claim_id) LEFT JOIN rs USING (facility_id, claim_id))
        SELECT facility_id, to_char(encounter_start, 'YYYY-MM') m, rec, count(*) n, count(first_ra) rn,
               coalesce(sum(net) FILTER (WHERE first_ra IS NOT NULL), 0) rnet,
               coalesce(sum(unpaid) FILTER (WHERE first_ra IS NOT NULL), 0) den,
               count(*) FILTER (WHERE first_ra IS NOT NULL AND unpaid > 0.5) dn,
               coalesce(sum(unpaid) FILTER (WHERE first_ra IS NOT NULL AND (
                   (last_resub IS NOT NULL AND (last_ra IS NULL OR last_ra < last_resub))
                   OR (last_resub IS NULL AND current_date - first_ra <= 21))), 0) pend
        FROM x GROUP BY 1, 2, 3
    """), {"d0": d0g, "d1": d1g, "ids": ids}).all()
    from collections import defaultdict as _dd4
    fm = _dd4(lambda: {"n": 0, "rn": 0, "rnet": 0.0, "den": 0.0, "dn": 0, "pend": 0.0, "pay": _dd4(float)})
    for r in fr:
        x = fm[(r.facility_id, r.m)]
        x["n"] += r.n; x["rn"] += r.rn; x["rnet"] += float(r.rnet); x["den"] += float(r.den); x["dn"] += r.dn
        x["pend"] += float(r.pend); x["pay"][r.rec] += float(r.den)
    rate_of = lambda x: 100 * x["den"] / x["rnet"] if x and x["rnet"] else None
    settled = lambda x: x and x["n"] and x["rn"] / x["n"] >= 0.95 and (x["den"] <= 0 or x["pend"] / x["den"] <= 0.20)
    all_m = []
    mm = d0g
    while mm < d1g:
        all_m.append(mm.strftime("%Y-%m")); mm = date(mm.year + (mm.month == 12), mm.month % 12 + 1, 1)
    pick, early = {}, {}
    for fid in ids:
        sm = [m for m in all_m if settled(fm.get((fid, m)))]
        if sm:
            pick[fid] = sm
        # early warning: most recent month with at least half the claims remitted, after the settled one
        for m in reversed(all_m):
            x = fm.get((fid, m))
            if x and x["n"] and x["rn"] / x["n"] >= 0.5:
                if not sm or m > sm[-1]:
                    early[fid] = m
                break
    # main unpaid denial codes per facility and encounter month (latest RA per activity)
    dc = _dd4(lambda: _dd4(float))
    for r in db.execute(text("""
        WITH c AS (SELECT DISTINCT facility_id, claim_id, to_char(encounter_start, 'YYYY-MM') m FROM claims
                   WHERE encounter_start >= :d0 AND encounter_start < :d1 AND facility_id = ANY(:ids)),
        l AS (SELECT DISTINCT ON (r.facility_id, r.claim_id, ad.activity_id) r.facility_id, c.m, ad.denial_code,
                     greatest(coalesce(ad.net, 0) - coalesce(ad.paid_amount, 0), 0) amt
              FROM remittances r JOIN c USING (facility_id, claim_id)
              JOIN remittance_activity_details ad ON ad.remittance_id_fk = r.id
              ORDER BY r.facility_id, r.claim_id, ad.activity_id, r.remittance_date DESC, r.id DESC)
        SELECT facility_id, m, denial_code, sum(amt) amt FROM l WHERE coalesce(denial_code, '') <> '' AND amt > 0.01
        GROUP BY 1, 2, 3
    """), {"d0": d0g, "d1": d1g, "ids": ids}):
        dc[(r.facility_id, r.m)][r.denial_code] += float(r.amt)
    mlab = lambda m: date(int(m[:4]), int(m[5:]), 1).strftime("%b %Y")
    for fid, sm in pick.items():          # settled final rate for the baselines table, every facility
        r_ = rate_of(fm[(fid, sm[-1])])
        if r_ is not None:
            base[fid].update(final_denial_pct=round(r_, 1), final_denial_month=sm[-1])
    for fid, sm in pick.items():
        ks = sm[-1]; x = fm[(fid, ks)]; rate = rate_of(x)
        if rate is None or x["rnet"] < 20000 or rate < 10:
            continue
        trend = [(m, rate_of(fm.get((fid, m)))) for m in sm[-3:]]
        eo = early.get(fid); xo = fm.get((fid, eo)) if eo else None; open_rate = rate_of(xo)
        open_pend = round(100 * xo["pend"] / xo["den"]) if xo and xo["den"] else None
        cd = dc[(fid, ks)]; codes = sorted(cd.items(), key=lambda kv: -kv[1])[:2]; ctot = sum(cd.values()) or 1
        tp = max(x["pay"], key=x["pay"].get) if x["pay"] else None
        tp_share = x["pay"][tp] / x["den"] if tp and x["den"] else 0
        fam = (codes[0][0] or "")[:4].upper() if codes else ""
        act = ("Clinicians / Documentation review" if fam in ("MNEC", "CODE") else
               "Clinic admin (approvals, eligibility, co-pay)" if fam in ("AUTH", "ELIG", "COPY", "BENX") else
               "Clinic admin / billing" if fam in ("CLAI", "PRCE", "DUPL", "TIME") else "Review the denial codes")
        if tp_share >= 0.6:
            act += f"; mostly one payer ({_pn3(tp) or tp})"
        ml = mlab(ks)
        tr = " · ".join(f"{mlab(m)[:3]} {v:.1f}%" for m, v in trend if v is not None)
        add(fid, "denial_rate_10", "high" if rate > 15 else "medium",
            f"Denial rate {rate:.1f}% for {ml} encounters (settled) — above the 10% limit",
            f"AED {x['den']:,.0f} unpaid of AED {x['rnet']:,.0f} remitted, after resubmissions ({x['dn']:,} claims; "
            f"{round(100 * x['rn'] / x['n'])}% of claims remitted, {round(100 * x['pend'] / x['den']) if x['den'] else 0}% "
            f"of the unpaid amount still in progress). Settled months: {tr}."
            + (f" Early warning: {mlab(eo)[:3]} so far {open_rate:.1f}% ({open_pend}% still in progress)."
               if open_rate is not None else ""),
            "/denial-trends", round(rate, 1), 10)
        alerts[-1].update(
            code=codes[0][0] if codes else None, month=ks, rate_pct=round(rate, 1),
            trend=[{"month": m, "pct": round(v, 1) if v is not None else None} for m, v in trend],
            open_month=eo, open_pct=round(open_rate, 1) if open_rate is not None else None, open_pending_pct=open_pend,
            top_codes=[{"code": k, "share_pct": round(100 * v / ctot)} for k, v in codes],
            top_payer=tp, top_payer_share_pct=round(100 * tp_share),
            insight={"cause": "habit" if fam in ("MNEC", "CODE") else "payer" if tp_share >= 0.6 else "unclear",
                     "label": " · ".join(f"{k} {round(100 * v / ctot)}%" for k, v in codes) or "Denials",
                     "evidence": [{"v": f"{rate:.1f}%", "l": f"{ml} encounters, settled"}, {"v": tr, "l": "settled months"}]
                     + ([{"v": f"{open_rate:.1f}%", "l": f"{mlab(eo)[:3]} so far ({open_pend}% in progress)"}] if open_rate is not None else []),
                     "act": act})

    # ---- 2. Payment rate + overdue (SN 3.9.1: RA within 45 days; 3.9.4: emergency within 7) --
    # Emergency = eClaimLink encounter type with Emergency Room (codes 2/4/6 or text "... Emergency Room").
    # Only ~2 claims in the DB carry one today, so the monthly rate is effectively the 45-day rule.
    rows = db.execute(text("""
        WITH c AS (
          SELECT facility_id, claim_id, min(submission_date) sub, max(net_amount) net,
                 bool_or(coalesce((encounter_type IN ('2', '4', '6') OR (encounter_type ILIKE '%emergency%' AND encounter_type NOT ILIKE '%no emergency%')), false)) em
          FROM claims
          WHERE submission_date BETWEEN :s AND :e AND resubmission_type IS NULL
            AND facility_id = ANY(:ids)
          GROUP BY 1, 2),
        r AS (
          SELECT DISTINCT ON (facility_id, claim_id) facility_id, claim_id, remittance_date, net_amount, paid_amount
          FROM remittances WHERE remittance_date >= :s AND facility_id = ANY(:ids)
          ORDER BY facility_id, claim_id, remittance_date, id)
        SELECT c.facility_id, to_char(c.sub, 'YYYY-MM') m, count(*) n,
               count(*) FILTER (WHERE r.remittance_date - c.sub <= CASE WHEN c.em THEN 7 ELSE 45 END) ra45,
               count(*) FILTER (WHERE c.em AND (r.claim_id IS NULL OR r.remittance_date - c.sub > 7)) em_breach,
               count(*) FILTER (WHERE r.claim_id IS NULL) no_ra,
               coalesce(sum(c.net) FILTER (WHERE r.claim_id IS NULL), 0) no_ra_amt,
               sum(r.paid_amount) paid, sum(r.net_amount) FILTER (WHERE r.claim_id IS NOT NULL) rnet
        FROM c LEFT JOIN r USING (facility_id, claim_id)
        GROUP BY 1, 2
    """), {"s": _month_start(today, 7),
      # only complete submission months whose every claim is past the 45-day SN window
      "e": _month_start(today - timedelta(days=46)) - timedelta(days=1) if (today - timedelta(days=46)).day < 28 else today - timedelta(days=46),
      "ids": ids}).all()
    pm = {}
    for r in rows:
        pm.setdefault(r.facility_id, []).append(r)
    for fid, ms in pm.items():
        ms = sorted([m for m in ms if m.n >= 30], key=lambda m: m.m)
        if len(ms) < 3:
            continue
        *hist, cur = ms
        hist = hist[-4:]
        ra = [m.ra45 / m.n * 100 for m in hist]
        paid = [float(m.paid or 0) / float(m.rnet) * 100 for m in hist if m.rnet]
        cur_ra = cur.ra45 / cur.n * 100
        cur_paid = float(cur.paid or 0) / float(cur.rnet) * 100 if cur.rnet else None
        ra_thr = statistics.mean(ra) - max(8.0, 2 * _sd(ra))
        base[fid].update(ra45_baseline=round(statistics.mean(ra), 1), ra45_threshold=round(ra_thr, 1),
                         ra45_latest=round(cur_ra, 1), ra45_latest_month=cur.m,
                         emergency_breached_7d=int(sum(m.em_breach for m in ms[-3:])))
        if cur_ra < ra_thr:
            add(fid, "payment_rate", "medium",
                f"Only {cur_ra:.0f}% of {cur.m} claims remitted within 45 days",
                f"SN 03/2019 3.9.1 requires RA + payment within 45 days (3.9.4: 7 days for emergency claims). Usual {statistics.mean(ra):.0f}%; "
                f"alert below {ra_thr:.0f}%. Payer may owe a 0.03%/day delay fee.",
                "/turnaround", round(cur_ra, 1), round(ra_thr, 1))
        if paid and cur_paid is not None:
            p_thr = statistics.mean(paid) - max(5.0, 2 * _sd(paid))
            base[fid].update(paid_pct_baseline=round(statistics.mean(paid), 1),
                             paid_pct_threshold=round(p_thr, 1), paid_pct_latest=round(cur_paid, 1))
            if cur_paid < p_thr:
                add(fid, "payment_rate", "high" if cur_paid < p_thr - 5 else "medium",
                    f"Paid {cur_paid:.0f}% of claimed for {cur.m} claims",
                    f"Usual {statistics.mean(paid):.0f}% at first RA; alert below {p_thr:.0f}%.",
                    "/denial-trends", round(cur_paid, 1), round(p_thr, 1))
        # Overdue: claims with no RA at all, >45 days after submission, as % of the month.
        od = [m.no_ra / m.n * 100 for m in hist]
        od_thr = max(10.0, statistics.mean(od) + 2 * _sd(od))
        tot_n = sum(m.no_ra for m in ms[-3:])
        tot_amt = float(sum(m.no_ra_amt for m in ms[-3:]))
        share = tot_n / max(1, sum(m.n for m in ms[-3:])) * 100
        base[fid].update(overdue_pct_threshold=round(od_thr, 1), overdue_pct_latest=round(share, 1),
                         overdue_claims=tot_n, overdue_amount=round(tot_amt, 2))
        if share > od_thr and tot_amt >= 10000:
            add(fid, "overdue", "high" if share > 2 * od_thr else "medium",
                f"{tot_n:,} claims ({share:.0f}%) with no RA after 45 days",
                f"AED {tot_amt:,.0f} outstanding beyond SN 03/2019 3.9.1 (last 3 submission months). "
                f"Alert above {od_thr:.0f}%.",
                "/turnaround", round(share, 1), round(od_thr, 1))

    # ---- 4. Submission cadence + SN 3.7.1 deadline ------------------------------------
    # First submissions only (resubmissions excluded), counted per day. Pattern and gaps use "batch days":
    # days carrying >= 5% of that month's first submissions, so a few late claims after a monthly batch
    # don't make a monthly facility look like it submits every few days (e.g. Alnoor Qusais: ~9,500 claims
    # on the 15th, then a handful on the 16th / 19th).
    rows = db.execute(text("""
        SELECT facility_id, submission_date::date d, count(*) n FROM claims
        WHERE submission_date >= :s AND submission_date < :t AND resubmission_type IS NULL AND facility_id = ANY(:ids)
        GROUP BY 1, 2 ORDER BY 1, 2
    """), {"s": today - timedelta(days=150), "t": today + timedelta(days=1), "ids": ids}).all()
    days = {}
    for r in rows:
        days.setdefault(r.facility_id, []).append((r.d, int(r.n)))
    for fid, dn in days.items():
        month_tot = {}
        for d, n in dn:
            month_tot[(d.year, d.month)] = month_tot.get((d.year, d.month), 0) + n
        ds = [d for d, n in dn if n >= 0.05 * month_tot[(d.year, d.month)]]
        gaps = [(b - a).days for a, b in zip(ds, ds[1:]) if a >= today - timedelta(days=120)]
        if len(gaps) < 2 or not ds:
            continue
        med = statistics.median(gaps)
        since = (today - ds[-1]).days
        if med >= 20:   # monthly batch: expect the next batch around the usual day of the month
            dom = round(statistics.median(d.day for d in ds if d >= today - timedelta(days=120)))
            pattern = f"monthly batch, usually around the {dom}{'th' if 10 <= dom % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(dom % 10, 'th')}"
            last_dom = min(dom, 28)
            nxt = ds[-1] + timedelta(days=15)          # next usual batch day after the last batch
            expected = nxt.replace(day=last_dom)
            if expected < nxt:
                y, m = (expected.year + (expected.month == 12), expected.month % 12 + 1)
                expected = date(y, m, last_dom)
            overdue = today > expected + timedelta(days=3)
            allowed = (expected + timedelta(days=3) - ds[-1]).days
            base[fid].update(next_batch=expected.isoformat(), alert_after=(expected + timedelta(days=3)).isoformat())
            reason = (f"Usual pattern: {pattern}. The batch due around {expected:%d %b} has not arrived "
                      f"(last batch {ds[-1]:%d %b}).")
        else:
            allowed = max(int(max(gaps)) + 3, 7)
            pattern = "daily" if med <= 1.5 else f"every ~{med:.0f} days"
            overdue = since > allowed
            reason = f"Usual pattern: {pattern}; longest normal gap {max(gaps)} days (alert after {allowed})."
        base[fid].update(submission_pattern=pattern, submission_gap_allowed=allowed,
                         last_submission=ds[-1].isoformat(), days_since_submission=since)
        if overdue:
            add(fid, "submission_gap", "high",
                f"No new claim batch for {since} days",
                reason + " Resubmissions are not counted. Check the facility ERP / eClaimLink access.",
                "/jobs", since, allowed)

    svc = _month_start(today, 1)  # service month due by the 15th of this month
    rows = db.execute(text("""
        SELECT facility_id, date_trunc('month', encounter_start)::date sm, count(*) n
        FROM claims
        WHERE encounter_start >= :s AND encounter_start < :e AND resubmission_type IS NULL
          AND submission_date <= :t AND facility_id = ANY(:ids)
          AND submission_date <= (date_trunc('month', encounter_start) + interval '1 month' + make_interval(days => :d))::date
        GROUP BY 1, 2
    """), {"s": _month_start(today, 4), "e": m0, "t": today, "ids": ids,
           "d": max(0, today.day - 1)}).all()
    vol = {}
    for r in rows:
        vol.setdefault(r.facility_id, {})[r.sm] = r.n
    if today.day >= 10:
        for fid in ids:
            v = vol.get(fid, {})
            prev = [v.get(_month_start(today, k), 0) for k in (2, 3, 4)]
            prev = [p for p in prev if p > 0]
            if len(prev) < 2:
                continue
            # prior months measured at the same day-of-month cut-off, so like is compared with like
            expected = statistics.mean(prev)
            got = v.get(svc, 0)
            base[fid].update(sn_month=svc.strftime("%Y-%m"), sn_submitted=got, sn_expected=round(expected))
            if got < 0.7 * expected:
                late = today.day > 15
                add(fid, "sn_deadline", "high" if late else "medium",
                    f"{svc:%b %Y} services: {got:,} of ~{expected:,.0f} usual claims submitted"
                    + (" — past the 15th" if late else ""),
                    "SN 03/2019 3.7.1: submit by the 15th of the next month; late claims carry a "
                    "0.03%/day delay fee on the net amount." if late else
                    f"Deadline {svc.year if svc.month < 12 else svc.year + 1}-"
                    f"{(svc.month % 12) + 1:02d}-15 (SN 03/2019 3.7.1).",
                    "/jobs", got, round(0.7 * expected))

    # ---- 7. Credentials (DHPO / Riayati) ---------------------------------------------
    rows = db.execute(text("""
        SELECT DISTINCT ON (facility_id, src) facility_id, src, status, started_at, err
        FROM (SELECT facility_id, CASE WHEN fetch_type = 'Riayati' THEN 'Riayati' ELSE 'DHPO' END src,
                     status, started_at, error_detail::text err
              FROM fetch_log WHERE started_at > now() - interval '7 days' AND status <> 'running'
                AND facility_id = ANY(:ids)) x
        ORDER BY facility_id, src, started_at DESC
    """), {"ids": ids}).all()
    for r in rows:
        e = (r.err or "").lower()
        if r.status != "success" and any(k in e for k in ("401", "unauthor", "login", "password", "credential",
                                                          "-2", "invalid user")):
            add(r.facility_id, "credentials", "high",
                f"{r.src} login rejected",
                f"Last {r.src} fetch ({r.started_at:%d %b %H:%M}) was refused (401 / login). "
                "Check the account in Facility settings.",
                "/facility-settings")

    # every link opens the target page on the alert's own facility ("" = all facilities)
    for a in alerts:
        sep = "&" if "?" in a["link"] else "?"
        a["link"] = f'{a["link"]}{sep}facility_id={a["facility_id"] or ""}'
        if a["type"] == "denial_code_spike":   # the spike is this week: open on the current month
            a["link"] += f"&month={today:%Y-%m}"
    order = {"high": 0, "medium": 1, "info": 3}
    alerts.sort(key=lambda a: (order.get(a["severity"], 2), a["type"] != "denial_code_spike", a["facility_name"]))
    return {"items": alerts, "baselines": sorted(base.values(), key=lambda b: b["facility_name"])}


def _get(db: Session, refresh: bool = False):
    """One computation at a time; concurrent callers wait for it instead of repeating it."""
    today = date.today()
    hit = _CACHE.get(today) or next(iter(_CACHE.values()), None)   # after midnight, yesterday's until rebuilt
    if not refresh and hit and today in _CACHE and time.time() - hit[0] <= _TTL:
        return hit
    if not refresh and hit:
        # stale: show the last result now and recompute in the background (nobody waits ~1.5 min)
        if not _LOCK.locked():
            warm()
        return hit
    with _LOCK:
        hit = _CACHE.get(today)
        if refresh or not hit or time.time() - hit[0] > _TTL:
            hit = (time.time(), _compute(db, today))
            _CACHE.clear()
            _CACHE[today] = hit
    return hit


def warm():
    """Compute alerts in the background at startup so the first page view is instant."""
    from ..database import SessionLocal

    def run():
        if _LOCK.locked():
            return
        db = SessionLocal()
        try:
            _get(db, refresh=True)
        except Exception:
            pass
        finally:
            db.close()
    threading.Thread(target=run, daemon=True).start()


@router.get("")
def list_alerts(
    refresh: bool = Query(False),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    hit = _get(db, refresh)
    data = hit[1]
    items, baselines = data["items"], data["baselines"]
    allowed = allowed_facility_ids(db, user)
    if allowed is not None:
        items = [a for a in items if a["facility_id"] in allowed
                 or (a["facility_id"] is None and allowed & set(a.get("facilities", [])))]
        baselines = [b for b in baselines if b["facility_id"] in allowed]
    return {"generated_at": datetime.fromtimestamp(hit[0]).astimezone(), "count": len(items),
            "high": sum(1 for a in items if a["severity"] == "high"),
            "items": items, "baselines": baselines,
            "source": "Defaults learned per facility; timelines per DHA SN 03/2019"}


@router.get("/lines.xlsx")
def alert_lines_xlsx(
    code: str, payer: Optional[str] = None, facility_id: Optional[int] = None, days: int = Query(31, ge=1, le=120),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Denied lines behind an alert, with stage (new / re-denied / reconciliation filed) and the deadline."""
    import io
    from fastapi.responses import StreamingResponse
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    facility_id = resolve_facility(db, user, facility_id)
    today = date.today()
    if days < 30:
        days = 30
    rows = db.execute(text("""
        WITH r AS (
          SELECT r.id, r.facility_id, r.claim_id, r.remittance_date::date ra, r.tpa_comments,
                 EXISTS (SELECT 1 FROM claims x WHERE x.facility_id = r.facility_id AND x.claim_id = r.claim_id
                           AND x.resubmission_key IN ('internal complaint', 'correction', 'reconciliation')
                           AND x.submission_date::date < r.remittance_date::date) redenied,
                 EXISTS (SELECT 1 FROM claims x WHERE x.facility_id = r.facility_id AND x.claim_id = r.claim_id
                           AND x.resubmission_key = 'reconciliation') recon
          FROM remittances r
          WHERE r.remittance_date >= :s AND (CAST(:fid AS int) IS NULL OR r.facility_id = :fid)),
        c AS (
          SELECT DISTINCT ON (c.facility_id, c.claim_id) c.facility_id, c.claim_id, c.member_id, c.patient_id,
                 c.submission_date::date first_sub,
                 c.encounter_start::date enc, coalesce(nullif(c.receiver_id, ''), c.payer_id) rec,
                 coalesce(nullif(trim(c.clinician_id), ''), c.clinician_name) cn
          FROM claims c JOIN (SELECT DISTINCT facility_id, claim_id FROM r) k
            ON k.facility_id = c.facility_id AND k.claim_id = c.claim_id
          ORDER BY c.facility_id, c.claim_id, c.submission_date)
        SELECT f.facility_name, r.claim_id, c.patient_id, c.member_id, c.enc, c.cn, d.activity_code, c.first_sub,
               greatest(coalesce(d.net, 0) - coalesce(d.paid_amount, 0), 0) denied, r.ra, r.redenied, r.recon,
               r.tpa_comments
        FROM remittance_activity_details d
        JOIN r ON r.id = d.remittance_id_fk
        JOIN c ON c.facility_id = r.facility_id AND c.claim_id = r.claim_id
        JOIN facilities f ON f.id = r.facility_id
        WHERE upper(d.denial_code) = upper(:code) AND (CAST(:payer AS text) IS NULL OR upper(c.rec) = upper(:payer))
        ORDER BY r.ra, r.claim_id
    """), {"s": today - timedelta(days=days), "fid": facility_id, "code": code, "payer": payer}).all()
    wb = Workbook(); ws = wb.active; ws.title = "Denied lines"
    head = ["Facility", "Claim ID", "MRN", "Member ID", "Encounter", "Clinician", "Service", "Denied AED",
            "RA date", "Stage", "Next action", "Deadline", "Days left", "TPA comment"]
    ws.append(head)
    for c_ in ws[1]:
        c_.font = Font(bold=True, color="FFFFFF"); c_.fill = PatternFill("solid", fgColor="1E6B75")
    for x in rows:
        if x.recon:
            stage, act, due, left = "Reconciliation filed", "Await reconciliation RA", "", ""
        elif x.redenied:
            valid = (x.first_sub.replace(year=x.first_sub.year + 2) if x.first_sub else None)
            stage, act = "Re-denied after resubmission", "Notify facility – reconcile with insurer"
            due = f"No 21-day limit (SN 3.10.4); claim valid to {valid:%d/%m/%Y}" if valid else "No 21-day limit (SN 3.10.4)"
            left = ""
        else:
            d = x.ra + timedelta(days=21); left = (d - today).days
            stage, act, due = "New denial (first RA)", ("Resubmit" if left >= 0 else "Window closed"), d
        ws.append([x.facility_name, x.claim_id, x.patient_id, x.member_id, x.enc, x.cn, x.activity_code,
                   float(x.denied), x.ra, stage, act, due, left, (x.tpa_comments or "")[:300]])
    for col, w in zip("ABCDEFGHIJKLMN", (24, 34, 12, 22, 11, 16, 14, 11, 11, 26, 34, 34, 9, 60)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "C2"; ws.auto_filter.ref = ws.dimensions
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    name = f"alert_{code}_{payer or 'all'}_{today:%Y%m%d}.xlsx"
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---- clinician panel: the lines behind one clinician alert -----------------------------------------
_KIND_FILTER = {
    "clinician_pattern": "ad.denial_code ILIKE 'MNEC%'",
    "clinician_rate": "ad.denial_code ILIKE 'MNEC%'",
    "clinician_tests": "ad.denial_code ILIKE 'MNEC%' AND ad.activity_code ~ '^8[0-9]{4}$'",
    "clinician_consult": "ad.denial_code ~* '^(PRCE|CLAI|MNEC)' AND ad.activity_code = ANY(:cc)",
    "clinician_unspec": "(ad.denial_code ILIKE 'MNEC%' OR ad.denial_code ILIKE 'CODE%') "
                        "AND (dx.diagnosis_code ~ '^R' OR dx.diagnosis_code ~ '\\.9$' OR dx.diagnosis_code ~ '^[A-Z][0-9]{2}\\.?9$')",
}
_CONS = ["9", "10", "11", "12", "13", "14", "9.01", "9.02", "10.01", "10.02", "11.01", "11.02", "22", "23", "24", "25"]


def _clinician_lines(db, facility_id: int, clinician: str, kind: str):
    if kind not in _KIND_FILTER:
        raise HTTPException(400, "Unknown alert type")
    # gap: tests -> days since the same test for the member; consultations -> days since the member's last visit
    gap = ("""(SELECT cl.encounter_start::date - max(p.encounter_start::date) FROM claims p JOIN claim_activities pa
                 ON pa.claim_id_fk = p.id WHERE p.facility_id = cl.facility_id AND p.member_id = cl.member_id
                 AND p.resubmission_key = '' AND pa.activity_code = ad.activity_code
                 AND p.encounter_start::date < cl.encounter_start::date AND p.encounter_start::date >= cl.encounter_start::date - 365)"""
           if kind == "clinician_tests" else
           """(SELECT cl.encounter_start::date - max(p.encounter_start::date) FROM claims p WHERE p.facility_id = cl.facility_id
                 AND p.member_id = cl.member_id AND p.resubmission_key = ''
                 AND p.encounter_start::date < cl.encounter_start::date AND p.encounter_start::date >= cl.encounter_start::date - 365)"""
           if kind == "clinician_consult" else "NULL::int")
    rows = db.execute(text(f"""
        WITH r AS (
          SELECT r.id, r.facility_id, r.claim_id, r.remittance_date, r.tpa_comments FROM remittances r
          WHERE r.remittance_date >= current_date - 90 AND r.facility_id = :f
            AND NOT EXISTS (SELECT 1 FROM claims x WHERE x.facility_id = r.facility_id AND x.claim_id = r.claim_id
                            AND x.resubmission_key <> '' AND x.submission_date::date < r.remittance_date::date))
        SELECT cl.claim_id, cl.encounter_start::date enc, r.remittance_date::date ra, ad.activity_code act,
               a.activity_description act_desc, dx.diagnosis_code dx,
               upper(coalesce(nullif(cl.receiver_id, ''), cl.payer_id)) payer, ad.denial_code code,
               greatest(coalesce(ad.net, 0) - coalesce(ad.paid_amount, 0), 0) amt, r.tpa_comments remark, {gap} gap,
               coalesce(ad.denial_description, '') reason
        FROM r JOIN remittance_activity_details ad ON ad.remittance_id_fk = r.id
        JOIN LATERAL (SELECT * FROM claims x WHERE x.facility_id = r.facility_id AND x.claim_id = r.claim_id
                      AND x.resubmission_key = '' ORDER BY x.id LIMIT 1) cl ON true
        LEFT JOIN claim_activities a ON a.claim_id_fk = cl.id AND a.activity_id = ad.activity_id
        LEFT JOIN LATERAL (SELECT d.diagnosis_code FROM claim_diagnoses d WHERE d.claim_id_fk = cl.id
                           ORDER BY (d.diagnosis_type ILIKE ANY (ARRAY['principal','primary'])) DESC, d.sequence_no NULLS LAST LIMIT 1) dx ON true
        WHERE coalesce(nullif(a.clinician_id, ''), cl.clinician_id) = :c
          AND coalesce(ad.net, 0) - coalesce(ad.paid_amount, 0) > 0.01 AND {_KIND_FILTER[kind]}
        ORDER BY amt DESC, r.remittance_date DESC
    """), {"f": facility_id, "c": clinician, "cc": _CONS}).all()
    if kind == "clinician_consult":   # same visit-rule filter as the alert itself
        from ..services.denial_category import classify
        rows = [x for x in rows if classify(x.reason or x.remark or "", x.act or "", "Consultation visit rules",
                                            x.code or "")[0] == "Consultation visit rules"]
    return rows


def _clin_scope(user, facility_id):
    resolve_facility(None, user, facility_id)


@router.get("/clinician-lines")
def clinician_lines(facility_id: int, clinician: str, kind: str,
                    db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Denied lines behind a clinician alert (new denials, last 90 days), grouped and listed."""
    _clin_scope(user, facility_id)
    rows = _clinician_lines(db, facility_id, clinician, kind)
    from collections import defaultdict
    grp = defaultdict(lambda: {"lines": 0, "amount": 0.0, "desc": None, "codes": defaultdict(int)})
    for x in rows:
        g = grp[(x.act, x.dx if kind != "clinician_tests" else None)]
        g["lines"] += 1; g["amount"] += float(x.amt); g["desc"] = g["desc"] or x.act_desc; g["codes"][x.code] += 1
    groups = sorted(({"activity_code": k[0], "diagnosis_code": k[1], "activity": v["desc"], "lines": v["lines"],
                      "amount": round(v["amount"], 2), "top_code": max(v["codes"], key=v["codes"].get)}
                     for k, v in grp.items()), key=lambda g: -g["amount"])[:12]
    gaps = [x.gap for x in rows if x.gap is not None]
    bands = None
    if kind == "clinician_tests":
        bands = {"≤90 days": sum(g <= 90 for g in gaps), "91–180 days": sum(90 < g <= 180 for g in gaps),
                 "first in 12 months": len(rows) - sum(g <= 180 for g in gaps)}
    elif kind == "clinician_consult":
        bands = {"≤7 days": sum(g <= 7 for g in gaps), "8–28 days": sum(7 < g <= 28 for g in gaps),
                 "over 28 days / first": len(rows) - sum(g <= 28 for g in gaps)}
    return {"total": len(rows), "amount": round(sum(float(x.amt) for x in rows), 2), "groups": groups, "gap_bands": bands,
            "rows": [{"claim_id": x.claim_id, "enc": x.enc, "ra": x.ra, "activity_code": x.act, "activity": x.act_desc,
                      "dx": x.dx, "payer": x.payer, "code": x.code, "amount": float(x.amt), "gap": x.gap,
                      "remark": (x.remark or "")[:200]} for x in rows[:300]]}


@router.get("/clinician-lines.xlsx")
def clinician_lines_xlsx(facility_id: int, clinician: str, kind: str,
                         db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    import io
    from fastapi.responses import StreamingResponse
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    _clin_scope(user, facility_id)
    rows = _clinician_lines(db, facility_id, clinician, kind)
    wb = Workbook(); ws = wb.active; ws.title = "Denied lines"
    gap_h = {"clinician_tests": "Days since same test", "clinician_consult": "Days since last visit"}.get(kind, "")
    hdr = ["Claim ID", "Encounter", "RA date", "Service", "Description", "Principal Dx", "Payer", "Denial code",
           "Denied AED", "TPA remark"] + ([gap_h] if gap_h else [])
    ws.append(hdr)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF"); c.fill = PatternFill("solid", fgColor="006884")
    for x in rows:
        ws.append([x.claim_id, x.enc, x.ra, x.act, x.act_desc, x.dx, x.payer, x.code, float(x.amt), (x.remark or "")[:300]]
                  + ([x.gap] if gap_h else []))
    for col, w in zip("ABCDEFGHIJK", (30, 11, 11, 12, 30, 12, 10, 12, 11, 60, 12)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "B2"; ws.auto_filter.ref = ws.dimensions
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    name = f"clinician_{clinician}_{kind.replace('clinician_', '')}_{date.today():%Y%m%d}.xlsx"
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---- facility denial panel: where the unpaid amount of one encounter month sits ----------------------
def _facility_denial_lines(db, facility_id: int, month: str):
    y, m = map(int, month.split("-"))
    d0, d1 = date(y, m, 1), date(y + (m == 12), m % 12 + 1, 1)
    return db.execute(text("""
        WITH c AS (
          SELECT DISTINCT ON (facility_id, claim_id) id, facility_id, claim_id, encounter_start::date enc, clinician_id,
                 upper(coalesce(nullif(receiver_id, ''), payer_id)) payer
          FROM claims WHERE facility_id = :f AND encounter_start >= :d0 AND encounter_start < :d1
          ORDER BY facility_id, claim_id, submission_date DESC NULLS LAST, id DESC),
        ras AS (SELECT r.claim_id, min(r.remittance_date::date) first_ra, max(r.remittance_date::date) last_ra
                FROM remittances r JOIN c USING (facility_id, claim_id) GROUP BY 1),
        rs AS (SELECT x.claim_id, max(x.submission_date::date) last_resub FROM claims x JOIN c USING (facility_id, claim_id)
               WHERE x.resubmission_key <> '' GROUP BY 1),
        l AS (SELECT DISTINCT ON (r.claim_id, ad.activity_id) r.claim_id, ad.activity_id, ad.activity_code, ad.denial_code,
                     greatest(coalesce(ad.net, 0) - coalesce(ad.paid_amount, 0), 0) amt, r.tpa_comments
              FROM remittances r JOIN c USING (facility_id, claim_id)
              JOIN remittance_activity_details ad ON ad.remittance_id_fk = r.id
              ORDER BY r.claim_id, ad.activity_id, r.remittance_date DESC, r.id DESC)
        SELECT c.claim_id, c.enc, l.activity_code act, coalesce(nullif(a.clinician_id, ''), c.clinician_id) clin,
               (SELECT d.diagnosis_code FROM claim_diagnoses d WHERE d.claim_id_fk = c.id
                ORDER BY (d.diagnosis_type ILIKE ANY (ARRAY['principal','primary'])) DESC, d.sequence_no NULLS LAST LIMIT 1) dx,
               c.payer, l.denial_code code, l.amt, l.tpa_comments remark, ras.first_ra, ras.last_ra, rs.last_resub
        FROM l JOIN c USING (claim_id) JOIN ras USING (claim_id) LEFT JOIN rs USING (claim_id)
        LEFT JOIN claim_activities a ON a.claim_id_fk = c.id AND a.activity_id = l.activity_id
        WHERE l.amt > 0.01 AND coalesce(l.denial_code, '') <> ''
        ORDER BY l.amt DESC
    """), {"f": facility_id, "d0": d0, "d1": d1}).all()


def _stage(x, today):
    if x.last_resub and (x.last_ra is None or x.last_ra < x.last_resub):
        return "Resubmission awaiting RA", None
    if not x.last_resub and x.first_ra and (today - x.first_ra).days <= 21:
        return "Still in window", x.first_ra + timedelta(days=21)
    return "Final", None


def _who(code):
    f = (code or "")[:4].upper()
    return ("Clinician" if f in ("MNEC", "CODE") else "Clinic admin" if f in ("AUTH", "ELIG", "COPY", "BENX")
            else "Billing" if f in ("CLAI", "PRCE", "DUPL", "TIME") else "Review")


@router.get("/facility-denials")
def facility_denials(facility_id: int, month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
                     db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Where one encounter month's unpaid amount sits: by code, payer, clinician, service; lines with stage."""
    _clin_scope(user, facility_id)
    from collections import defaultdict
    from ..services.payer_names import payer_name as _pnx
    rows = _facility_denial_lines(db, facility_id, month)
    today = date.today()
    tot = sum(float(x.amt) for x in rows) or 1

    def top(keyf, n=6):
        g = defaultdict(lambda: [0.0, 0])
        for x in rows:
            k = keyf(x)
            g[k][0] += float(x.amt); g[k][1] += 1
        return sorted(({"key": k, "amount": round(v[0], 2), "lines": v[1], "share_pct": round(100 * v[0] / tot)}
                       for k, v in g.items()), key=lambda r: -r["amount"])[:n]
    codes = top(lambda x: x.code)
    for c in codes:
        c["description"] = _dd_desc(c["key"]); c["who"] = _who(c["key"])
    payers = top(lambda x: x.payer)
    for p in payers:
        p["name"] = _pnx(p["key"]) or ""
    clins = top(lambda x: x.clin)
    try:
        from .reports import _clinician_name
        for c in clins:
            n_ = _clinician_name(c["key"]) if c["key"] else ""
            c["name"] = n_ if n_ and n_ != c["key"] else ""
    except Exception:
        pass
    stages = defaultdict(float)
    out = []
    for x in rows:
        st, due = _stage(x, today)
        stages[st] += float(x.amt)
        if len(out) < 300:
            out.append({"claim_id": x.claim_id, "enc": x.enc, "activity_code": x.act, "dx": x.dx, "payer": x.payer,
                        "code": x.code, "amount": float(x.amt), "stage": st, "due": due, "remark": (x.remark or "")[:200]})
    return {"month": month, "total": len(rows), "codes": codes, "payers": payers, "clinicians": clins,
            "services": top(lambda x: x.act),
            "stages": [{"stage": k, "share_pct": round(100 * v / tot)} for k, v in sorted(stages.items(), key=lambda kv: -kv[1])],
            "rows": out}


@router.get("/facility-denials.xlsx")
def facility_denials_xlsx(facility_id: int, month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
                          db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    import io
    from fastapi.responses import StreamingResponse
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    _clin_scope(user, facility_id)
    rows = _facility_denial_lines(db, facility_id, month)
    today = date.today()
    wb = Workbook(); ws = wb.active; ws.title = f"Unpaid {month}"
    ws.append(["Claim ID", "Encounter", "Service", "Principal Dx", "Clinician", "Payer", "Denial code", "Unpaid AED",
               "Stage", "Resubmit by", "Who acts", "TPA remark"])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF"); c.fill = PatternFill("solid", fgColor="006884")
    for x in rows:
        st, due = _stage(x, today)
        ws.append([x.claim_id, x.enc, x.act, x.dx, x.clin, x.payer, x.code, float(x.amt), st, due, _who(x.code),
                   (x.remark or "")[:300]])
    for col, w in zip("ABCDEFGHIJKL", (30, 11, 12, 12, 16, 10, 12, 11, 24, 12, 13, 60)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "B2"; ws.auto_filter.ref = ws.dimensions
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="facility_{facility_id}_unpaid_{month}.xlsx"'})
