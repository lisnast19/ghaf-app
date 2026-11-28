/**
 * ResubWorklist.jsx — Daily Resubmission Worklist
 *
 * Built from each night's RA denials. The Ghaf resubmission team works the
 * Action List; items that need facility input (doctor justification,
 * approvals, documents, eligibility) are routed to the facility, who reply here.
 *
 * Tabs: New Today · Action List · Needs Facility · Closed · Playbook · Bulk resubmission
 */
import { useEffect, useMemo, useState } from "react";
import apiClient from "../api/client";
import BulkResub from "./BulkResub";
import Ra21Resub from "./Ra21Resub";
import { useAuth } from "../context/AuthContext";
import { readFacility, saveFacility } from "../utils/facility";
import useFacilityDefault from "../hooks/useFacilityDefault";

const PRIORITY_STYLE = {
  P1: "bg-orange-100 text-orange-800 font-bold",
  P2: "bg-amber-100 text-amber-800 font-semibold",
  P3: "bg-slate-100 text-slate-600",
};
const VERDICT_STYLE = {
  close: "bg-slate-200 text-slate-700",
  recode: "bg-violet-100 text-violet-800",
  resubmit: "bg-emerald-100 text-emerald-800",
  check: "bg-amber-100 text-amber-800",
};
const OWNER_STYLE = {
  Ghaf: "bg-teal-100 text-teal-800",
  Facility: "bg-indigo-100 text-indigo-800",
};

