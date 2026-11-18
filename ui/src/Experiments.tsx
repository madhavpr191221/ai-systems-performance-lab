import { useEffect, useMemo, useState, type FormEvent } from "react";

type Summary = {
  condition: string; repetition: number; offered: number; success: number; rejected: number;
  failed: number; timed_out: number; unfinished: number; rejection_fraction: number;
  successful_goodput_rps: number; deadline_compliance_among_offered: number;
  successful_end_to_end_ms: { p50: number | null; p95: number | null; p99: number | null };
  successful_stage_ms: { queue_wait: { p95: number | null } };
};
type Run = {
  run_id: string; source: string; recorded_source?: string | null; aws_calls: boolean | null; bedrock_calls: boolean | null;
  created_utc: string | null; method: string; request?: Record<string, unknown> | null;
  conditions: Summary[]; paired_statistics?: Record<string, {
    mean_paired_difference: number | null; bootstrap_95_percent_interval: number[] | null;
    paired_repetitions: number;
  }> | null;
};
type Detail = { run_id: string; manifest: Record<string, any>; summary: { conditions: Summary[]; paired_statistics?: Run["paired_statistics"] } | Summary[]; occupancy: Record<string, string>[]; requests: Record<string, string>[] };
type Job = { job_id: string; status: string; completed_runs: number; total_runs: number; current?: string | null; run_id?: string | null; error?: string | null };

const initial = { factor: "queue_capacity", baseline_value: "0", candidate_value: "16", arrival: "poisson", rate_rps: "20", concurrency: "2", queue_capacity: "4", duration_s: "5", repetitions: "10", seed: "20261118", deadline_ms: "500", tool_delay_ms: "20", model_delay_ms: "30" };
const factorLabels: Record<string, string> = { arrival: "Arrival process", rate: "Offered arrival rate", concurrency: "Processing concurrency", queue_capacity: "Waiting queue capacity" };

