import { useEffect, useMemo, useState } from "react";
import apiClient from "../api/client";
import { useAuth } from "../context/AuthContext";
import PrepareResubmission from "../components/resub/PrepareResubmission";

// RA last 21 days (SN 03/2019): every denied claim from RAs received in the window, grouped by RA file.
// Scrubbed claims get the resubmission comment prepared automatically; the rest are written by the team.
const STAGE = {
  candidate:      { label: "Resubmission candidate", cls: "bg-emerald-100 text-emerald-800" },
  reconciliation: { label: "Reconciliation (1st resub done)", cls: "bg-violet-100 text-violet-800" },
  awaiting_ra:    { label: "Resubmitted · awaiting RA", cls: "bg-amber-100 text-amber-800" },
  limit:          { label: "SN limit reached", cls: "bg-slate-200 text-slate-700" },
};
const fmt = (n) => Number(n || 0).toLocaleString("en-AE", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

function ClaimPanel({ facilityId, c, isAdmin, onFullEditor }) {
  const [draft, setDraft] = useState(null);
  const [err, setErr] = useState("");
  const [rtype, setRtype] = useState("");
  const [comment, setComment] = useState("");
  const [gen, setGen] = useState(null);
  const [busy, setBusy] = useState("");
  const [up, setUp] = useState(null);
  const [auths, setAuths] = useState({});   // activity_id -> prior approval number (AUTH denials)

  useEffect(() => {
    apiClient.get("/resub-prepare/draft", { params: { facility_id: facilityId, claim_id: c.claim_id } })
      .then((r) => {
        setDraft(r.data);
        const s = r.data.suggestion || {};
        const a = {};
        (r.data.denied || []).forEach((d) => { if (/^AUTH/i.test(d.facts?.denial_code || "")) a[d.activity_id] = d.facts?.prior_auth || ""; });
        setAuths(a);
        setRtype(c.stage === "reconciliation" ? "reconciliation" : (s.resubmission_type === "correction" ? "correction" : "internal complaint"));
        setComment(c.scrubbed ? (s.comment || "") : "");
      })
      .catch((e) => setErr(e?.response?.data?.detail || "Could not prepare this claim."));
  }, [facilityId, c.claim_id, c.scrubbed, c.stage]);

  async function build() {
    setBusy("build"); setGen(null); setUp(null);
    try {
      const activities = {};
      let text = comment.trim();
      Object.entries(auths).forEach(([aid, no]) => {
        const v = (no || "").trim();
        if (!v) return;
        activities[aid] = { prior_authorization_id: v };
        const d = (draft.denied || []).find((x) => x.activity_id === aid);
        if (!text.includes(v)) text += ` Prior approval ${v} for ${d?.code || "activity " + aid}.`;
      });
      setComment(text);
      const edits = { resubmission_type: rtype, comment: text, activities, scope: rtype === "internal complaint" ? "denied_only" : "whole" };
      const r = await apiClient.post("/resub-prepare/xml", { facility_id: facilityId, claim_id: c.claim_id, disposition: "TEST", edits });
      setGen(r.data);
    } catch (e) { setGen({ errors: [e?.response?.data?.detail || "Build failed"] }); }
    setBusy("");
  }
  async function upload() {
    setBusy("upload");
    try {
      const r = await apiClient.post("/resub-prepare/upload", { facility_id: facilityId, id: gen.id, disposition: "TEST" });
      setUp(r.data);
    } catch (e) { setUp({ status: "refused", message: e?.response?.data?.detail || "Upload failed" }); }
    setBusy("");
  }

  if (err) return <div className="p-3 text-xs text-red-700">{err}</div>;
  if (!draft) return <div className="p-3 text-xs text-slate-500">Preparing…</div>;
  const s = draft.suggestion || {};
  const total = draft.facts?.activities?.length || 0;
  const nDenied = (draft.denied || []).length;
  const authMissing = Object.values(auths).some((v) => !(v || "").trim());
  return (
    <div className="space-y-3 bg-slate-50 p-4 text-sm">
      <p className="text-xs font-semibold text-slate-700">Denied activities: {nDenied}/{total}
        <span className="ml-2 font-normal text-slate-500">({total - nDenied} paid)</span></p>
      {draft.mnec && (draft.mnec.weak > 0 || draft.mnec.findings?.length > 0 || draft.mnec.results?.length > 0 || draft.denied?.some((d) => d.mnec)) && (
        <div className="rounded border border-slate-200 bg-white p-2 text-xs text-slate-700">
          <b>MNEC evidence</b> · case sheet {draft.mnec.case_sheet ? "found" : "not available"}
          {draft.mnec.findings?.length > 0 && <> · findings: {draft.mnec.findings.join(", ")}</>}
          {draft.mnec.results?.length > 0 && <> · results: {draft.mnec.results.join(", ")}</>}
          {draft.mnec.weak > 0 && <span className="ml-1 text-red-700">· {draft.mnec.weak} weak line{draft.mnec.weak > 1 ? "s" : ""}</span>}
        </div>
      )}
      <div className="grid gap-2 text-xs text-slate-600 md:grid-cols-2">
        {(draft.denied || []).map((d) => (
          <div key={d.activity_id} className="rounded border border-slate-200 bg-white p-2">
            <span className="font-mono">{d.code}</span> · {d.facts?.denial_code} — {d.facts?.denial_description || ""}
            {d.suggestion?.category && <span className="ml-1 rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-semibold text-slate-700">{d.suggestion.category}</span>}
            <span className="float-right text-red-700">{fmt((Number(d.facts?.net) || 0) - (Number(d.facts?.paid) || 0))}</span>
            {d.mnec && (d.mnec.weak
              ? <div className="mt-1 rounded bg-red-50 px-1.5 py-1 text-[11px] text-red-800">Weak: {d.mnec.why}</div>
              : <div className="mt-1 text-[11px] text-teal-800">Supported: {d.mnec.indication} ({d.mnec.dx}) — {d.mnec.guideline}</div>)}
            {d.mnec?.needs_reason && <div className="mt-1 text-[11px] text-amber-800">State {d.mnec.needs_reason}.</div>}
            {d.mnec?.frequency && (
              <div className={`mt-1 text-[11px] ${d.mnec.frequency.last_paid_date ? "text-amber-800" : "text-slate-600"}`}>
                Frequency: {d.mnec.frequency.note}
                {d.mnec.frequency.count > 1 && <> Earlier: {d.mnec.frequency.prior.slice(1, 4).map((p) => `${p.date} (${p.days_before}d, ${p.status})`).join(", ")}{d.mnec.frequency.count > 4 ? " …" : ""}</>}
              </div>
            )}
            {d.activity_id in auths && (
              <label className="mt-2 block text-xs text-slate-700">Prior approval number (required for {d.facts?.denial_code})
                <input value={auths[d.activity_id]} onChange={(e) => setAuths((a) => ({ ...a, [d.activity_id]: e.target.value }))}
                  placeholder="Approval / e-auth number"
                  className={`mt-1 w-full rounded border px-2 py-1 font-mono text-xs ${auths[d.activity_id]?.trim() ? "border-slate-300" : "border-red-400"}`} />
                {d.facts?.prior_auth && <span className="text-slate-500">On the original claim: {d.facts.prior_auth}</span>}
              </label>
            )}
          </div>
        ))}
      </div>
      <div className="flex flex-wrap items-end gap-3">
        <label className="text-xs text-slate-600">Resubmission type
          <select value={rtype} onChange={(e) => setRtype(e.target.value)} className="mt-1 block rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            {c.stage === "reconciliation"
              ? <option value="reconciliation">reconciliation (needs payer agreement)</option>
              : <>
                  <option value="correction">correction — codes changed, all activities</option>
                  <option value="internal complaint">internal complaint — comment / report only, denied lines</option>
                </>}
          </select>
        </label>
        <span className="text-xs text-slate-500">
          {rtype === "correction" ? "All activities (paid and denied) are resubmitted." :
           rtype === "internal complaint" ? "Only the denied activities are resubmitted." : "Reconciliation only after agreement with the payer."}
        </span>
      </div>
      <label className="block text-xs text-slate-600">
        Resubmission comment {c.scrubbed ? "(prepared from the scrub findings and denial — check and edit)" : "(not scrubbed — write the comment)"}
        <textarea value={comment} onChange={(e) => setComment(e.target.value)} rows={4}
          placeholder={s.comment || "Write the justification for the payer…"}
          className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm" />
      </label>
      {rtype === "correction" && <p className="text-xs text-amber-700">For a correction, change the codes in the full editor first.</p>}
      <div className="flex flex-wrap gap-2">
        <button type="button" className="gh-btn-secondary gh-btn-sm" onClick={onFullEditor}>Open full editor (codes, approval, attachment)</button>
        <button type="button" className="gh-btn-primary gh-btn-sm" disabled={busy || !comment.trim() || authMissing} onClick={build}>
          {busy === "build" ? "Building…" : "Build TEST file"}</button>
        {gen?.id && !gen.errors?.length && isAdmin && (
          <button type="button" className="gh-btn-secondary gh-btn-sm" disabled={!!busy} onClick={upload}>
            {busy === "upload" ? "Uploading…" : "Test upload to DHPO"}</button>
        )}
      </div>
      {gen?.errors?.length > 0 && <ul className="list-disc pl-5 text-xs text-red-700">{gen.errors.map((e, i) => <li key={i}>{e}</li>)}</ul>}
      {gen?.id && !gen.errors?.length && <p className="text-xs text-emerald-700">TEST file built ({gen.filename}). {isAdmin ? "DHPO validates a TEST file and returns errors; it is not stored or sent to the payer." : "An admin can run the DHPO test upload."}</p>}
      {up && <p className={`text-xs ${up.status?.startsWith("uploaded") ? "text-emerald-700" : "text-red-700"}`}>
        DHPO TEST: {up.status}{up.dhpo_result != null ? ` (result ${up.dhpo_result})` : ""}{up.message ? ` — ${up.message}` : ""}</p>}
    </div>
  );
}