function fmt(n) {
  if (n == null) return "—";
  return Number(n).toLocaleString("en-AE", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fmtDate(s) {
  if (!s) return "—";
  const [y, m, d] = s.slice(0, 10).split("-");
  return `${d}/${m}/${y}`;
}

function DaysChip({ d, bucket, route }) {
  if (route === "Reconciliation") return <span className="gh-chip gh-chip-brand" title="SN 03/2019 3.10.4: no 21-day limit for reconciliation; the claim is valid 24 months from submission">No 21-day limit</span>;
  if (bucket === "expired" || d < 0) return <span className="gh-chip gh-chip-neutral">Expired</span>;
  if (d <= 5) return <span className="gh-chip gh-chip-pending font-bold">{d}d left</span>;
  return <span className="gh-chip gh-chip-paid">{d}d left</span>;
}

function ReconSummary({ rows, onPick }) {
  const groups = useMemo(() => {
    const m = new Map();
    for (const x of rows) {
      const k = `${x.receiver || "—"}|${x.denial_code}`;
      const g = m.get(k) || { receiver: x.receiver || "—", code: x.denial_code, claims: new Set(), lines: 0, denied: 0, status: {} };
      g.claims.add(x.claim_id); g.lines += 1; g.denied += x.denied;
      g.status[x.status] = (g.status[x.status] || 0) + 1;
      m.set(k, g);
    }
    return [...m.values()].sort((a, b) => b.denied - a.denied);
  }, [rows]);
  const [all, setAll] = useState(false);
  if (!rows.length) return null;
  const tot = rows.reduce((a, x) => a + x.denied, 0);
  const st = s => ["Sent to facility (reconciliation)", "Facility discussing with payer"].includes(s) ? "gh-chip-pending" : s === "New" ? "gh-chip-neutral" : "gh-chip-brand";
  return (
    <section className="gh-card p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="gh-section-title">By insurer and denial code</h2>
        <p className="text-xs text-slate-500">{rows.length.toLocaleString()} lines · AED {Math.round(tot).toLocaleString()}</p>
      </div>
      <p className="mb-2 mt-0.5 text-xs text-slate-500">
        Claims denied again after resubmission. Reconciliation (the 2nd and last resubmission) is allowed only after the
        facility and the insurer agree (SN 03/2019 3.10.1), so send these to the facility to take up with the insurer.
        No 21-day limit (3.10.4); claims stay valid 24 months from submission (3.10.5). Click a row to filter the list.
      </p>
      <div className="overflow-x-auto">
        <table className="min-w-full text-sm">
          <thead className="text-left text-xs uppercase tracking-wide text-slate-500">
            <tr><th className="py-1.5 pr-3">Receiver / insurer</th><th className="py-1.5 pr-3">Denial code</th>
              <th className="py-1.5 pr-3 text-right">Claims</th><th className="py-1.5 pr-3 text-right">Lines</th>
              <th className="py-1.5 pr-3 text-right">Denied AED</th><th className="py-1.5">Status</th></tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {groups.slice(0, all ? undefined : 12).map(g => (
              <tr key={g.receiver + g.code} className="cursor-pointer hover:bg-sage-mint" onClick={() => onPick(`${g.code}`, g.receiver)}>
                <td className="py-1.5 pr-3">{g.receiver}</td>
                <td className="py-1.5 pr-3 font-mono text-xs">{g.code}</td>
                <td className="py-1.5 pr-3 text-right tabular-nums">{g.claims.size.toLocaleString()}</td>
                <td className="py-1.5 pr-3 text-right tabular-nums">{g.lines.toLocaleString()}</td>
                <td className="py-1.5 pr-3 text-right tabular-nums">{Math.round(g.denied).toLocaleString()}</td>
                <td className="py-1.5">
                  <div className="flex flex-wrap gap-1">
                    {Object.entries(g.status).map(([k, v]) => <span key={k} className={`gh-chip ${st(k)}`}>{k.replace(" (reconciliation)", "")}: {v}</span>)}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {groups.length > 12 && (
        <button className="mt-2 text-sm text-ghaf-600 hover:underline" onClick={() => setAll(v => !v)}>
          {all ? "Show top 12" : `Show all ${groups.length}`}
        </button>
      )}
    </section>
  );
}

function CopyButton({ text }) {
  const [done, setDone] = useState(false);
  return (
    <button
      onClick={() => { navigator.clipboard.writeText(text || ""); setDone(true); setTimeout(() => setDone(false), 1500); }}
      className="gh-btn-secondary gh-btn-sm">
      {done ? "Copied" : "Copy"}
    </button>
  );
}

export default function ResubWorklist() {
  const { user } = useAuth();
  const [facilities, setFacilities] = useState([]);
  const [facilityId, setFacilityId] = useState(() => {
    const saved = readFacility();
    return user?.facility_id || (saved ? Number(saved) : 13);
  });
  const isSuperAdmin = useFacilityDefault(facilities, facilityId, setFacilityId);
  const [data, setData] = useState(null);
  const [playbook, setPlaybook] = useState([]);
  const [tab, setTab] = useState("action");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [expanded, setExpanded] = useState(null);
  const [saving, setSaving] = useState(null);
  const [filters, setFilters] = useState({ q: "", owner: "", priority: "", category: "" });
  const [limit, setLimit] = useState(200);
  const [groupByClaim, setGroupByClaim] = useState(true);
  const [openClaims, setOpenClaims] = useState(new Set());
  const [raRange, setRaRange] = useState(() => {   // default: last month by RA transaction date
    const t = new Date(), iso = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    return { from: iso(new Date(t.getFullYear(), t.getMonth() - 1, 1)), to: iso(new Date(t.getFullYear(), t.getMonth(), 0)) };
  });
  const [raBusy, setRaBusy] = useState(false);

  const facilityView = data?.facility_view;
  const canEditPlaybook = ["admin", "billing_manager"].includes(user?.role);

  useEffect(() => {
    apiClient.get("/facilities").then(r => setFacilities(Array.isArray(r.data) ? r.data : [])).catch(() => {});
    load(facilityId);
    apiClient.get("/resub/playbook").then(r => setPlaybook(r.data)).catch(() => {});
  }, []);

  async function load(fid) {
    setLoading(true); setError(null); setExpanded(null);
    try {
      const r = await apiClient.get("/resub/worklist", { params: { facility_id: fid } });
      const { texts = [], text_fields = [] } = r.data;
      const items = r.data.items.map(x => {
        const y = { ...x };
        for (const f of text_fields) if (typeof y[f] === "number") y[f] = texts[y[f]];
        return y;
      });
      setData({ ...r.data, items });
      if (r.data.facility_view) setTab("facility");
    } catch (e) {
      setError(e.response?.data?.detail || e.message);
    } finally { setLoading(false); }
  }

  function changeFacility(id) {
    setFacilityId(id);
    saveFacility(id);
    load(id);
  }

  async function saveItem(item, patch) {
    setSaving(item.key);
    try {
      await apiClient.post("/resub/tracking", {
        facility_id: facilityId,
        updates: [{ claim_id: item.claim_id, activity_id: item.activity_id, ra_date: item.ra_date, ...patch }],
      });
      setData(d => ({
        ...d,
        items: d.items.map(x => x.key === item.key ? {
          ...x, ...patch,
          owner: patch.owner_override || x.owner,
          bucket: ["Resubmitted", "Not resubmitting (write-off)", "Collect from patient"].includes(patch.status) ? "closed_team" : x.bucket,
          updated_by: user?.email,
        } : x),
      }));
    } catch (e) {
      setError(e.response?.data?.detail || e.message);
    } finally { setSaving(null); }
  }

  async function downloadRaWorkbook() {
    if (!raRange.from || !raRange.to) return setError("Pick the RA date range first.");
    setRaBusy(true); setError(null);
    try {
      const r = await apiClient.get("/resub/ra-workbook", {
        params: { facility_id: facilityId, date_from: raRange.from, date_to: raRange.to }, responseType: "blob" });
      const url = URL.createObjectURL(r.data);
      const a = document.createElement("a"); a.href = url;
      const code = facilities.find(f => f.id === facilityId)?.facility_code || facilityId;
      a.download = `Resub_Workbook_${code}_${raRange.from}_${raRange.to}.xlsx`; a.click();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
    } catch (e) {
      const msg = e.response?.data instanceof Blob ? JSON.parse(await e.response.data.text()).detail : e.message;
      setError("Resubmission workbook failed: " + msg);
    } finally { setRaBusy(false); }
  }

  async function exportExcel(audience) {
    try {
      const r = await apiClient.get("/resub/export", { params: { facility_id: facilityId, audience }, responseType: "blob" });
      const url = URL.createObjectURL(r.data);
      const a = document.createElement("a"); a.href = url;
      a.download = `Resub_Worklist_${audience}_${facilityId}.xlsx`; a.click();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
    } catch (e) {
      setError("Export failed: " + (e.response?.data?.detail || e.message));
    }
  }

  const items = data?.items || [];
  const open = items.filter(x => x.bucket === "open");
  const lists = {
    new: open.filter(x => x.is_new),
    action: open,
    facility: open.filter(x => x.owner === "Facility"),
    closed: items.filter(x => !["open", "reconciliation"].includes(x.bucket)),
    reconciliation: items.filter(x => x.bucket === "reconciliation"),
  };
  const categories = useMemo(() => [...new Set(items.map(x => x.category))].sort(), [items]);

  const shown = (lists[tab] || []).filter(x =>
    (!filters.owner || x.owner === filters.owner) &&
    (!filters.priority || x.priority === filters.priority) &&
    (!filters.category || x.category === filters.category) &&
    (!filters.q || `${x.claim_id} ${x.denial_code} ${x.receiver} ${x.payer} ${x.authorization_id} ${x.clinician} ${x.member_id}`.toLowerCase().includes(filters.q.toLowerCase()))
  );

  const PRI = { P1: 0, P2: 1, P3: 2 };
  const groups = useMemo(() => {
    if (!groupByClaim) return null;
    const m = new Map();
    for (const x of shown) {
      const k = `${x.claim_id}|${x.ra_date}`;
      if (!m.has(k)) m.set(k, []);
      m.get(k).push(x);
    }
    return [...m.entries()].map(([k, lines]) => ({
      key: k, lines,
      priority: lines.reduce((p, x) => (x.priority && (!p || PRI[x.priority] < PRI[p]) ? x.priority : p), null),
      days_left: Math.min(...lines.map(x => x.days_left)),
      denied: lines.reduce((a, x) => a + x.denied, 0),
      expected: lines.reduce((a, x) => a + x.expected_recovery, 0),
    })).sort((a, b) => (PRI[a.priority] ?? 3) - (PRI[b.priority] ?? 3) || a.days_left - b.days_left || b.expected - a.expected);
  }, [shown, groupByClaim]);

  function toggleClaim(k) {
    setOpenClaims(prev => { const n = new Set(prev); n.has(k) ? n.delete(k) : n.add(k); return n; });
  }

  const s = data?.summary;
  const tabs = facilityView
    ? [{ key: "facility", label: `Needs Your Input (${lists.facility.length})` },
       { key: "reconciliation", label: `Reconciliation (${lists.reconciliation.length})` }]
    : [
        { key: "ra21", label: "RA last 21 days" },
        { key: "new", label: `New Today (${lists.new.length})` },
        { key: "action", label: `Action List (${lists.action.length})` },
        { key: "facility", label: `Needs Facility (${lists.facility.length})` },
        { key: "reconciliation", label: `Reconciliation (${lists.reconciliation.length})` },
        { key: "closed", label: `Closed (${lists.closed.length})` },
        { key: "playbook", label: `Playbook (${playbook.length})` },
        { key: "bulk", label: "Bulk resubmission" },
      ];

  return (
    <div className="space-y-6">
      <div>
        <p className="gh-eyebrow">Resubmission</p>
        <h1 className="gh-h1">Resubmission Worklist</h1>
        <p className="text-sm text-slate-500">
          {facilityView
            ? "Claims where the Ghaf team needs your input to resubmit before the payer deadline."
            : `Every denied activity from the RAs, with what to do and who does it. Window ${data?.window_days ?? 21} days from RA date.`}
        </p>
      </div>

      {/* Facility + actions */}
      <div className="gh-card p-4 flex flex-wrap items-end gap-3">
        <div className="min-w-48 flex-1">
          <label className="mb-1 block text-xs font-medium text-slate-600">Facility</label>
          <select value={facilityId} disabled={facilityView}
            onChange={e => changeFacility(Number(e.target.value))}
            className="gh-input w-full py-1.5">
            {facilities.filter(f => f.facility_code !== "GHAF-001").map(f => (
              <option key={f.id} value={f.id}>{f.facility_name}</option>
            ))}
          </select>
        </div>
        <button onClick={() => load(facilityId)} disabled={loading}
          className="gh-btn gh-btn-sm">
          {loading ? "Loading…" : "Refresh"}
        </button>
        {!facilityView && (
          <button onClick={() => exportExcel("ghaf")}
            className="gh-btn-secondary gh-btn-sm">
            Excel — Ghaf team
          </button>
        )}
        <button onClick={() => exportExcel("reconciliation")}
          className="gh-btn-secondary gh-btn-sm">
          Excel — Reconciliation
        </button>
        <button onClick={() => exportExcel("facility")}
          className="gh-btn-secondary gh-btn-sm">
          Excel — Facility requests
        </button>
      </div>

      {/* RA report + resubmission remarks */}
      {!facilityView && (
        <div className="gh-card p-4">
          <p className="gh-label">Resubmission workbook</p>
          <p className="mb-3 text-xs text-slate-500">
            The Remittance Activity report for the RA dates below, every service line with a remark on how to
            resubmit it in the ERP. Includes a Consultation Corrections sheet to send to the facility for recoding.
          </p>
          <div className="flex flex-wrap items-end gap-3">
            <div>
              <label className="mb-1 block text-xs font-medium text-slate-600">RA date from</label>
              <input type="date" value={raRange.from} onChange={e => setRaRange(r => ({ ...r, from: e.target.value }))}
                className="gh-input py-1.5" />
            </div>
            <div>
              <label className="mb-1 block text-xs font-medium text-slate-600">RA date to</label>
              <input type="date" value={raRange.to} onChange={e => setRaRange(r => ({ ...r, to: e.target.value }))}
                className="gh-input py-1.5" />
            </div>
            <button onClick={downloadRaWorkbook} disabled={raBusy || !facilityId} className="gh-btn gh-btn-sm">
              {raBusy ? "Building… (up to a minute)" : "Download resubmission workbook"}
            </button>
          </div>
        </div>
      )}

      {error && <div className="rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</div>}

      {/* KPI cards */}
      {s && !facilityView && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
          {[
            { label: "New from last night", v: s.new, c: "bg-sky-50 text-sky-800" },
            { label: "Open (window running)", v: s.open, c: "bg-white text-slate-800" },
            { label: "Urgent (≤5 days)", v: s.urgent, c: s.urgent.count ? "bg-orange-50 text-orange-800" : "bg-white text-slate-400" },
            { label: "Ghaf team", v: s.ghaf, c: "bg-teal-50 text-teal-800" },
            { label: "Needs facility", v: s.facility, c: "bg-indigo-50 text-indigo-800" },
            { label: "Reconciliation", v: s.reconciliation, c: "bg-rose-50 text-rose-800" },
          ].map(k => (
            <div key={k.label} className={`rounded-lg border border-hairline p-4 ${k.c}`}>
              <p className="text-2xl font-bold">{k.v.count.toLocaleString()}</p>
              <p className="mt-0.5 text-xs font-medium opacity-80">{k.label}</p>
              <p className="mt-1 text-xs opacity-70">AED {fmt(k.v.denied)} denied · ~{fmt(k.v.expected)} expected</p>
            </div>
          ))}
        </div>
      )}
      {data && !facilityView && (
        <p className="text-xs text-slate-500">
          Expected recovery uses this facility's own history: of the amounts resubmitted to each payer for each
          denial code, how much was paid. Facility average: <strong>{data.overall_rate}%</strong>.
          {s?.expired_7d?.count > 0 && <> · {s.expired_7d.count} activities (AED {fmt(s.expired_7d.denied)}) expired in the last 7 days without resubmission.</>}
        </p>
      )}

      {/* Tabs */}
      <div className="flex flex-wrap gap-1 border-b border-slate-200">
        {tabs.map(t => (
          <button key={t.key} onClick={() => { setTab(t.key); setExpanded(null); setLimit(200); }}
            className={`border-b-2 px-4 py-2 text-sm font-medium transition-colors ${
              tab === t.key ? "border-ghaf-600 text-ghaf-700" : "border-transparent text-slate-500 hover:text-slate-700"}`}>
            {t.label}
          </button>
        ))}
      </div>

      {tab === "ra21" ? (
        <Ra21Resub facilityId={facilityId} />
      ) : tab === "bulk" ? (
        <BulkResub facilityId={facilityId} facilities={facilities.filter(f => f.facility_code !== "GHAF-001")} />
      ) : tab === "playbook" ? (
        <Playbook rows={playbook} setRows={setPlaybook} canEdit={canEditPlaybook} setError={setError} />
      ) : (
        <>
          {tab === "reconciliation" && (
            <ReconSummary rows={lists.reconciliation}
              onPick={(code, rec) => setFilters(f => ({ ...f, q: `${code}` }))} />
          )}
          {/* Filters */}
          <div className="flex flex-wrap gap-2">
            <input placeholder="Search claim, code, receiver, payer, auth ID, clinician, member…" value={filters.q}
              onChange={e => setFilters(f => ({ ...f, q: e.target.value }))}
              className="gh-input min-w-64 flex-1 py-1.5" />
            {!facilityView && (
              <select value={filters.owner} onChange={e => setFilters(f => ({ ...f, owner: e.target.value }))}
                className="gh-input px-2 py-1.5">
                <option value="">All owners</option><option>Ghaf</option><option>Facility</option>
              </select>
            )}
            <label className="flex items-center gap-2 rounded border border-sage-line px-3 py-1.5 text-sm text-ink">
              <input type="checkbox" checked={groupByClaim} onChange={e => { setGroupByClaim(e.target.checked); setOpenClaims(new Set()); }} />
              Group by claim
            </label>
            <select value={filters.priority} onChange={e => setFilters(f => ({ ...f, priority: e.target.value }))}
              className="gh-input px-2 py-1.5">
              <option value="">All priorities</option><option>P1</option><option>P2</option><option>P3</option>
            </select>
            <select value={filters.category} onChange={e => setFilters(f => ({ ...f, category: e.target.value }))}
              className="gh-input px-2 py-1.5">
              <option value="">All categories</option>
              {categories.map(c => <option key={c}>{c}</option>)}
            </select>
          </div>

          <div className="gh-card overflow-hidden">
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-slate-200 text-sm">
                <thead className="bg-slate-50">
                  <tr>
                    {["Priority", "Deadline", groupByClaim ? "Claim · denied / claimed activities" : "Claim / Activity", "Receiver / Payer", "Denial", "Denied (AED)",
                      ...(facilityView ? [] : ["Expected", "Owner"]), "Status"].map(h => (
                      <th key={h} className="whitespace-nowrap px-3 py-2 text-left font-semibold text-slate-600">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {groupByClaim
                    ? groups.slice(0, limit).map(g => (
                      <ClaimGroup key={g.key} g={g} open={openClaims.has(g.key)} onToggle={() => toggleClaim(g.key)}
                        facilityView={facilityView} statuses={data.statuses}
                        onSaveAll={status => Promise.all(g.lines.map(x => saveItem(x, { status })))}>
                        {g.lines.map(x => (
                          <Row key={x.key} x={x} nested expanded={expanded === x.key}
                            onToggle={() => setExpanded(expanded === x.key ? null : x.key)}
                            onSave={patch => saveItem(x, patch)} saving={saving === x.key}
                            statuses={data.statuses} facilityView={facilityView} />
                        ))}
                      </ClaimGroup>
                    ))
                    : shown.slice(0, limit).map(x => (
                      <Row key={x.key} x={x} expanded={expanded === x.key}
                        onToggle={() => setExpanded(expanded === x.key ? null : x.key)}
                        onSave={patch => saveItem(x, patch)} saving={saving === x.key}
                        statuses={data.statuses} facilityView={facilityView} />
                    ))}
                  {shown.length === 0 && (
                    <tr><td colSpan={9} className="px-3 py-8 text-center text-slate-600">
                      {loading ? "Loading…" : "No claims in this list. Try another tab or facility, or press Refresh after the next RA fetch."}
                    </td></tr>
                  )}
                </tbody>
              </table>
            </div>
            {(groupByClaim ? groups.length : shown.length) > limit && (
              <div className="border-t border-slate-100 p-3 text-center">
                <button onClick={() => setLimit(l => l + 200)} className="text-sm font-medium text-ghaf-700 hover:underline">
                  Show more ({(groupByClaim ? groups.length : shown.length) - limit} {groupByClaim ? "claims" : "lines"} remaining)
                </button>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}

function Row({ x, expanded, onToggle, onSave, saving, statuses, facilityView, nested }) {
  const [remarks, setRemarks] = useState(x.remarks || "");
  const [assigned, setAssigned] = useState(x.assigned_to || "");
  const [reply, setReply] = useState(x.facility_reply || "");
  const facilityStatuses = x.route === "Reconciliation"
    ? ["Facility discussing with payer", "Agreed — resubmit as Reconciliation", "Not agreed with payer (write-off)"]
    : ["Facility replied", "Collect from patient"];

  return (
    <>
      <tr className={`cursor-pointer hover:bg-slate-50 ${nested ? "bg-slate-50/60 text-[13px]" : x.is_new ? "bg-sky-50/50" : ""}`} onClick={onToggle}>
        <td className="px-3 py-2">
          {x.priority ? <span className={`rounded px-2 py-0.5 text-xs ${PRIORITY_STYLE[x.priority]}`}>{x.priority}</span> : "—"}
          {x.is_new && <span className="ml-1 rounded bg-sky-100 px-1.5 py-0.5 text-[10px] font-semibold text-sky-700">NEW</span>}
        </td>
        <td className="whitespace-nowrap px-3 py-2">
          <DaysChip d={x.days_left} bucket={x.bucket} route={x.route} />
          <div className="mt-0.5 text-xs text-slate-600">{x.route === "Reconciliation" ? `valid to ${fmtDate(x.deadline)}` : fmtDate(x.deadline)}</div>
        </td>
        <td className={`px-3 py-2 ${nested ? "pl-8" : ""}`}>
          {nested
            ? <div className="font-medium text-slate-700">↳ {x.activity_code} <span className="text-xs font-normal text-slate-600">· activity {x.activity_id}</span></div>
            : <>
                <div className="font-medium text-slate-800">{x.claim_id}</div>
                <div className="text-xs text-slate-600">{x.activity_code} · {x.activity_id}</div>
              </>}
        </td>
        <td className="max-w-[220px] px-3 py-2 text-xs">
          <div className="truncate text-slate-700" title={x.receiver}>{x.receiver || "—"}</div>
          <div className="truncate text-slate-600" title={x.payer}>{x.payer || "—"}</div>
        </td>
        <td className="max-w-xs px-3 py-2">
          <div className="font-mono text-xs font-semibold text-slate-700">{x.denial_code}</div>
          <div className="truncate text-xs text-slate-500" title={x.denial_reason}>{x.category}{x.route === "Reconciliation" ? " · Reconciliation" : ""}</div>
          {x.consult && (
            <span className={`mt-1 inline-block rounded px-1.5 py-0.5 text-[11px] font-medium ${VERDICT_STYLE[x.consult.consult_action]}`}>
              {x.consult.verdict}
            </span>
          )}
        </td>
        <td className="px-3 py-2 text-right tabular-nums">{fmt(x.denied)}</td>
        {!facilityView && (
          <>
            <td className="px-3 py-2 text-right text-xs tabular-nums text-slate-500">
              {fmt(x.expected_recovery)}<div className="text-slate-600">{x.recovery_pct}%</div>
            </td>
            <td className="px-3 py-2">
              <span className={`rounded px-2 py-0.5 text-xs font-medium ${OWNER_STYLE[x.owner]}`}>{x.owner}</span>
              {x.owner === "Facility" && x.facility_role && <div className="mt-0.5 text-xs text-slate-600">{x.facility_role}</div>}
            </td>
          </>
        )}
        <td className="px-3 py-2 text-xs" onClick={e => e.stopPropagation()}>
          <select value={x.status} disabled={saving || x.bucket === "closed_resubmitted"}
            onChange={e => onSave({ status: e.target.value })}
            className="gh-input px-1.5 py-1 text-xs">
            {(facilityView ? [x.status, ...facilityStatuses.filter(s => s !== x.status)] : statuses).map(s => <option key={s}>{s}</option>)}
          </select>
          {x.updated_by && <div className="mt-0.5 text-[10px] text-slate-600">{x.updated_by}</div>}
        </td>
      </tr>
      {expanded && (
        <tr className="bg-slate-50/70">
          <td colSpan={9} className="px-4 py-4">
            <div className="grid gap-4 text-sm lg:grid-cols-2">
              <div className="space-y-2">
                <Field label="Denial reason (from payer)">{x.denial_reason || "—"}</Field>
                <div className="gh-card grid grid-cols-2 gap-x-4 gap-y-2 p-3 sm:grid-cols-3">
                  <Field label="Receiver (TPA)">{x.receiver || "—"}</Field>
                  <Field label="Payer (insurer)">{x.payer || "—"}</Field>
                  <Field label="Member ID"><span className="font-mono">{x.member_id || "—"}</span></Field>
                  <Field label="Auth ID">
                    {x.authorization_id
                      ? <span className="flex items-center gap-2 font-mono">{x.authorization_id} <CopyButton text={x.authorization_id} /></span>
                      : <span className="text-slate-600">none on claim</span>}
                  </Field>
                  <Field label="Treatment / RA">{fmtDate(x.treatment_date)} · {fmtDate(x.ra_date)}</Field>
                  <Field label="Clinician">{x.clinician || "—"}</Field>
                </div>
                {x.consult && !facilityView && (
                  <div className={`rounded-lg p-3 ${VERDICT_STYLE[x.consult.consult_action]}`}>
                    <div className="text-xs font-semibold uppercase tracking-wide">Visit history check — {x.consult.verdict}</div>
                    <div className="mt-1 text-sm">
                      {x.consult.evidence.prev_claim
                        ? <>Previous consultation <span className="font-mono">{x.consult.evidence.prev_claim}</span> on {fmtDate(x.consult.evidence.prev_date)} (code {x.consult.evidence.prev_code}, {x.consult.evidence.prev_dx}) — {x.consult.evidence.gap_days} days before this visit ({x.consult.evidence.dx}).</>
                        : <>No consultation for this patient at the facility in the previous 28 days (this visit: {x.consult.evidence.dx}).</>}
                    </div>
                    {x.consult.recode && (
                      <div className="mt-1 text-sm font-semibold">
                        Recode {x.activity_code} → {x.consult.recode} · net AED {x.consult.repeat_rate}
                        <span className="ml-1 font-normal opacity-80">({x.consult.rate_basis})</span>
                      </div>
                    )}
                  </div>
                )}
                {x.recon_recoverability && (
                  <div className={`rounded-lg p-3 ${x.recon_recoverability === "Low" ? "bg-red-50 text-red-900" : x.recon_recoverability === "High" ? "bg-emerald-50 text-emerald-900" : "bg-amber-50 text-amber-900"}`}>
                    <div className="text-xs font-semibold uppercase tracking-wide">
                      Reconciliation review — {x.recon_recoverability === "Low" ? "less recoverable" : `${x.recon_recoverability.toLowerCase()} recoverability`} · {x.recon_outcome}
                    </div>
                    <div className="mt-1 text-sm">{x.recon_why}</div>
                    {x.category_original && (
                      <div className="mt-1 text-xs">Category corrected: {x.category_original} → <b>{x.category}</b> ({x.category_note})</div>
                    )}
                    {!facilityView && (
                      <div className="mt-1 text-xs">
                        Scrub: {x.scrubbed ? (x.scrub_rules?.length ? x.scrub_rules.join(", ") : "clear") : "not scrubbed"}
                      </div>
                    )}
                  </div>
                )}
                {x.recon_remark && <Field label={<span className="flex items-center gap-2">Suggested remark for the facility to use with the payer <CopyButton text={x.recon_remark} /></span>}><span className="italic">{x.recon_remark}</span></Field>}
                {!facilityView && <Field label="Action (Ghaf team)">{x.action}</Field>}
                {x.facility_request && <Field label={`Needed from facility${x.facility_role ? ` — ${x.facility_role}` : ""}`}>{x.facility_request}</Field>}
                <Field label="Documents to attach">{x.documents || "—"}</Field>
                {!facilityView && x.draft_comment && x.bucket !== "reconciliation" && (
                  <Field label={<span className="flex items-center gap-2">Draft resubmission comment <CopyButton text={x.draft_comment} /></span>}>
                    <span className="italic">{x.draft_comment}</span>
                  </Field>
                )}
              </div>
              <div className="space-y-3">
                {!facilityView && (
                  <>
                    <div className="flex flex-wrap gap-2">
                      <label className="text-xs font-medium text-slate-600">Route to
                        <select value={x.owner} disabled={saving}
                          onChange={e => onSave({ owner_override: e.target.value })}
                          className="gh-input ml-2 px-1.5 py-1 text-xs">
                          <option>Ghaf</option><option>Facility</option>
                        </select>
                      </label>
                    </div>
                    <TextBox label="Assigned to" value={assigned} onChange={setAssigned}
                      onSave={() => onSave({ assigned_to: assigned })} saving={saving} single />
                    <TextBox label="Ghaf remarks" value={remarks} onChange={setRemarks}
                      onSave={() => onSave({ remarks })} saving={saving} />
                  </>
                )}
                <TextBox label={x.route === "Reconciliation" ? "Facility outcome with payer (agreed / not agreed, date)" : "Facility reply"} value={reply} onChange={setReply} saving={saving}
                  onSave={() => onSave(facilityView ? { facility_reply: reply, status: "Facility replied" } : { facility_reply: reply })} />
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

function ClaimGroup({ g, open, onToggle, onSaveAll, facilityView, statuses, children }) {
  const x = g.lines[0];
  const n = g.lines.length, total = x.claim_activities || n;
  const owners = [...new Set(g.lines.map(l => l.owner))];
  const codes = [...new Set(g.lines.map(l => l.denial_code))];
  const cats = [...new Set(g.lines.map(l => l.category))];
  const verdicts = [...new Set(g.lines.filter(l => l.consult).map(l => l.consult.verdict))];
  const allStatus = [...new Set(g.lines.map(l => l.status))];
  return (
    <>
      <tr className={`cursor-pointer border-t-2 border-slate-200 hover:bg-slate-50 ${g.lines.some(l => l.is_new) ? "bg-sky-50/40" : ""}`} onClick={onToggle}>
        <td className="px-3 py-2">
          {g.priority ? <span className={`rounded px-2 py-0.5 text-xs ${PRIORITY_STYLE[g.priority]}`}>{g.priority}</span> : "—"}
          {g.lines.some(l => l.is_new) && <span className="ml-1 rounded bg-sky-100 px-1.5 py-0.5 text-[10px] font-semibold text-sky-700">NEW</span>}
        </td>
        <td className="whitespace-nowrap px-3 py-2">
          <DaysChip d={g.days_left} bucket={x.bucket} route={x.route} />
          <div className="mt-0.5 text-xs text-slate-600">{x.route === "Reconciliation" ? `valid to ${fmtDate(x.deadline)}` : fmtDate(x.deadline)}</div>
        </td>
        <td className="px-3 py-2">
          <div className="flex items-center gap-2 font-semibold text-slate-800">
            <span className="text-slate-600">{open ? "▾" : "▸"}</span>{x.claim_id}
          </div>
          <div className="mt-0.5 text-xs">
            <span className="rounded bg-rose-50 px-1.5 py-0.5 font-semibold text-rose-700">{n} of {total} activities denied</span>
            <span className="ml-2 text-slate-600">claim net AED {fmt(x.claim_net)}</span>
          </div>
        </td>
        <td className="max-w-[220px] px-3 py-2 text-xs">
          <div className="truncate text-slate-700" title={x.receiver}>{x.receiver || "—"}</div>
          <div className="truncate text-slate-600" title={x.payer}>{x.payer || "—"}</div>
        </td>
        <td className="max-w-xs px-3 py-2">
          <div className="font-mono text-xs font-semibold text-slate-700">{codes.slice(0, 4).join(", ")}{codes.length > 4 ? " …" : ""}</div>
          <div className="truncate text-xs text-slate-500">{cats.join(" · ")}</div>
          {verdicts.map(v => <div key={v} className="mt-0.5 text-[11px] text-violet-700">{v}</div>)}
        </td>
        <td className="px-3 py-2 text-right font-semibold tabular-nums">{fmt(g.denied)}</td>
        {!facilityView && (
          <>
            <td className="px-3 py-2 text-right text-xs tabular-nums text-slate-500">{fmt(g.expected)}</td>
            <td className="px-3 py-2">
              {owners.map(o => <span key={o} className={`mr-1 rounded px-2 py-0.5 text-xs font-medium ${OWNER_STYLE[o]}`}>{o}</span>)}
            </td>
          </>
        )}
        <td className="px-3 py-2 text-xs" onClick={e => e.stopPropagation()}>
          {allStatus.length === 1 ? <span className="text-slate-600">{allStatus[0]}</span> : <span className="text-slate-500">mixed</span>}
          {!facilityView && (
            <select value="" onChange={e => e.target.value && onSaveAll(e.target.value)}
              className="gh-input mt-1 block px-1.5 py-1 text-xs">
              <option value="">Set all lines…</option>
              {statuses.map(st => <option key={st}>{st}</option>)}
            </select>
          )}
        </td>
      </tr>
      {open && children}
    </>
  );
}

function Field({ label, children }) {
  return (
    <div>
      <div className="gh-label">{label}</div>
      <div className="mt-0.5 text-slate-700">{children}</div>
    </div>
  );
}

function TextBox({ label, value, onChange, onSave, saving, single }) {
  return (
    <div>
      <div className="gh-label mb-1">{label}</div>
      <div className="flex gap-2">
        {single
          ? <input value={value} onChange={e => onChange(e.target.value)} className="gh-input flex-1 px-2 py-1" />
          : <textarea value={value} onChange={e => onChange(e.target.value)} rows={2} className="gh-input flex-1 px-2 py-1" />}
        <button onClick={onSave} disabled={saving}
          className="gh-btn gh-btn-sm self-start">
          {saving ? "…" : "Save"}
        </button>
      </div>
    </div>
  );
}

function Playbook({ rows, setRows, canEdit, setError }) {
  const [editing, setEditing] = useState(null);
  const [draft, setDraft] = useState({});
  const FIELDS = [
    ["pattern", "Denial code pattern", true], ["activity_pattern", "Activity code pattern (optional)", true],
    ["reason_pattern", "Denial reason contains (optional)", true], ["category", "Category", true], ["owner", "Owner", true],
    ["facility_role", "Facility role", true], ["action", "Action (Ghaf team)"],
    ["facility_request", "What we ask the facility"], ["documents", "Documents to attach"],
    ["draft_comment", "Draft resubmission comment"],
  ];

  async function save(id) {
    try {
      const r = await apiClient.put(`/resub/playbook/${id}`, draft);
      setRows(rs => rs.map(x => x.id === id ? r.data : x));
      setEditing(null);
    } catch (e) { setError(e.response?.data?.detail || e.message); }
  }

  return (
    <div className="space-y-3">
      <div className="rounded-xl border border-teal-200 bg-teal-50 px-4 py-3 text-xs text-teal-900">
        Rules are checked top to bottom; the first rule whose denial code (and, if set, activity code and denial reason) all match is used.
        <strong> Owner = Facility</strong> sends the item to the facility with the request text below.
        {!canEdit && " Only admins and billing managers can edit."}
      </div>
      {rows.map(p => (
        <div key={p.id} className="gh-card p-4">
          {editing === p.id ? (
            <div className="grid gap-3 md:grid-cols-2">
              {FIELDS.map(([k, label, short]) => (
                <label key={k} className={`text-xs font-medium text-slate-600 ${short ? "" : "md:col-span-2"}`}>
                  {label}
                  {k === "owner" ? (
                    <select value={draft.owner ?? p.owner} onChange={e => setDraft(d => ({ ...d, owner: e.target.value }))}
                      className="gh-input mt-1 block w-full px-2 py-1">
                      <option>Ghaf</option><option>Facility</option>
                    </select>
                  ) : short ? (
                    <input value={draft[k] ?? p[k] ?? ""} onChange={e => setDraft(d => ({ ...d, [k]: e.target.value }))}
                      className="gh-input mt-1 block w-full px-2 py-1" />
                  ) : (
                    <textarea rows={2} value={draft[k] ?? p[k] ?? ""} onChange={e => setDraft(d => ({ ...d, [k]: e.target.value }))}
                      className="gh-input mt-1 block w-full px-2 py-1" />
                  )}
                </label>
              ))}
              <div className="flex gap-2 md:col-span-2">
                <button onClick={() => save(p.id)} className="gh-btn gh-btn-sm">Save</button>
                <button onClick={() => setEditing(null)} className="gh-btn-secondary gh-btn-sm">Cancel</button>
              </div>
            </div>
          ) : (
            <div className="grid gap-2 text-sm md:grid-cols-[220px_1fr]">
              <div>
                <div className="font-mono text-xs font-semibold text-slate-700">{p.pattern === ".*" && (p.activity_pattern || p.reason_pattern) ? "any code" : p.pattern}</div>
                {p.activity_pattern && <div className="mt-0.5 text-[11px] text-slate-500">activity: <span className="font-mono">{p.activity_pattern}</span></div>}
                {p.reason_pattern && <div className="mt-0.5 text-[11px] text-slate-500">reason: <span className="font-mono">{p.reason_pattern}</span></div>}
                <div className="mt-1 font-medium text-slate-800">{p.category}</div>
                <span className={`mt-1 inline-block rounded px-2 py-0.5 text-xs font-medium ${OWNER_STYLE[p.owner]}`}>
                  {p.owner}{p.facility_role ? ` · ${p.facility_role}` : ""}
                </span>
                {canEdit && (
                  <button onClick={() => { setEditing(p.id); setDraft({}); }}
                    className="mt-2 block text-xs font-medium text-ghaf-700 hover:underline">Edit</button>
                )}
              </div>
              <div className="space-y-1.5">
                <Field label="Action (Ghaf team)">{p.action}</Field>
                {p.facility_request && <Field label="What we ask the facility">{p.facility_request}</Field>}
                <Field label="Documents">{p.documents || "—"}</Field>
                <Field label="Draft comment"><span className="italic">{p.draft_comment}</span></Field>
                {p.updated_by && <div className="text-[10px] text-slate-600">Last edited by {p.updated_by}</div>}
              </div>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