export default function Experiments({ onBack }: { onBack: () => void }) {
  const [runs, setRuns] = useState<Run[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [detail, setDetail] = useState<Detail | null>(null);
  const [form, setForm] = useState(initial);
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState("");
  const [loadingRun, setLoadingRun] = useState(false);

  async function refreshRuns(selectFirst = false) {
    try {
      const response = await fetch("/experiments/runs");
      if (!response.ok) throw new Error("Could not load saved experiment runs.");
      const data = (await response.json()) as Run[];
      setRuns(data);
      if (selectFirst && data.length) setSelectedId(data[0].run_id);
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Could not load runs."); }
  }

  useEffect(() => { void refreshRuns(true); void fetch("/experiments/jobs/current").then((r) => r.json()).then(setJob).catch(() => undefined); }, []);
  useEffect(() => {
    if (!selectedId) { setDetail(null); return; }
    let ignore = false;
    setLoadingRun(true);
    fetch(`/experiments/runs/${encodeURIComponent(selectedId)}`)
      .then((response) => { if (!response.ok) throw new Error("Could not open this run."); return response.json(); })
      .then((data: Detail) => { if (!ignore) setDetail(data); })
      .catch((caught: unknown) => { if (!ignore) setError(caught instanceof Error ? caught.message : "Could not open run."); })
      .finally(() => { if (!ignore) setLoadingRun(false); });
    return () => { ignore = true; };
  }, [selectedId]);
  useEffect(() => {
    if (!job || !["running"].includes(job.status)) return;
    let ignore = false;
    const timer = window.setInterval(async () => {
      try {
        const response = await fetch("/experiments/jobs/current");
        const next = (await response.json()) as Job | null;
        if (ignore || !next) return;
        setJob(next);
        if (next.status !== "running") {
          window.clearInterval(timer);
          await refreshRuns();
          if (next.run_id) setSelectedId(next.run_id);
        }
      } catch { /* next poll reports whether the API recovered */ }
    }, 800);
    return () => { ignore = true; window.clearInterval(timer); };
  }, [job?.status, job?.job_id]);

  const summaries = detail ? (Array.isArray(detail.summary) ? detail.summary : detail.summary.conditions) : [];
  const paired = detail && !Array.isArray(detail.summary) ? detail.summary.paired_statistics : null;
  const isRunning = job?.status === "running";
  const request = useMemo(() => {
    const value = (raw: string) => form.factor === "arrival" ? raw : Number(raw);
    return { factor: form.factor, baseline_value: value(form.baseline_value), candidate_value: value(form.candidate_value), arrival: form.arrival, rate_rps: Number(form.rate_rps), concurrency: Number(form.concurrency), queue_capacity: Number(form.queue_capacity), duration_s: Number(form.duration_s), repetitions: Number(form.repetitions), seed: Number(form.seed), deadline_ms: Number(form.deadline_ms), tool_delay_ms: Number(form.tool_delay_ms), model_delay_ms: Number(form.model_delay_ms) };
  }, [form]);

  async function startRun(event: FormEvent) {
    event.preventDefault(); setError("");
    try {
      const response = await fetch("/experiments/jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(request) });
      const body = await response.json();
      if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Experiment configuration was rejected.");
      setJob(body);
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Could not start experiment."); }
  }

  async function cancelRun() {
    const response = await fetch("/experiments/jobs/current", { method: "DELETE" });
    if (response.ok) setJob(await response.json());
  }

  return <main className="app-shell experiments-shell">
    <header className="topbar"><a className="brand" href="#lab"><span className="brand-mark">A</span><span><strong>AI Systems</strong><small>PERFORMANCE LAB</small></span></a><div className="topbar-right"><span className="backend-pill simulated"><i />Local simulated experiments</span><span className="region-label">SYSTEMS LAB</span></div></header>
    <div className="experiments-layout">
      <aside className="experiment-sidebar"><div className="rail-heading">WORKSPACE</div><button className="nav-item" onClick={onBack}><span>◉</span> Case analyst</button><button className="nav-item selected"><span>⌁</span> Performance lab</button><div className="experiment-side-note"><strong>Evidence comes from runs</strong><p>Each run saves its workload definition, request outcomes, occupancy samples, and paired analysis.</p></div><div className="scope-caption">LOCAL SIMULATION · NO AWS CALLS</div></aside>
      <section className="experiment-main">
        <div className="experiment-title"><div><div className="eyebrow">STOCHASTIC WORKLOAD ANALYSIS</div><h1>Explore the bottleneck</h1><p>Change one system factor, replay matched workloads, then inspect what changed in queueing, throughput, rejection, and tail latency.</p></div><button className="secondary-button" onClick={() => void refreshRuns()}>Refresh runs</button></div>
        <div className="experiment-grid">
          <section className="lab-card run-control"><div className="card-heading"><div><div className="eyebrow">CONTROLLED COMPARISON</div><h2>Choose a change</h2></div><span className="source-badge">SIMULATED HTTP</span></div>
            <form onSubmit={startRun}>
              <label className="lab-label">Compare one factor</label><select className="lab-input" value={form.factor} onChange={(e) => { const factor=e.target.value; setForm({ ...form, factor, baseline_value: factor === "arrival" ? "deterministic" : factor === "rate" ? "12" : factor === "concurrency" ? "1" : "0", candidate_value: factor === "arrival" ? "poisson" : factor === "rate" ? "24" : factor === "concurrency" ? "4" : "16" }); }}><option value="arrival">Arrival variability</option><option value="rate">Offered arrival rate</option><option value="concurrency">Processing concurrency</option><option value="queue_capacity">Queue capacity</option></select>
              <div className="lab-two-col"><label className="lab-label">Baseline value{form.factor === "arrival" ? <select className="lab-input" value={form.baseline_value} onChange={(e) => setForm({ ...form, baseline_value: e.target.value })}><option value="deterministic">Deterministic</option><option value="poisson">Poisson</option></select> : <input className="lab-input" type="number" min={form.factor === "rate" ? 0.1 : form.factor === "concurrency" ? 1 : 0} max={form.factor === "rate" ? 50 : form.factor === "concurrency" ? 16 : 128} step={form.factor === "rate" ? 0.1 : 1} value={form.baseline_value} onChange={(e) => setForm({ ...form, baseline_value: e.target.value })} />}</label><label className="lab-label">Candidate value{form.factor === "arrival" ? <select className="lab-input" value={form.candidate_value} onChange={(e) => setForm({ ...form, candidate_value: e.target.value })}><option value="deterministic">Deterministic</option><option value="poisson">Poisson</option></select> : <input className="lab-input" type="number" min={form.factor === "rate" ? 0.1 : form.factor === "concurrency" ? 1 : 0} max={form.factor === "rate" ? 50 : form.factor === "concurrency" ? 16 : 128} step={form.factor === "rate" ? 0.1 : 1} value={form.candidate_value} onChange={(e) => setForm({ ...form, candidate_value: e.target.value })} />}</label></div>
              <div className="lab-two-col"><label className="lab-label">Arrival process<select className="lab-input" value={form.arrival} onChange={(e) => setForm({ ...form, arrival: e.target.value })}><option value="poisson">Poisson</option><option value="deterministic">Deterministic</option></select></label><label className="lab-label">Rate (requests/s)<input className="lab-input" type="number" min="0.1" max="50" step="0.5" value={form.rate_rps} onChange={(e) => setForm({ ...form, rate_rps: e.target.value })} /></label></div>
              <div className="lab-two-col"><label className="lab-label">Concurrency<input className="lab-input" type="number" min="1" max="16" value={form.concurrency} onChange={(e) => setForm({ ...form, concurrency: e.target.value })} /></label><label className="lab-label">Queue positions<input className="lab-input" type="number" min="0" max="128" value={form.queue_capacity} onChange={(e) => setForm({ ...form, queue_capacity: e.target.value })} /></label></div>
              <div className="lab-three-col"><label className="lab-label">Seconds / run<input className="lab-input" type="number" min="1" max="30" value={form.duration_s} onChange={(e) => setForm({ ...form, duration_s: e.target.value })} /></label><label className="lab-label">Paired repetitions<input className="lab-input" type="number" min="1" max="10" value={form.repetitions} onChange={(e) => setForm({ ...form, repetitions: e.target.value })} /></label><label className="lab-label">Deadline ms<input className="lab-input" type="number" min="10" max="30000" value={form.deadline_ms} onChange={(e) => setForm({ ...form, deadline_ms: e.target.value })} /></label></div>
              <p className="form-explainer">{factorLabels[form.factor]} changes between baseline and candidate. Other system controls are held fixed. Same seed per pair. Maximum 10 repetitions and 10 minutes of offered experiment time.</p>
              <div className="run-actions"><button className="primary-button" type="submit" disabled={isRunning}>{isRunning ? "Experiment running…" : "Run paired experiment"}</button>{isRunning && <button className="secondary-button" type="button" onClick={() => void cancelRun()}>Cancel</button>}</div>
            </form>
            {job && <div className={`job-status ${job.status}`}><div><strong>{job.status === "running" ? "Experiment in progress" : `Last job ${job.status}`}</strong><span>{job.completed_runs}/{job.total_runs} runs{job.current ? ` · ${job.current}` : ""}{job.run_id ? ` · ${job.run_id}` : ""}{job.error ? ` · ${job.error}` : ""}</span></div>{job.status === "running" && <div className="progress-track"><i style={{ width: `${(job.completed_runs / Math.max(job.total_runs, 1)) * 100}%` }} /></div>}</div>}
          </section>
          <section className="lab-card saved-runs"><div className="card-heading"><div><div className="eyebrow">RECORDED EVIDENCE</div><h2>Saved experiments</h2></div><span className="tiny-label">{runs.length} RUNS</span></div>{runs.length === 0 ? <div className="empty-runs">No saved runs yet. Start a local comparison to create an experiment record.</div> : <div className="run-list">{runs.map((run) => <button className={`run-row ${run.run_id === selectedId ? "active" : ""}`} key={run.run_id} onClick={() => setSelectedId(run.run_id)}><strong>{run.run_id}</strong><span>{run.method}</span><small title={run.recorded_source ?? run.source}>{run.source} · {run.conditions.length} run summaries</small></button>)}</div>}</section>
        </div>
        {error && <div className="error-banner">{error}</div>}
        <section className="lab-card results-card"><div className="card-heading"><div><div className="eyebrow">MEASURED RESULTS</div><h2>{detail ? detail.run_id : "Select an experiment"}</h2></div><span className="source-badge">{detail?.manifest.source ?? "NO RUN SELECTED"}</span></div>
          {loadingRun ? <p className="empty-runs">Loading run artifacts…</p> : !detail ? <p className="empty-runs">Results will appear here with source labels, workload settings, uncertainty notes, and raw samples for inspection.</p> : <>
            <div className="provenance-strip"><span title={detail.manifest.source}><b>Source</b> {detail.manifest.display_source ?? detail.manifest.source ?? "Unknown"}</span><span><b>AWS calls</b> {String(detail.manifest.aws_calls ?? "not recorded")}</span><span><b>Bedrock calls</b> {String(detail.manifest.bedrock_calls ?? "not recorded")}</span><span><b>Scope</b> {detail.manifest.scope ?? "recorded local experiment"}</span></div>
            <MetricCompare summaries={summaries} />
            <OutcomeTable summaries={summaries} />
            {detail.occupancy.length > 0 && <OccupancyPlot rows={detail.occupancy} />}
            {paired && <PairedTable paired={paired} />}
            <details className="artifact-details"><summary>Request-level samples ({detail.requests.length} shown; capped by API)</summary><div className="table-scroll"><table><thead><tr>{["condition", "repetition", "outcome", "reason", "end_to_end_ms", "queue_wait_ms", "processing_ms", "within_deadline"].map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>{detail.requests.slice(0, 100).map((row, index) => <tr key={`${row.condition}-${row.repetition}-${index}`}>{["condition", "repetition", "outcome", "reason", "end_to_end_ms", "queue_wait_ms", "processing_ms", "within_deadline"].map((key) => <td key={key}>{row[key] ?? "—"}</td>)}</tr>)}</tbody></table></div></details>
            <p className="analysis-caveat">P95 values are computed from successful requests within each run. Paired uncertainty uses repetition-level differences, not requests treated as independent samples. Intervals appear only with at least 10 valid pairs and remain descriptive for this small experiment. Occupancy and latency are local simulated-agent HTTP measurements, not AWS capacity results.</p>
          </>}
        </section>
      </section>
    </div>
  </main>;
}

function OutcomeTable({ summaries }: { summaries: Summary[] }) {
  if (!summaries.length) return null;
  return <div className="paired-panel"><div className="plot-heading"><div><strong>Outcomes by repetition</strong><small>Unfinished requests remain separate from completed failures.</small></div></div><div className="table-scroll"><table><thead><tr>{["condition", "rep", "offered", "success", "rejected", "failed", "timed out", "unfinished"].map((x) => <th key={x}>{x}</th>)}</tr></thead><tbody>{summaries.map((row, index) => <tr key={`${row.condition}-${row.repetition}-${index}`}><td>{row.condition}</td><td>{row.repetition}</td><td>{row.offered}</td><td>{row.success}</td><td>{row.rejected}</td><td>{row.failed}</td><td>{row.timed_out}</td><td>{row.unfinished}</td></tr>)}</tbody></table></div></div>;
}

function MetricCompare({ summaries }: { summaries: Summary[] }) {
  const conditions = [...new Set(summaries.map((item) => item.condition))];
  const avg = (condition: string, getter: (item: Summary) => number | null) => { const values = summaries.filter((item) => item.condition === condition).map(getter).filter((x): x is number => x !== null && Number.isFinite(x)); return values.length ? values.reduce((a, b) => a + b, 0) / values.length : null; };
  const cards = [
    ["Successful goodput", (name: string) => avg(name, (x) => x.successful_goodput_rps), "req/s"],
    ["Rejected / offered", (name: string) => avg(name, (x) => x.rejection_fraction * 100), "%"],
    ["Deadline compliance", (name: string) => avg(name, (x) => x.deadline_compliance_among_offered * 100), "%"],
    ["Mean run end-to-end P95", (name: string) => avg(name, (x) => x.successful_end_to_end_ms.p95), "ms"],
    ["Mean run queue wait P95", (name: string) => avg(name, (x) => x.successful_stage_ms.queue_wait.p95), "ms"],
  ] as const;
  return <div className="comparison-metrics">{cards.map(([label, get, unit]) => <div className="comparison-metric" key={label}><span>{label}</span>{conditions.map((condition) => <div key={condition}><small>{condition}</small><strong>{get(condition) === null ? "—" : `${get(condition)!.toFixed(1)} ${unit}`}</strong></div>)}</div>)}</div>;
}

function OccupancyPlot({ rows }: { rows: Record<string, string>[] }) {
  const conditions = [...new Set(rows.map((row) => row.condition))];
  const colors = ["#65d9d0", "#e9aa68"];
  const series = conditions.map((condition) => {
    const selected = rows.filter((row) => row.condition === condition);
    const reps = [...new Set(selected.map((row) => row.repetition))];
    const bins = new Map<number, number[]>();
    selected.forEach((row) => { const bin = Math.round(Number(row.offset_s) * 4) / 4; const count = Number(row.waiting ?? 0); bins.set(bin, [...(bins.get(bin) ?? []), count]); });
    const data = [...bins.entries()].sort(([a], [b]) => a - b).map(([x, ys]) => ({ x, y: ys.reduce((a, b) => a + b, 0) / ys.length }));
    return { condition, color: colors[conditions.indexOf(condition) % colors.length], data, reps: reps.length };
  });
  const maxX = Math.max(1, ...series.flatMap((s) => s.data.map((p) => p.x)));
  const maxY = Math.max(1, ...series.flatMap((s) => s.data.map((p) => p.y)));
  const path = (data: { x: number; y: number }[]) => data.map((p, i) => `${i ? "L" : "M"}${40 + p.x / maxX * 720},${180 - p.y / maxY * 145}`).join(" ");
  return <div className="plot-panel"><div className="plot-heading"><div><strong>Waiting queue occupancy</strong><small>Mean across available repetition samples · 250 ms bins</small></div><div className="plot-legend">{series.map((s) => <span key={s.condition}><i style={{ background: s.color }} />{s.condition}</span>)}</div></div><svg className="occupancy-svg" viewBox="0 0 790 215" role="img" aria-label="Waiting queue occupancy over elapsed time"><line x1="40" x2="760" y1="180" y2="180" stroke="#34485d"/><line x1="40" x2="40" y1="30" y2="180" stroke="#34485d"/><text x="40" y="205" fill="#8192a5" fontSize="10">0 s</text><text x="725" y="205" fill="#8192a5" fontSize="10">{maxX.toFixed(1)} s</text><text x="7" y="35" fill="#8192a5" fontSize="10">{maxY}</text>{series.map((s) => <path key={s.condition} d={path(s.data)} fill="none" stroke={s.color} strokeWidth="2" />)}</svg><p className="plot-footnote">Lines pool sampled occupancy across repetitions; sparse polling can miss short-lived queue peaks.</p></div>;
}

function PairedTable({ paired }: { paired: NonNullable<Run["paired_statistics"]> }) {
  const labels: Record<string, string> = { successful_goodput_rps: "Goodput (req/s)", rejection_fraction: "Rejection fraction", deadline_compliance_among_offered: "Deadline compliance", successful_end_to_end_p95_ms: "Successful latency P95 (ms)", queue_wait_p95_ms: "Queue wait P95 (ms)" };
  return <div className="paired-panel"><div className="plot-heading"><div><strong>Paired difference summary</strong><small>Candidate minus baseline; comparisons use the matched repetition as the unit</small></div></div><div className="table-scroll"><table><thead><tr><th>Metric</th><th>Mean paired difference</th><th>95% interval</th><th>Paired runs</th></tr></thead><tbody>{Object.entries(paired).map(([key, value]) => <tr key={key}><td>{labels[key] ?? key}</td><td>{value.mean_paired_difference == null ? "—" : value.mean_paired_difference.toFixed(3)}</td><td>{value.bootstrap_95_percent_interval ? `[${value.bootstrap_95_percent_interval.map((x) => x.toFixed(3)).join(", ")}]` : "Not estimated (<10 valid pairs)"}</td><td>{value.paired_repetitions}</td></tr>)}</tbody></table></div></div>;
}