export default function Ra21Resub({ facilityId }) {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [stage, setStage] = useState("candidate");
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(null);
  const [full, setFull] = useState(null);

  useEffect(() => {
    if (!facilityId) return;
    setData(null); setError("");
    apiClient.get("/resub-prepare/ra21", { params: { facility_id: facilityId } })
      .then((r) => setData(r.data)).catch((e) => setError(e?.response?.data?.detail || "Could not load RAs."));
  }, [facilityId]);

  const files = useMemo(() => {
    if (!data) return [];
    const s = q.trim().toLowerCase();
    return data.files.map((f) => ({ ...f, claims: f.claims.filter((c) => (!stage || c.stage === stage) &&
      (!s || [c.claim_id, c.payer_id, f.ra_file, ...c.scrub_rules].join(" ").toLowerCase().includes(s))) }))
      .filter((f) => f.claims.length);
  }, [data, stage, q]);
  const counts = useMemo(() => {
    const k = {}; (data?.files || []).forEach((f) => f.claims.forEach((c) => { k[c.stage] = (k[c.stage] || 0) + 1; })); return k;
  }, [data]);

  if (error) return <div className="gh-card border-red-200 bg-red-50 p-4 text-sm text-red-700">{error}</div>;
  if (!data) return <div className="gh-card p-8 text-center text-sm text-slate-500">Loading RAs from the last 21 days…</div>;

  return (
    <div className="space-y-3">
      <div className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-xs text-amber-900">
        Every denied claim from RAs received in the last {data.window_days} days (SN 03/2019: resubmit within 21 days of the RA).
        A claim on its first RA with a denial is a <b>resubmission candidate</b>; after the 1st resubmission it moves to <b>reconciliation</b>.
        Scrubbed claims get the comment prepared automatically; for the others the team writes it.
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {[["candidate"], ["reconciliation"], ["awaiting_ra"], ["limit"], [""]].map(([k]) => (
          <button key={k || "all"} type="button" onClick={() => setStage(k)}
            className={`rounded-full border px-3 py-1 text-xs ${stage === k ? "border-teal-600 bg-teal-50 text-teal-800" : "border-slate-200 text-slate-600"}`}>
            {k ? STAGE[k].label : "All"} ({k ? counts[k] || 0 : Object.values(counts).reduce((a, b) => a + b, 0)})
          </button>
        ))}
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Claim ID, payer, RA file, rule…"
          className="ml-auto w-64 rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
      </div>
      {files.length === 0 && <div className="gh-card p-6 text-center text-sm text-slate-500">No claims in this list.</div>}
      {files.map((f) => (
        <div key={f.ra_file} className="gh-card overflow-hidden">
          <div className="flex flex-wrap items-center gap-3 border-b border-slate-100 bg-slate-50 px-4 py-2 text-xs">
            <span className="font-medium text-slate-800">{f.ra_file}</span>
            <span className="text-slate-500">RA {f.remittance_date}</span>
            <span className={f.days_left <= 5 ? "font-semibold text-red-700" : "text-slate-600"}>resubmit by {f.resubmit_by} · {f.days_left}d left</span>
            <span className="ml-auto text-slate-600">{f.claims.length} claims · AED {fmt(f.claims.reduce((a, c) => a + c.denied, 0))} denied</span>
          </div>
          <table className="min-w-full text-sm">
            <tbody className="divide-y divide-slate-100">
              {f.claims.map((c) => {
                const key = f.ra_file + c.claim_id;
                const canWork = c.stage === "candidate" || c.stage === "reconciliation";
                return [
                  <tr key={key} className="align-top">
                    <td className="px-4 py-2 font-mono text-xs">{c.claim_id}<div className="font-sans text-slate-500">{c.payer_id} · enc {c.encounter_date}</div></td>
                    <td className="px-3 py-2"><span className={`rounded px-1.5 py-0.5 text-xs ${STAGE[c.stage].cls}`}>{STAGE[c.stage].label}</span>
                      <div className="mt-1 text-xs text-slate-500">{c.resubs_used} of 2 resubmissions used</div></td>
                    <td className="px-3 py-2 text-xs">{c.scrubbed
                      ? <span className="text-teal-700">Scrubbed{c.scrub_rules.length ? `: ${c.scrub_rules.slice(0, 2).join(", ")}${c.scrub_rules.length > 2 ? "…" : ""}` : " · clear"}</span>
                      : <span className="text-slate-500">Not scrubbed · manual comment</span>}</td>
                    <td className="px-3 py-2 text-right text-xs">net {fmt(c.net)}<div className="font-semibold text-red-700">denied {fmt(c.denied)}</div></td>
                    <td className="px-4 py-2 text-right">{canWork &&
                      <button type="button" className="gh-btn-secondary gh-btn-sm" onClick={() => setOpen(open === key ? null : key)}>
                        {open === key ? "Close" : "Prepare resubmission"}</button>}</td>
                  </tr>,
                  open === key && <tr key={key + "p"}><td colSpan={5}>
                    <ClaimPanel facilityId={facilityId} c={c} isAdmin={isAdmin}
                      onFullEditor={() => setFull({ facility_id: facilityId, claim_id: c.claim_id })} /></td></tr>,
                ];
              })}
            </tbody>
          </table>
        </div>
      ))}
      {full && <PrepareResubmission claim={full} onClose={() => setFull(null)} />}
    </div>
  );
}
