"""
denial_category.py — category of a denied activity from the payer's OWN reason for that activity.

Payer reasons are usually one claim-level string covering several activities, each part naming its codes:
  '1. GP referral required but not found~Requested service was repeated too frequently{82947, 84550}'
  '{{Service is repeated too soon[9], Service item is paid as per tariff in the previous submission[94640]}}'
  '[82465]  The procedure code (82465) does not match with any of the diagnosis ...'
  '96374-THER/PROPH/DIAG INJ IV PUSH, GENERAL SERVICES DETAIL-ADM14 - We have approved/settled ...'
line_reason() keeps only the parts that apply to the given activity (parts naming no code apply to all);
classify() maps that text to a category, falling back to the denial-code category.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

CONSULT = re.compile(r"^(9|10|11)(\.\d+)?$")
_CODE = r"(?:\d{4,5}|[A-Z]\d{4}|\d{4}-\d{6}-\d{4}|\d{1,2}(?:\.\d{1,2})?)"
_LIST = re.compile(r"[\{\[]\s*(" + _CODE + r"(?:\s*,\s*" + _CODE + r")*)\s*[\}\]]")
_PREFIX = re.compile(r"^\s*\(?(" + _CODE + r")\)?\s*[-–)]\s*")
_SPLIT = re.compile(r"\s*(?:~|\|\||\n|;|(?<=[\}\]]),\s*|,\s*(?=\[)|,\s*(?=[A-Z][a-z]))\s*")


def _norm(c: str) -> str:
    return c.strip().upper()


def _codes_in(seg: str) -> List[str]:
    out = []
    for grp in _LIST.findall(seg):
        out += [_norm(c) for c in grp.split(",")]
    m = _PREFIX.match(seg)
    if m:
        out.append(_norm(m.group(1)))
    return out


def _parts(reason: str, act: str):
    r = (reason or "").replace("{{", "").replace("}}", "").replace("[[", "").replace("]]", "")
    act = _norm(act or "")
    mine, generic, any_codes = [], [], False
    for p in [p for p in _SPLIT.split(r) if p and p.strip(" .,-")]:
        cs = _codes_in(p)
        if cs:
            any_codes = True
            if act in cs:
                mine.append(p)
        else:
            generic.append(p)
    return mine, generic, any_codes


def line_reason(reason: str, act: str) -> Tuple[str, bool]:
    """(text that applies to this activity, cited_elsewhere). cited_elsewhere = parts name codes, none is this one."""
    r = (reason or "").replace("{{", "").replace("}}", "").replace("[[", "").replace("]]", "")
    act = _norm(act or "")
    parts = [p for p in _SPLIT.split(r) if p and p.strip(" .,-")]
    if not parts:
        return "", False
    mine, generic, any_codes = [], [], False
    for p in parts:
        cs = _codes_in(p)
        if cs:
            any_codes = True
            if act in cs:
                mine.append(p)
        else:
            generic.append(p)
    if mine:
        return " ~ ".join(mine + generic), False
    if any_codes and not generic:
        return "", True
    return " ~ ".join(generic), any_codes and not mine and not generic


# signal -> category, in priority order (first match wins)
_RULES: List[Tuple[str, str]] = [
    (r"(clinician|provider|doctor|physician|practitioner)\W+(\w+\W+){0,3}licen[cs]e|licen[cs]e (is )?(expired|invalid)|"
     r"no licen[cs]e", "Clinician licence"),
    (r"referral", "GP referral"),
    (r"resubmitted amount is greater than denied amount|not compliant with resubmission type", "Resubmission type error"),
    (r"cancell?ed on provider|recalled by provider", "Recalled by provider"),
    (r"direct bil+ing", "Coverage / eligibility"),
    (r"missing tooth|tooth number", "Coding"),
    (r"receiver is not responsible|wrong submission", "Wrong receiver"),
    (r"late submission|time.?barred|submitted after|reconciled/signed-off period|maximum allowed submissions",
     "Late submission"),
    (r"paid as per tariff in the previous|paid in the previous|already paid|duplicate and already paid", "Duplicate — already paid"),
    (r"patient share|beneficiary share|deductible|co.?participation|excess copay|co.?pay|co.?insurance", "Patient share"),
    (r"panel must include|this panel must", "Coding — panel composition"),
    (r"initial consultation code for the same condition|consultation.{0,40}within \d+ days|follow.?up (visit|consultation)|"
     r"another consultation with", "Consultation visit rules"),
    (r"within the period of ip admission|during (ip|inpatient) admission", "Inpatient overlap"),
    (r"does not exist in provider agreement|not in (the )?provider agreement|not part of (the )?contract", "Contract & tariff"),
    (r"waiting period|could not identify the payer|member not covered", "Coverage / eligibility"),
    (r"too frequent|repeated too|too soon", "FREQ"),
    (r"does not match with any of the diagnosis|cpts? does not match|does not match (the )?prescribed diagnosis|"
     r"services does not match", "Medical necessity"),
    (r"bundl|cannot be billed together|excludes ?1|mue value|primary diagnosis|principal diagnosis|coding guideline|"
     r"remove the chronic icd|code does not exist|not found in fdb|cannot be billed for adult|not allowed with the service code",
     "Coding"),
    (r"approved/settled the service as per the agreed tariff", "Contract & tariff"),
    (r"duplicat", "Duplicate"),
    (r"more than agreed tariff|claimed amount|price", "Contract & tariff"),
    (r"not justified|not clinically|not medically|medical necessity for|not indicated|clinically indicated", "Medical necessity"),
    (r"agreed tariff|partial rejection|partially approved|approved as per", "Contract & tariff"),
    (r"requires approval|approval (is )?required|pre.?auth|preauthori[sz]ation|authori[sz]ation (not|missing)|"
     r"approval not|link the valid approval", "Prior approval"),
    (r"not covered|not eligible|eligib|member id is not validated|exclusion|excluded|benefit", "Coverage / eligibility"),
]
# category from the denial code when the payer's reason does not say (blank / generic)
CODE_FALLBACK = [(r"^MNEC-005", "Medical necessity — frequency"), (r"^CLAI-012", "CLAI012"), (r"^COPY", "Patient share"), (r"^CLAI-014", "Resubmission type error"), (r"^CLAI-018", "Recalled by provider"),
                 (r"^CLAI-017", "Coverage / eligibility"), (r"^CLAI-013", "Coding"), (r"^SURC", "Medical necessity"),
                 (r"^WRNG", "Wrong receiver"), (r"^TIME-001", "Late submission")]
CLAIM_WIDE = {"Clinician licence", "Late submission", "Coverage / eligibility", "Inpatient overlap"}
_NOISE = re.compile(r"approval test chk", re.I)


def _match(t: str, act: str) -> Optional[str]:
    t = t.lower()
    for pat, cat in _RULES:
        if re.search(pat, t):
            if cat == "FREQ":
                return "Consultation visit rules" if CONSULT.match(_norm(act)) else "Medical necessity — frequency"
            if cat == "Consultation visit rules" and not CONSULT.match(_norm(act)):
                continue   # consultation rule text on a non-consultation line
            return cat
    return None


def classify(reason: str, act: str, code_category: str, denial_code: str = "") -> Tuple[str, str, bool]:
    """(category, the text it was decided from, cited_elsewhere). Parts naming this activity decide first,
    then parts naming no activity; parts naming only other activities are ignored."""
    clean = _NOISE.sub("", reason or "")
    mine, generic, any_codes = _parts(clean, act)
    text, _ = line_reason(clean, act)
    if any_codes and not mine and not generic:
        return (code_category if code_category != "Consultation visit rules" else "Medical necessity"), "", True
    if generic:   # claim-wide reasons that deny every line whatever else is said about it
        g = _match(" ~ ".join(generic), act)
        if g in CLAIM_WIDE:
            return g, " ~ ".join(generic), False
    for group in (mine, generic):
        if group:
            cat = _match(" ~ ".join(group), act)
            if cat:
                return cat, " ~ ".join(group), False
    for pat, cat in CODE_FALLBACK:
        if re.search(pat, (denial_code or "").upper()):
            if cat == "CLAI012":   # contractual non-compliance, never prior approval by default
                cat = "Contractual (CLAI-012)" if text.strip(" -") else "Contractual (CLAI-012, no reason given)"
            return cat, text, False
    if code_category == "Consultation visit rules" and not CONSULT.match(_norm(act)):
        return "Medical necessity — alternative service", text, False   # MNEC-006 on a non-consultation line
    return code_category, text, False
