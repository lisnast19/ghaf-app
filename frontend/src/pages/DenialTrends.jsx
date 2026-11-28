/**
 * DenialTrends.jsx — monthly denial trends per payer + scrubber rule coverage ("rule gaps").
 *
 * Complements Denial Intelligence (Calibration.jsx: all-time top codes, rule precision, recall queue).
 * API: GET /api/denial-trends/monthly, GET /api/denial-trends/rule-coverage
 */
import { Suspense, lazy, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import apiClient from "../api/client";
import DenialDrill from "./DenialDrill";
import { useAuth } from "../context/AuthContext";
import { readFacility, saveFacility } from "../utils/facility";
import useFacilityDefault from "../hooks/useFacilityDefault";

const TrendChart = lazy(() =>
  import("recharts").then(({ BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer }) => ({
    default: ({ data }) => (
      <ResponsiveContainer width="100%" height={180}>
        <BarChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
          <XAxis dataKey="month" tick={{ fontSize: 11 }} />
          <YAxis tick={{ fontSize: 11 }} width={48} />
          <Tooltip formatter={(v, n) => [n === "amount" ? `AED ${fmt(v)}` : Number(v).toLocaleString(), n === "amount" ? "Denied amount" : "Denied lines"]} />
          <Bar dataKey="lines" fill="#0f766e" radius={[3, 3, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    ),
  }))
);

const fmt = n => (n == null ? "—" : Number(n).toLocaleString("en-AE", { maximumFractionDigits: 0 }));
const monthLabel = m => {
  if (!m) return "";
  const [y, mo] = m.split("-");
  return new Date(Number(y), Number(mo) - 1, 1).toLocaleString("en-GB", { month: "short", year: "numeric" });
};

function Trend({ pct, diff }) {
  if (pct == null) return <span className="gh-chip gh-chip-neutral">new</span>;
  if (Math.abs(pct) < 5) return <span className="text-xs text-slate-500">→ {pct}%</span>;
  const up = pct > 0;   // more denials = bad
  return (
    <span className={`text-xs font-semibold ${up ? "text-red-600" : "text-emerald-600"}`}>
      {up ? "▲" : "▼"} {Math.abs(pct)}%{diff != null ? ` (${diff > 0 ? "+" : ""}${fmt(diff)})` : ""}
    </span>
  );
}

function WhyBtn({ onClick }) {
  return (
    <button onClick={onClick} title="See the TPA reasons, services, clinicians and diagnoses behind this code"
      className="ml-1 rounded-full border border-ghaf-600/40 px-2 py-0.5 text-[11px] font-semibold text-ghaf-600 hover:bg-ghaf-600 hover:text-white">
      Why?
    </button>
  );
}

function Coverage({ c }) {
  if (c === "exact") return <span className="gh-chip gh-chip-paid">rule</span>;
  if (c === "family") return <span className="gh-chip gh-chip-pending">family only</span>;
  return <span className="gh-chip gh-chip-denied">no rule</span>;
}

const STAGE_LABEL = { new: "New denials", redenied: "Re-denied after resubmission", all: "All denials" };

function Outcomes({ facilityId, payer }) {
  const [months, setMonths] = useState(3);
  const [d, setD] = useState(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    setD(null); setErr("");
    const params = { months };
    if (facilityId) params.facility_id = facilityId;
    if (payer) params.payer = payer;
    apiClient.get("/denial-trends/resub-outcomes", { params, timeout: 300000 })
      .then(r => setD(r.data)).catch(e => setErr(e?.response?.data?.detail || "Could not load outcomes"));
  }, [facilityId, payer, months]);
  const t = d?.totals;
  const answered = t ? t.paid_full + t.paid_part + t.redenied : 0;
  const n = v => (v == null ? "—" : Math.round(v).toLocaleString());
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="max-w-3xl text-sm text-slate-600">
          What happened to the lines the team <b>resubmitted</b>, by payer and original denial code. <b>Success</b> =
          paid in full or in part on the resubmission RA. <b>Re-denied</b> lines can only go to reconciliation with the
          insurer (Resubmission worklist → Reconciliation). Use a low success rate to change the approach or stop
          routine resubmission for that code.
        </p>
        <select className="gh-input py-1.5 text-sm" value={months} onChange={e => setMonths(Number(e.target.value))}>
          {[1, 3, 6, 12].map(m => <option key={m} value={m}>Resubmitted in last {m} month{m > 1 ? "s" : ""}</option>)}
        </select>
      </div>
      {err && <p className="text-sm text-denied">{err}</p>}
      {!d && !err && <p className="text-sm text-slate-500">Matching resubmissions with their remittances…</p>}
      {t && (
        <div className="grid gap-3 sm:grid-cols-4">
          <div className="gh-card p-4"><p className="gh-label">Lines resubmitted</p><p className="mt-1 text-2xl font-semibold">{n(t.lines)}</p><p className="text-xs text-slate-500">AED {n(t.denied_amount)} originally denied</p></div>
          <div className="gh-card p-4"><p className="gh-label">Awaiting resubmission RA</p><p className="mt-1 text-2xl font-semibold">{n(t.pending)}</p><p className="text-xs text-slate-500">insurer must answer within 30 days (SN 3.9.2)</p></div>
          <div className="gh-card border-l-4 border-l-paid p-4"><p className="gh-label">Paid after resubmission</p><p className="mt-1 text-2xl font-semibold">{answered ? Math.round(100 * (t.paid_full + t.paid_part) / answered) : 0}%</p><p className="text-xs text-slate-500">AED {n(t.recovered)} recovered</p></div>
          <div className="gh-card border-l-4 border-l-denied p-4"><p className="gh-label">Re-denied</p><p className="mt-1 text-2xl font-semibold">{answered ? Math.round(100 * t.redenied / answered) : 0}%</p><p className="text-xs text-slate-500">AED {n(t.redenied_amount)} → reconciliation</p></div>
        </div>
      )}
      {d && (
        <section className="gh-card overflow-x-auto p-4">
          <table className="min-w-full text-sm">
            <thead className="text-left text-xs uppercase tracking-wide text-slate-500">
              <tr><th className="py-2 pr-3">Payer</th><th className="py-2 pr-3">Original code</th>
                <th className="py-2 pr-3 text-right">Lines</th><th className="py-2 pr-3 text-right">Awaiting RA</th>
                <th className="py-2 pr-3 text-right">Paid</th><th className="py-2 pr-3 text-right">Re-denied</th>
                <th className="py-2 pr-3 text-right">Success</th><th className="py-2 pr-3 text-right">Recovered AED</th>
                <th className="py-2 pr-3 text-right">Re-denied AED</th><th className="py-2">Top re-denial remark</th></tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {d.rows.slice(0, 60).map(r => (
                <tr key={r.payer + r.denial_code}>
                  <td className="py-1.5 pr-3">{r.payer}<span className="block max-w-[180px] truncate text-xs text-slate-500">{r.payer_name}</span></td>
                  <td className="py-1.5 pr-3"><span className="font-mono">{r.denial_code}</span><span className="block max-w-[220px] truncate text-xs text-slate-500">{r.description}</span></td>
                  <td className="py-1.5 pr-3 text-right tabular-nums">{n(r.lines)}</td>
                  <td className="py-1.5 pr-3 text-right tabular-nums text-slate-500">{n(r.pending)}</td>
                  <td className="py-1.5 pr-3 text-right tabular-nums">{n(r.paid_full + r.paid_part)}</td>
                  <td className="py-1.5 pr-3 text-right tabular-nums">{n(r.redenied)}</td>
                  <td className="py-1.5 pr-3 text-right tabular-nums">
                    {r.success_pct == null ? "—" : <span className={`gh-chip ${r.success_pct < 20 ? "gh-chip-denied" : r.success_pct < 50 ? "gh-chip-pending" : "gh-chip-paid"}`}>{Math.round(r.success_pct)}%</span>}
                  </td>
                  <td className="py-1.5 pr-3 text-right tabular-nums">{n(r.recovered)}</td>
                  <td className="py-1.5 pr-3 text-right tabular-nums">{n(r.redenied_amount)}</td>
                  <td className="max-w-[260px] py-1.5 text-xs text-slate-600">{r.top_redenial_remark || "—"}</td>
                </tr>
              ))}
              {!d.rows.length && <tr><td colSpan={10} className="py-4 text-center text-slate-500">No resubmissions in this period.</td></tr>}
            </tbody>
          </table>
        </section>
      )}
    </div>
  );
}

export default function DenialTrends() {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";
  const [facilities, setFacilities] = useState([]);
  const [params] = useSearchParams();
  // a link (e.g. from Facility alerts) can name the facility: ?facility_id=7; otherwise the last one used
  const [facilityId, setFacilityId] = useState(() => (isAdmin ? (params.get("facility_id") ?? readFacility("")) : String(user?.facility_id || "")));
  const isSuperAdmin = useFacilityDefault(facilities, facilityId, setFacilityId);
  const [payer, setPayer] = useState(() => params.get("payer") || "");
  const [months, setMonths] = useState(6);
  const [month, setMonth] = useState(() => params.get("month") || "");
  const [exporting, setExporting] = useState(false);
  const [drill, setDrill] = useState(null);
  const [stage, setStage] = useState("all");
  const [view, setView] = useState("trends");
  const exportXlsx = async () => {
    const q = new URLSearchParams({ months: String(months), stage });
    if (facilityId) q.set("facility_id", facilityId);
    if (payer) q.set("payer", payer);
    setExporting(true);
    try {
      const r = await apiClient.get(`/denial-trends/export.xlsx?${q}`, { responseType: "blob", timeout: 300000 });
      const name = ((r.headers["content-disposition"] || "").match(/filename="([^"]+)"/) || [])[1] || "denial_trends.xlsx";
      const url = URL.createObjectURL(r.data);
      const a = document.createElement("a");
      a.href = url; a.download = name; a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } finally { setExporting(false); }
  };
  const [monthly, setMonthly] = useState(null);
  const [coverage, setCoverage] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [gapsOnly, setGapsOnly] = useState(true);
  const [cats, setCats] = useState(null);

  useEffect(() => {
    if (!isAdmin) return;
    apiClient.get("/facilities")
      .then(r => setFacilities((r.data || []).filter(f => f.facility_code !== "GHAF-001")))
      .catch(() => {});
  }, [isAdmin]);

  useEffect(() => {
    const params = { months, stage };
    if (facilityId) params.facility_id = facilityId;
    if (payer) params.payer = payer;
    setLoading(true); setError("");
    Promise.all([
      apiClient.get("/denial-trends/monthly", { params: { ...params, top: 20 } }),
      apiClient.get("/denial-trends/rule-coverage", { params }),
    ])
      .then(([m, c]) => {
        setMonthly(m.data); setCoverage(c.data);
        const ms = m.data.months || [];
        // default to the last complete month
        const complete = ms.filter(x => x !== m.data.partial_month);
        setMonth(prev => (ms.includes(prev) ? prev : complete[complete.length - 1] || ms[ms.length - 1] || ""));
      })
      .catch(e => setError(e?.response?.data?.detail || "Could not load denial trends"))
      .finally(() => setLoading(false));
  }, [facilityId, payer, months, stage]);

  useEffect(() => {
    const params = { months, stage };
    if (facilityId) params.facility_id = facilityId;
    if (payer) params.payer = payer;
    setCats(null);
    apiClient.get("/denial-trends/categories", { params, timeout: 300000 })
      .then(r => setCats(r.data)).catch(() => setCats(null));
  }, [facilityId, payer, months, stage]);

  const payerOptions = monthly?.payers || [];
  const [attnFirst, setAttnFirst] = useState(true);
  const rows = useMemo(() => {
    const rs = (monthly?.rows || []).filter(r => r.month === month);
    if (attnFirst) rs.sort((a, b) => (b.attention - a.attention) || (b.lines - a.lines));
    return rs.slice(0, 40);
  }, [monthly, month, attnFirst]);
  const attnCount = (monthly?.rows || []).filter(r => r.month === month && r.attention).length;
  const isPartial = monthly?.partial_month === month;
  const codes = useMemo(
    () => (coverage?.codes || []).filter(c => !gapsOnly || c.coverage === "none"),
    [coverage, gapsOnly]
  );
  const s = coverage?.summary;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="gh-eyebrow">Denial intelligence</p>
          <h1 className="gh-h1">Denial trends</h1>
          <p className="mt-1 text-sm text-slate-600">
            Top denial codes per payer by <b>RA month</b> (the month the remittance advice arrived, not the
            treatment month), and which ones the Claim Scrubber already catches before submission.
          </p>
        </div>
        <button className="gh-btn-secondary text-sm" disabled={exporting} onClick={exportXlsx}>
          {exporting ? "Preparing…" : "Download Excel"}
        </button>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200">
        <div className="flex gap-1">
          {[["trends", "Denial trends (new denials)"], ["outcomes", "Resubmission outcomes"]].map(([k, l]) => (
            <button key={k} onClick={() => setView(k)}
              className={`-mb-px border-b-2 px-4 py-2 text-sm font-medium ${view === k ? "border-ghaf-600 text-ghaf-700" : "border-transparent text-slate-500 hover:text-slate-700"}`}>{l}</button>
          ))}
        </div>
        {view === "trends" && (
          <div className="flex items-center gap-1 pb-1 text-xs">
            {Object.entries(STAGE_LABEL).map(([k, l]) => (
              <button key={k} onClick={() => setStage(k)} className={`gh-chip ${stage === k ? "gh-chip-brand" : "gh-chip-neutral"}`}>{l}</button>
            ))}
          </div>
        )}
      </div>
      {view === "trends" && stage === "new" && (
        <p className="-mt-3 text-xs text-slate-500">Counting lines denied on the claim's <b>first</b> remittance only — the current denial pattern. Denials of resubmitted claims are on the Resubmission outcomes tab.</p>
      )}

      <section className="gh-section">
        <div className="flex flex-wrap items-end gap-3">
          {isAdmin && (
            <label className="min-w-[200px] flex-1">
              <span className="gh-label mb-1 block">Facility</span>
              <select className="gh-input w-full" value={facilityId}
                onChange={e => { setFacilityId(e.target.value); saveFacility(e.target.value); setPayer(""); }}>
                {isSuperAdmin && <option value="">All facilities</option>}
                {facilities.map(f => <option key={f.id} value={f.id}>{f.facility_name}</option>)}
              </select>
            </label>
          )}
          <label className="min-w-[200px] flex-1">
            <span className="gh-label mb-1 block">Payer / TPA</span>
            <select className="gh-input w-full" value={payer} onChange={e => setPayer(e.target.value)}>
              <option value="">All payers</option>
              {payer && !payerOptions.some(p => p.payer === payer) && <option value={payer}>{payer}</option>}
              {payerOptions.map(p => <option key={p.payer} value={p.payer}>{p.payer}{p.name ? ` — ${p.name}` : ""}</option>)}
            </select>
          </label>
          <label className="w-32">
            <span className="gh-label mb-1 block">Window</span>
            <select className="gh-input w-full" value={months} onChange={e => setMonths(Number(e.target.value))}>
              {[3, 6, 12].map(n => <option key={n} value={n}>{n} months</option>)}
            </select>
          </label>
          <label className="w-40">
            <span className="gh-label mb-1 block">RA month</span>
            <select className="gh-input w-full" value={month} onChange={e => setMonth(e.target.value)}>
              {(monthly?.months || []).map(m => (
                <option key={m} value={m}>{monthLabel(m)}{m === monthly?.partial_month ? " (so far)" : ""}</option>
              ))}
            </select>
          </label>
        </div>
        {loading && <p className="mt-3 text-sm text-slate-500">Loading… (first load of all facilities can take ~10 s)</p>}
        {error && <p className="mt-3 text-sm text-red-600">{error}</p>}
      </section>
      {view === "outcomes" ? <Outcomes facilityId={facilityId} payer={payer} /> : (<>

      {cats?.categories?.length > 0 && (
        <section className="gh-section">
          <p className="gh-eyebrow">By category · {cats.months[0] && monthLabel(cats.months[0])} – {cats.months.length && monthLabel(cats.months[cats.months.length - 1])}</p>
          <p className="mt-1 text-xs text-slate-500">Category from the payer's own reason for each activity (not the denial code alone).</p>
          <div className="mt-3 overflow-x-auto">
            <table className="gh-table w-full text-sm">
              <thead><tr>
                <th className="text-left">Category</th><th className="text-right">Lines</th><th className="text-right">Share</th>
                <th className="text-right">Denied (AED)</th>
                {cats.months.map(m => <th key={m} className="text-right">{monthLabel(m)}</th>)}
                <th className="text-left">Main denial codes</th>
              </tr></thead>
              <tbody>
                {cats.categories.map(c => (
                  <tr key={c.category}>
                    <td className="font-medium">{c.category}</td>
                    <td className="text-right">{fmt(c.lines)}</td>
                    <td className="text-right">{c.share_pct}%</td>
                    <td className="text-right">{fmt(c.amount)}</td>
                    {c.by_month.map((n, i) => <td key={i} className="text-right text-slate-600">{fmt(n)}</td>)}
                    <td className="font-mono text-xs text-slate-600">{c.top_codes.map(([k, n]) => `${k} (${n})`).join(", ")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {monthly && (
        <div className="grid gap-4 md:grid-cols-4">
          <div className="gh-card p-4">
            <p className="gh-eyebrow">Denied lines · RA {monthLabel(month)}</p>
            <p className="text-2xl font-semibold">{fmt(monthly.totals.find(t => t.month === month)?.lines)}</p>
          </div>
          <div className="gh-card p-4">
            <p className="gh-eyebrow">Denied amount · RA {monthLabel(month)}</p>
            <p className="text-2xl font-semibold">AED {fmt(monthly.totals.find(t => t.month === month)?.amount)}</p>
          </div>
          <div className="gh-card p-4">
            <p className="gh-eyebrow">Lines with a scrubber rule</p>
            <p className="text-2xl font-semibold">{s ? `${s.covered_lines_pct}%` : "—"}</p>
          </div>
          <div className="gh-card p-4">
            <p className="gh-eyebrow">Codes with no rule</p>
            <p className="text-2xl font-semibold">{s ? `${s.gap_codes} / ${s.codes}` : "—"}</p>
          </div>
        </div>
      )}

      {monthly?.totals?.length > 1 && (
        <section className="gh-card p-4">
          <p className="gh-eyebrow mb-2">Denied lines per RA month</p>
          <Suspense fallback={<div className="h-[180px]" />}>
            <TrendChart data={monthly.totals} />
          </Suspense>
        </section>
      )}

      <section className="gh-card overflow-x-auto p-4">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <p className="gh-eyebrow">
            Top denial codes by payer · RA {monthLabel(month)} · {STAGE_LABEL[stage].toLowerCase()}
            {isPartial ? " · compared with the same days of last month" : " · compared with the previous month"}
          </p>
          <label className="flex items-center gap-2 text-xs text-slate-600">
            <input type="checkbox" checked={attnFirst} onChange={e => setAttnFirst(e.target.checked)} />
            Above-usual first {attnCount > 0 && <span className="gh-chip gh-chip-denied">{attnCount} above usual</span>}
          </label>
        </div>
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs uppercase text-slate-500">
              <th className="py-2 pr-3">Payer</th><th className="pr-3">Code</th><th className="pr-3">Description</th>
              <th className="pr-3 text-right">Lines</th><th className="pr-3 text-right">Claims</th>
              <th className="pr-3 text-right">Denied AED</th><th className="pr-3">{isPartial ? "vs same days last month" : "vs prev month"}</th><th className="pr-3" title="Against the average of the 3 full months before (pro-rated for a part month)">vs usual</th><th>Scrubber</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(r => (
              <tr key={`${r.payer}-${r.denial_code}`} className={`border-t border-slate-100 ${r.attention ? "bg-denied/5" : ""}`}>
                <td className="max-w-[170px] truncate py-1.5 pr-3" title={`${r.payer_name || ""} ${r.payer}`}>{r.payer}{r.payer_name && <span className="block truncate text-xs text-slate-400">{r.payer_name}</span>}</td>
                <td className="whitespace-nowrap pr-3"><span className="font-mono">{r.denial_code}</span> <WhyBtn onClick={() => setDrill({ code: r.denial_code, payer: r.payer, month })} /></td>
                <td className="min-w-[220px] max-w-[340px] py-1.5 pr-3 leading-snug text-slate-600">{r.description || "—"}</td>
                <td className="pr-3 text-right">{fmt(r.lines)}</td>
                <td className="pr-3 text-right">{fmt(r.claims)}</td>
                <td className="pr-3 text-right">{fmt(r.amount)}</td>
                <td className="whitespace-nowrap pr-3"><Trend pct={r.change_pct} diff={r.change_lines} /></td>
                <td className="whitespace-nowrap pr-3">{r.attention
                  ? <span className="gh-chip gh-chip-denied" title={`Usual ≈ ${fmt(r.usual_lines)} lines`}>▲ {r.vs_usual_pct == null ? "new" : `${r.vs_usual_pct > 0 ? "+" : ""}${r.vs_usual_pct}%`}</span>
                  : <span className="text-xs text-slate-500" title={r.usual_lines ? `Usual ≈ ${fmt(r.usual_lines)} lines` : ""}>{r.vs_usual_pct == null ? "—" : `${r.vs_usual_pct > 0 ? "+" : ""}${r.vs_usual_pct}%`}</span>}</td>
                <td>{r.has_rule ? <span className="gh-chip gh-chip-paid">rule</span> : <span className="gh-chip gh-chip-denied">gap</span>}</td>
              </tr>
            ))}
            {!rows.length && !loading && <tr><td colSpan={9} className="py-4 text-center text-slate-500">No denials in this month.</td></tr>}
          </tbody>
        </table>
      </section>

      <section className="gh-card overflow-x-auto p-4">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <p className="gh-eyebrow">
            Rule coverage · {months} months · trend {coverage?.trend_months?.filter(Boolean).map(monthLabel).join(" → ")}
          </p>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={gapsOnly} onChange={e => setGapsOnly(e.target.checked)} /> Rule gaps only
          </label>
        </div>
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs uppercase text-slate-500">
              <th className="py-2 pr-3">Code</th><th className="pr-3">Description</th>
              <th className="pr-3 text-right">Lines</th><th className="pr-3 text-right">Share</th>
              <th className="pr-3 text-right">Denied AED</th><th className="pr-3">Trend</th>
              <th className="pr-3">Top payers</th><th className="pr-3">Coverage</th><th>Rules / playbook</th>
            </tr>
          </thead>
          <tbody>
            {codes.map(c => (
              <tr key={c.denial_code} className="border-t border-slate-100 align-top">
                <td className="whitespace-nowrap py-1.5 pr-3"><span className="font-mono">{c.denial_code}</span> <WhyBtn onClick={() => setDrill({ code: c.denial_code, payer, month: "" })} /></td>
                <td className="min-w-[200px] max-w-[320px] py-1.5 pr-3 leading-snug text-slate-600">{c.description || "—"}</td>
                <td className="pr-3 text-right">{fmt(c.lines)}</td>
                <td className="pr-3 text-right">{c.share_pct}%</td>
                <td className="pr-3 text-right">{fmt(c.amount)}</td>
                <td className="pr-3"><Trend pct={c.change_pct} /></td>
                <td className="pr-3 text-xs">{c.top_payers.map(p => `${p.name || p.payer} (${fmt(p.lines)})`).join(", ")}</td>
                <td className="pr-3"><Coverage c={c.coverage} /></td>
                <td className="text-xs">
                  {c.rules.map((r, i) => <div key={i}><b>{r.source}:</b> {r.rule}</div>)}
                  {c.playbook && <div className="text-slate-500">Playbook: {c.playbook.category} ({c.playbook.owner})</div>}
                  {!c.rules.length && !c.playbook && "—"}
                </td>
              </tr>
            ))}
            {!codes.length && !loading && <tr><td colSpan={9} className="py-4 text-center text-slate-500">Nothing to show.</td></tr>}
          </tbody>
        </table>
      </section>
      </>)}
      {drill && <DenialDrill code={drill.code} payer={drill.payer} facilityId={facilityId} month={drill.month} stage={stage} onClose={() => setDrill(null)} />}
    </div>
  );
}
