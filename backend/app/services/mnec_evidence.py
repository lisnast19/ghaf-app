"""
mnec_evidence.py — rule-based evidence for MNEC (medical necessity) resubmission comments. No AI, nothing leaves the server.

For each MNEC-denied activity:
  1. GUIDELINES: test/service code + any diagnosis on the claim (principal or secondary) → clinical indication and the
     guideline that supports ordering it. Built from the most frequent MNEC denials in BIX Lite (Jul–Nov 2026).
     No match → the line is marked weak (add a supporting diagnosis from the notes, or accept the denial).
  2. Case sheet (scrub_claims.case_sheet, from Documentation review): positive clinical findings by keyword
     (negated phrases such as "NO FEVER" are skipped).
  3. Results on the claim's own Observation elements (Type=Result): values outside the usual reference range.
The comment is grouped by indication so the same justification is not repeated for every test.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Guideline table — (codes, diagnosis prefixes, indication, guideline)
# Diagnosis prefixes match the start of any ICD-10 code on the claim.
# ---------------------------------------------------------------------------
ADA = "ADA Standards of Care in Diabetes"
HTN = "ACC/AHA Hypertension Guideline (baseline laboratory evaluation)"
LIPID = "ACC/AHA Guideline on the Management of Blood Cholesterol"
ATA = "American Thyroid Association hypothyroidism guideline"
CHEST = "ACC/AHA Guideline for the Evaluation of Chest Pain"
IDSA_GAS = "IDSA guideline for Group A streptococcal pharyngitis"
IDSA_FLU = "IDSA clinical practice guideline for seasonal influenza"
NICE_LRTI = "NICE guideline on pneumonia / lower respiratory tract infection (CRP point-of-care testing)"
GINA = "GINA / GOLD guidance for acute bronchospasm"
ACR_GOUT = "ACR guideline for the management of gout"
EULAR = "EULAR recommendations for early arthritis (inflammatory markers)"
ACG_HP = "ACG guideline on dyspepsia / H. pylori"
IRON = "BSG / AGA guidance on iron-deficiency anaemia"
ACOG = "ACOG routine prenatal care"
AASLD = "AASLD practice guidance on fatty liver disease"
KDIGO = "KDIGO guideline on CKD evaluation"

DM = ("E10", "E11", "E13", "R73", "O24", "Z86.3")
HTN_DX = ("I10", "I11", "I12", "I13", "I15")
LIP_DX = ("E78",)
THY = ("E01", "E02", "E03", "E05", "E06", "E07", "R94.6")
CHESTDX = ("R07", "I20", "I21", "I24", "I25", "R00", "R002")
THROAT = ("J02", "J03", "J35", "J36")
FLU = ("J09", "J10", "J11", "R50", "J06", "J02", "J00")
LRTI = ("J12", "J13", "J14", "J15", "J16", "J18", "J20", "J21", "J22", "J40", "J44")
WHEEZE = ("J45", "J44", "J21", "R06.2", "R062", "J20", "J98.01")
JOINT = ("M10", "M25", "M05", "M06", "M13", "M79.6", "M255")
GOUT = ("M10", "M1A", "E79")
GI = ("K29", "K30", "K21", "R10", "K25", "K26", "K27")
ANAEMIA = ("D50", "D51", "D52", "D53", "D64", "R53", "N92")
PREG = ("O", "Z33", "Z34", "Z36")
CKD = ("N18", "N19", "R80", "R31")
FEVER_DEHYD = ("E86", "R11", "R50", "A09", "K52", "R57")

GUIDELINES: List[Tuple[Tuple[str, ...], Tuple[str, ...], str, str]] = [
    # most specific diagnosis first
    (("84460", "84450", "82977", "84075"), ("K76", "K75", "K70", "K74", "R74"), "monitoring of fatty liver / liver disease", AASLD),
    (("84550",), ("M10", "M1A", "E79"), "serum urate monitoring of hyperuricaemia / gout", ACR_GOUT),
    (("80061", "82465", "83721", "84478", "83718"), LIP_DX, "lipid monitoring of dyslipidaemia / response to lipid-lowering therapy", LIPID),
    (("84460", "84450"), LIP_DX, "liver enzymes for safety monitoring of lipid-lowering (statin) therapy", LIPID),
    (("83036", "82947", "82950", "82951"), DM, "glycaemic monitoring of diabetes / prediabetes", ADA),
    (("80061", "82465", "83721", "84478", "83718"), DM, "annual lipid assessment for cardiovascular risk in diabetes / prediabetes", ADA),
    (("82565", "82043", "82570", "84520", "81001"), DM, "annual kidney assessment (eGFR / urine albumin) in diabetes", ADA),
    (("84460", "84450"), DM, "screening for fatty liver disease in metabolic risk (diabetes / prediabetes)", ADA),
    (("82565", "84520", "82947", "83036", "80061", "82465", "83721", "84478", "81001", "85025", "84443", "93000",
      "84132", "84295", "82310"), HTN_DX, "baseline / annual laboratory evaluation of hypertension", HTN),
    (("84443", "84439", "84436", "84480", "86376"), THY, "thyroid function monitoring of thyroid disease", ATA),
    (("93000", "93005", "93010"), CHESTDX, "ECG in the initial evaluation of chest pain / ischaemic symptoms", CHEST),
    (("80061", "82465", "83721", "82947", "83036"), CHESTDX, "cardiovascular risk assessment in chest pain / ischaemic heart disease", CHEST),
    (("87880", "87430", "87081"), THROAT, "rapid streptococcal antigen test to guide antibiotic use in pharyngitis", IDSA_GAS),
    (("87804", "87502", "87400"), FLU, "influenza testing in influenza-like illness where antiviral treatment depends on the result", IDSA_FLU),
    (("86140", "85025"), LRTI, "CRP / blood count to distinguish bacterial lower respiratory infection and guide antibiotics", NICE_LRTI),
    (("86738", "86735"), ("J18", "J15", "J16", "J20", "J22"), "Mycoplasma serology for suspected atypical pneumonia / bronchitis", NICE_LRTI),
    (("94640", "94664", "J7613", "J7620"), WHEEZE, "nebulised bronchodilator for acute wheeze / bronchospasm", GINA),
    (("84550",), GOUT + ("M25", "M79.6", "M255"), "serum urate to diagnose / monitor gout in joint pain", ACR_GOUT),
    (("85652", "86140", "86431", "86200"), JOINT, "inflammatory markers to exclude inflammatory arthritis in joint pain", EULAR),
    (("86677", "87338", "83013", "83014"), GI, "H. pylori testing in dyspepsia / gastritis", ACG_HP),
    (("85025",), GI + ANAEMIA, "blood count to exclude anaemia / bleeding", IRON),
    (("83540", "82728", "83550", "85025"), ANAEMIA, "iron studies for suspected iron-deficiency anaemia", IRON),
    (("81001", "81003", "85025", "84443", "82947", "86900", "86901", "87086"), PREG, "routine antenatal screening", ACOG),
    (("82565", "84520", "82043", "81001", "80069"), CKD, "kidney function monitoring of CKD / proteinuria", KDIGO),
    (("96360", "96361", "96365", "96374", "96375"), FEVER_DEHYD, "parenteral treatment because of dehydration / vomiting / inability to take oral medication", "clinical practice (oral route not possible)"),
    (("96372",), FEVER_DEHYD + ("R52", "M54.5", "M545", "N23", "G43"), "parenteral analgesia / antiemetic for severe pain or vomiting where oral route is not effective", "clinical practice (oral route not possible)"),
]

# Services that payers usually accept only with a documented reason — always ask for it explicitly.
NEEDS_REASON = {
    "96360": "why IV fluids were needed (dehydration signs, vomiting, inability to drink)",
    "96365": "why IV infusion was needed instead of oral treatment",
    "96374": "why IV push was needed instead of oral treatment (vomiting, severe pain, unable to swallow)",
    "96375": "why an additional IV push was needed",
    "96372": "why the injection route was needed instead of oral (vomiting, severe pain, oral intolerance)",
    "70210": "red flags for imaging (symptoms > 10 days, complications) — plain sinus X-ray is not recommended for uncomplicated acute sinusitis",
}

# ---------------------------------------------------------------------------
# Case sheet findings — keyword → label (positive mentions only)
# ---------------------------------------------------------------------------
FINDINGS = [
    (r"HIGH[\s-]*GRADE\s+FEVER|FEVER[^.]{0,40}HIGH\s*GRADE", "high-grade fever"),
    (r"\bFEVER\b", "fever"),
    (r"\bWHEEZ", "wheeze"),
    (r"\bCREP", "crepitations on auscultation"),
    (r"\bRHONCH", "rhonchi"),
    (r"BREATHLESS|SHORTNESS OF BREATH|\bSOB\b|DYSPN", "breathlessness"),
    (r"\bCOUGH\b", "cough"),
    (r"SORE THROAT|THROAT[^.]{0,15}(CONGEST|INFLAM|RED)", "sore / congested throat"),
    (r"TONSIL[^.]{0,20}(ENLARG|EXUDATE|HYPERTROPH|INFLAM)", "inflamed tonsils"),
    (r"\bEXUDATE", "tonsillar exudate"),
    (r"VOMIT", "vomiting"),
    (r"DIARRH|LOOSE STOOL|LOOSE MOTION", "diarrhoea"),
    (r"DEHYDRAT|DRY TONGUE|DRY MUCOSA|SUNKEN", "signs of dehydration"),
    (r"POOR (ORAL|FOOD) INTAKE|NOT TOLERATING ORAL|REDUCED INTAKE", "poor oral intake"),
    (r"EPIGASTR", "epigastric pain"),
    (r"ABDOMINAL PAIN|PAIN ABDOMEN|ABD\.? PAIN", "abdominal pain"),
    (r"CHEST PAIN|CHEST DISCOMFORT|CHEST TIGHT", "chest pain"),
    (r"PALPITAT", "palpitations"),
    (r"JOINT PAIN|ARTHRALG|KNEE PAIN|ANKLE PAIN|SWOLLEN JOINT|JOINT SWELL", "joint pain / swelling"),
    (r"BACK PAIN|LBA|LOW BACK", "back pain"),
    (r"SEVERE PAIN|PAIN SCORE\s*[7-9]|VAS\s*[7-9]", "severe pain"),
    (r"FATIGUE|TIREDNESS|WEAKNESS|MALAISE", "fatigue / malaise"),
    (r"\bPALLOR\b", "pallor"),
    (r"POLYURIA|POLYDIPSIA|INCREASED THIRST|FREQUENT URINATION", "polyuria / polydipsia"),
    (r"WEIGHT GAIN|OBES", "weight gain / obesity"),
    (r"WEIGHT LOSS", "weight loss"),
    (r"HEADACHE", "headache"),
    (r"DIZZ|GIDDINESS|VERTIGO", "dizziness"),
    (r"FAMILY HISTORY|F/H|FH OF", "relevant family history"),
    (r"KNOWN CASE OF|K/C/O|KCO\b|H/O DM|DIABETIC|HYPERTENSIVE", "known chronic condition"),
    (r"ON (STATIN|ATORVA|ROSUVA|METFORMIN|INSULIN|AMLOD|LOSARTAN|TELMI|THYROX|ELTROX)", "on long-term medication requiring monitoring"),
]
_NEG = re.compile(r"\b(NO|NOT|NIL|DENIES|WITHOUT|ABSENT)\s+(H/O\s+)?([A-Z/ ]{0,12})$")
VITALS = [
    (r"\bBP\s*[:\-]?\s*(\d{2,3})\s*/\s*(\d{2,3})", lambda m: f"BP {m.group(1)}/{m.group(2)}" if int(m.group(1)) >= 140 or int(m.group(2)) >= 90 else None),
    (r"\b(TEMP|T)\s*[:\-]?\s*(3[89]|4[01])(\.\d)?\s*°?\s*C?", lambda m: f"temperature {m.group(2)}{m.group(3) or ''} °C"),
    (r"\b(SPO2|SAO2|O2 SAT)\s*[:\-]?\s*(\d{2})\s*%?", lambda m: f"SpO2 {m.group(2)}%" if int(m.group(2)) < 95 else None),
    (r"\bBMI\s*[:\-]?\s*(\d{2}(\.\d)?)", lambda m: f"BMI {m.group(1)}" if float(m.group(1)) >= 25 else None),
    (r"\b(PR|PULSE|HR)\s*[:\-]?\s*(\d{2,3})\b", lambda m: f"pulse {m.group(2)}/min" if int(m.group(2)) > 100 else None),
]


def case_findings(text: Optional[str], limit: int = 6) -> List[str]:
    if not text:
        return []
    t = re.sub(r"\s+", " ", text.upper())
    out: List[str] = []
    for pat, label in FINDINGS:
        for m in re.finditer(pat, t):
            if _NEG.search(t[max(0, m.start() - 20):m.start()]):
                continue
            if label not in out and not (label == "fever" and "high-grade fever" in out):
                out.append(label)
            break
    for pat, fn in VITALS:
        m = re.search(pat, t)
        if m:
            v = fn(m)
            if v:
                out.append(v)
    return out[:limit]


# ---------------------------------------------------------------------------
# Results on the claim (Observation Type=Result) — abnormal values only
# (name pattern, low, high, label)
# ---------------------------------------------------------------------------
# (name pattern, low, high, label, alt: (test, low, high) for the other common unit — used when `test(value)` is true)
RANGES = [
    (r"HBA1C|A1C|GLYCO", None, 5.7, "HbA1c %", None),
    (r"^(FBS|FBG|GLU|GLUCOSE|FPG|RBS)", 70, 100, "glucose", (lambda v: v < 30, 3.9, 5.6)),
    (r"^(LDL|LDLC|LDL-C)$", None, 130, "LDL", (lambda v: v < 15, None, 3.4)),
    (r"^(CHOL|TC|TOTAL ?CHOL(ESTEROL)?)$", None, 200, "total cholesterol", (lambda v: v < 20, None, 5.2)),
    (r"^(TG|TRIG|TRIGLYCERIDES?)$", None, 150, "triglycerides", (lambda v: v < 20, None, 1.7)),
    (r"^(HDL|HDLC|HDL-C)$", 40, None, "HDL", (lambda v: v < 10, 1.0, None)),
    (r"^(ALT|SGPT|GPT)", None, 41, "ALT", None),
    (r"^(AST|SGOT|GOT)", None, 41, "AST", None),
    (r"CRP", None, 5, "CRP", None),
    (r"^(WBC|TLC|WCC)", 4, 11, "WBC", None),
    (r"^(HB|HGB|HAEMOGLOBIN|HEMOGLOBIN)$", 12, None, "haemoglobin", None),
    (r"URIC|^UA$", None, 7.2, "uric acid", (lambda v: v > 30, None, 420)),
    (r"CREAT", None, 1.3, "creatinine", (lambda v: v > 20, None, 115)),
    (r"^TSH", 0.4, 4.5, "TSH", None),
    (r"ESR", None, 20, "ESR", None),
    (r"FERRITIN", 30, None, "ferritin", None),
]


def abnormal_results(claim_el) -> List[str]:
    out: List[str] = []
    seen = set()
    if claim_el is None:
        return out
    for o in claim_el.iter("Observation"):
        typ = (o.findtext("Type") or "").strip().lower()
        code = (o.findtext("Code") or "").strip().upper()
        val = (o.findtext("Value") or "").strip()
        if typ != "result" or not code:
            continue
        try:
            v = float(re.sub(r"[^\d.\-]", "", val))
        except ValueError:
            continue
        for pat, lo, hi, label, alt in RANGES:
            if re.search(pat, code):
                if label in seen:      # first value per test only (labs repeat a code for ratios / other units)
                    break
                seen.add(label)
                if alt and alt[0](v):
                    lo, hi = alt[1], alt[2]
                if (lo is not None and v < lo) or (hi is not None and v >= hi):
                    s = f"{label} {v:g}" + (" (low)" if lo is not None and v < lo else " (high)")
                    if s not in out:
                        out.append(s)
                break
    return out[:6]


# ---------------------------------------------------------------------------
def _match(code: str, dx_codes: List[str]) -> Optional[Tuple[str, str, str]]:
    code = code.upper()
    norm = [d.upper().replace(".", "") for d in dx_codes]
    for codes, prefixes, indication, ref in GUIDELINES:
        if code not in codes:
            continue
        for d, n in zip(dx_codes, norm):
            for p in prefixes:
                pn = p.upper().replace(".", "")
                if n.startswith(pn):
                    return indication, ref, d
    return None


def frequency_note(code: str, f: Dict[str, Any]) -> str:
    if not f.get("count"):
        return f"No previous {code} for this member in the last 12 months (this facility)."
    tail = f"; {f['count']} earlier tests in the last 12 months, {f['paid_count']} paid" if f["count"] > 1 else ""
    note = f"Previous {code} on {f['last_date']} ({f['days_since']} days earlier, {f['last_status']}{tail})."
    if f.get("last_paid_date") and f["last_paid_date"] != f["last_date"]:
        note += f" Last paid {f['last_paid_date']} ({f['last_paid_days']} days earlier)."
    elif any(p["status"] == "no RA" for p in f["prior"]):
        note += " No earlier test is confirmed paid — remittance missing for some; check before claiming it is not a repeat."
    else:
        note += " None of the earlier tests was paid."
    return note


def groups_lines(groups: Dict[Tuple[str, str, str], List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    return [x for lines in groups.values() for x in lines]


def evidence(denied: List[Dict[str, Any]], dx_list: List[Dict[str, Any]], case_sheet: Optional[str], claim_el,
             pdx: Dict[str, Any], dos: str, freq: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Annotate MNEC lines in `denied` (adds d['mnec']) and return the grouped MNEC comment + summary.
    `freq` (code -> earlier billing in the last 12 months) adds a frequency note to every MNEC line and,
    for MNEC-005 (too frequent), a frequency sentence to the comment."""
    freq = freq or {}
    dx_codes = [d["code"] for d in dx_list if d.get("code")]
    dx_desc = {d["code"]: d.get("description") or "" for d in dx_list}
    findings = case_findings(case_sheet)
    results = abnormal_results(claim_el)
    groups: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    weak: List[Dict[str, Any]] = []
    for d in denied:
        if not (str(d["facts"].get("denial_code") or "").upper().startswith("MNEC")
                or str((d.get("suggestion") or {}).get("category") or "").startswith("Medical necessity")):
            continue
        m = _match(d["code"], dx_codes)
        reason = NEEDS_REASON.get(d["code"].upper())
        if m:
            d["mnec"] = {"weak": False, "indication": m[0], "guideline": m[1], "dx": m[2], "needs_reason": reason}
            d["suggestion"]["justification"] = (f"{d['code']} ordered for {m[0]} ({m[2]}), as recommended by the {m[1]}."
                                                + (f" [State {reason}.]" if reason else ""))
            groups.setdefault(m, []).append(d)
        else:
            d["mnec"] = {"weak": True, "needs_reason": reason,
                         "why": "No guideline links this service to the diagnoses on the claim — add the supporting "
                                "diagnosis from the notes, or consider accepting the denial."}
            weak.append(d)
        f = freq.get(d["code"].upper())
        if f is not None:
            d["mnec"]["frequency"] = {**f, "note": frequency_note(d["code"], f)}
    if not groups and not weak:
        return {"comment": "", "findings": findings, "results": results, "weak": 0}

    p = f"{pdx.get('code', '')} ({pdx.get('description') or 'see notes'})" if pdx else "[diagnosis]"
    sec = [f"{c} ({dx_desc.get(c) or ''})".replace(" ()", "") for c in dx_codes if c != pdx.get("code")][:3]
    parts = [f"Medical necessity for services on {dos}. Principal diagnosis {p}"
             + (f"; secondary diagnoses {', '.join(sec)}" if sec else "") + "."]
    if findings:
        parts.append("Clinical findings documented: " + ", ".join(findings) + ".")
    else:
        parts.append("Clinical findings: [symptoms / examination findings from the case sheet].")
    if results:
        parts.append("Relevant results: " + ", ".join(results) + ".")
    for (indication, ref, dx), lines in groups.items():
        codes = ", ".join(f"{x['code']}{' (' + x['description'] + ')' if x.get('description') else ''}" for x in lines)
        parts.append(f"{codes}: ordered for {indication} ({dx}), as recommended by the {ref}.")
        for x in lines:
            if x["mnec"]["needs_reason"]:
                parts.append(f"{x['code']}: [state {x['mnec']['needs_reason']}].")
    for x in weak:
        extra = f" — state {x['mnec']['needs_reason']}" if x["mnec"]["needs_reason"] else ""
        parts.append(f"{x['code']}{' (' + x['description'] + ')' if x.get('description') else ''}: "
                     f"[clinical indication from the notes{extra}].")
    freq_lines = [x for x in groups_lines(groups) + weak
                  if (str(x["facts"].get("denial_code") or "").upper() == "MNEC-005"
                      or (x.get("suggestion") or {}).get("category") == "Medical necessity — frequency")
                  and x["mnec"].get("frequency")]
    fgroups: Dict[Tuple[Any, Any], List[str]] = {}
    for x in freq_lines:
        f = x["mnec"]["frequency"]
        unknown = next((p["date"] for p in f["prior"] if p["status"] == "no RA"), None) if not f["last_paid_date"] else None
        fgroups.setdefault((f["last_paid_date"], f["last_paid_days"], unknown), []).append(x["code"])
    for (paid_date, days, unknown), codes in fgroups.items():
        cs = ", ".join(codes)
        if unknown:
            parts.append(f"{cs}: [check payment of the previous test on {unknown} — no remittance on file; if unpaid, "
                         f"state this is not a repeat of a paid service, else give the reason to repeat].")
        elif not paid_date:
            parts.append(f"{cs}: not a repeat of a paid service — no previous {'test' if len(codes) == 1 else 'tests'} "
                         f"for this member paid in the last 12 months.")
        else:
            parts.append(f"{cs}: previously paid on {paid_date} ({days} days earlier); repeat justified by "
                         f"[change in condition / medication adjustment / abnormal previous result].")
    parts.append("Clinical notes" + (" and lab reports" if results else "") + " attached.")
    return {"comment": " ".join(parts), "findings": findings, "results": results, "weak": len(weak)}
