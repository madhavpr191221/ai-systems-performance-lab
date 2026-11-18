import { useEffect, useMemo, useState, type FormEvent } from "react";

type Summary = {
  condition: string;
  repetition: number;
  offered: number;
  success: number;
  rejected: number;
  failed: number;
  timed_out: number;
  unfinished: number;
  rejection_fraction: number;
  successful_goodput_rps: number;
  deadline_compliance_among_offered: number;
  successful_end_to_end_ms: { p50: number | null; p95: number | null; p99: number | null };
  successful_stage_ms: { queue_wait: { p95: number | null } };
};
type PairedMetric = {
  mean_paired_difference: number | null;
  bootstrap_95_percent_interval: number[] | null;
  paired_repetitions: number;
};
type Run = {
  run_id: string;
  source: string;
  recorded_source?: string | null;
  aws_calls: boolean | null;
  bedrock_calls: boolean | null;
  created_utc: string | null;
  method: string;
  request?: Record<string, unknown> | null;
  conditions: Summary[];
  paired_statistics?: Record<string, PairedMetric> | null;
};
type TimePoint = {
  condition: string;
  offset_s: number;
  repetitions: number;
  offered?: number;
  admitted?: number;
  rejected?: number;
  completed?: number;
  active?: number;
  waiting?: number;
};
type Detail = {
  run_id: string;
  manifest: Record<string, any>;
  summary: { conditions: Summary[]; paired_statistics?: Record<string, PairedMetric>; sizing_study?: SizingReport } | Summary[];
  occupancy: Record<string, string>[];
  time_series?: TimePoint[];
  requests: Record<string, string>[];
  architecture_review?: Record<string, any> | null;
};
type SizingReport = { study_id: string; focus: string; scope: string; slo_profile: Record<string, number | string>; candidate_results: { configuration_id: string; concurrency: number; queue_capacity: number; arrival: string; arrival_rate_rps: number; classification: string; rare_event_warning: string[]; feasible: boolean | null; diagnostics: Record<string, number | null>; slo_metrics: Record<string, { mean: number | null; interval: number[] | null; repetition_count: number }> }[]; recommendation: { configuration_id: string | null; basis: string; cost_optimized: boolean; cost_limitation: string } };
type Job = {
  job_id: string;
  status: string;
  completed_runs: number;
  total_runs: number;
  current?: string | null;
  run_id?: string | null;
  error?: string | null;
};
type FormState = {
  factor: string;
  baseline_value: string;
  candidate_value: string;
  arrival: string;
  rate_rps: string;
  concurrency: string;
  queue_capacity: string;
  duration_s: string;
  repetitions: string;
  seed: string;
  deadline_s: string;
  tool_delay_ms: string;
  model_delay_ms: string;
};
type SizingForm = { focus: string; rate: string; rateCandidates: string; arrival: string; arrivalCandidates: string; concurrency: string; concurrencyCandidates: string; queue: string; queueCandidates: string; duration: string; repetitions: string; deadline: string; goodput: string; rejection: string; failure: string };

const initial: FormState = {
  factor: "queue_capacity",
  baseline_value: "0",
  candidate_value: "16",
  arrival: "poisson",
  rate_rps: "20",
  concurrency: "2",
  queue_capacity: "4",
  duration_s: "5",
  repetitions: "10",
  seed: "20261118",
  deadline_s: "0.5",
  tool_delay_ms: "20",
  model_delay_ms: "30",
};
const initialSizing: SizingForm = { focus: "queue_capacity", rate: "12", rateCandidates: "8, 12, 16", arrival: "poisson", arrivalCandidates: "deterministic, poisson", concurrency: "2", concurrencyCandidates: "1, 2, 4", queue: "8", queueCandidates: "0, 8, 16", duration: "2", repetitions: "10", deadline: "5", goodput: "95", rejection: "1", failure: "1" };

const factorLabels: Record<string, string> = {
  arrival: "arrival variability",
  rate: "arrival rate",
  concurrency: "processing concurrency",
  queue_capacity: "waiting queue capacity",
};
const factorTitles: Record<string, string> = {
  arrival: "Arrival variability",
  rate: "Arrival rate",
  concurrency: "Concurrency limit",
  queue_capacity: "Queue capacity",
};

const presets = [
  { factor: "queue_capacity", label: "Queue capacity", description: "How many waiting positions change deadline misses and rejection?", base: "0", candidate: "16" },
  { factor: "concurrency", label: "Processing concurrency", description: "How many simultaneous agent turns should the service process?", base: "1", candidate: "4" },
  { factor: "arrival", label: "Arrival variability", description: "Do irregular arrivals change SLO performance at the same mean rate?", base: "deterministic", candidate: "poisson" },
  { factor: "rate", label: "Offered arrival rate", description: "At what tested request rate does this configuration stop meeting SLOs?", base: "12", candidate: "24" },
];

export default function Experiments({
  onBack,
  theme,
  onToggleTheme,
}: {
  onBack: () => void;
  theme: "light" | "dark";
  onToggleTheme: () => void;
}) {
  const [runs, setRuns] = useState<Run[]>([]);
  const [runsLoaded, setRunsLoaded] = useState(false);
  const [selectedId, setSelectedId] = useState("");
  const [detail, setDetail] = useState<Detail | null>(null);
  const [form, setForm] = useState<FormState>(initial);
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState("");
  const [loadingRun, setLoadingRun] = useState(false);
  const [sizing, setSizing] = useState<SizingForm>(initialSizing);
  const [review, setReview] = useState<Record<string, any> | null>(null);
  const [reviewing, setReviewing] = useState(false);
  const [reviewError, setReviewError] = useState("");

  async function refreshRuns() {
    try {
      const response = await fetch("/experiments/runs");
      if (!response.ok) throw new Error("Could not load saved experiments.");
      const data = (await response.json()) as Run[];
      setRuns(data);
      setRunsLoaded(true);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not load experiments.");
    }
  }

  useEffect(() => {
    void fetch("/experiments/jobs/current")
      .then((response) => response.json())
      .then((current: Job | null) => setJob(current?.status === "running" ? current : null))
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    setReview(null);
    setReviewError("");
    let ignore = false;
    setLoadingRun(true);
    fetch(`/experiments/runs/${encodeURIComponent(selectedId)}`)
      .then((response) => {
        if (!response.ok) throw new Error("Could not open this experiment.");
        return response.json();
      })
      .then((data: Detail) => { if (!ignore) { setDetail(data); setReview(data.architecture_review ?? null); } })
      .catch((caught: unknown) => {
        if (!ignore) setError(caught instanceof Error ? caught.message : "Could not open experiment.");
      })
      .finally(() => { if (!ignore) setLoadingRun(false); });
    return () => { ignore = true; };
  }, [selectedId]);

  useEffect(() => {
    if (job?.status !== "running") return;
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
      } catch {
        // The completed-job refresh will expose a persistent API error.
      }
    }, 800);
    return () => { ignore = true; window.clearInterval(timer); };
  }, [job?.status, job?.job_id]);

  const summaries = detail
    ? (Array.isArray(detail.summary) ? detail.summary : detail.summary.conditions)
    : [];
  const paired = detail && !Array.isArray(detail.summary)
    ? detail.summary.paired_statistics
    : null;
  const timeSeries = detail?.time_series ?? [];
  const isRunning = job?.status === "running";
  const request = useMemo(() => {
    const value = (raw: string) => form.factor === "arrival" ? raw : Number(raw);
    return {
      factor: form.factor,
      baseline_value: value(form.baseline_value),
      candidate_value: value(form.candidate_value),
      arrival: form.arrival,
      rate_rps: Number(form.rate_rps),
      concurrency: Number(form.concurrency),
      queue_capacity: Number(form.queue_capacity),
      duration_s: Number(form.duration_s),
      repetitions: Number(form.repetitions),
      seed: Number(form.seed),
      deadline_ms: Number(form.deadline_s) * 1000,
      tool_delay_ms: Number(form.tool_delay_ms),
      model_delay_ms: Number(form.model_delay_ms),
    };
  }, [form]);

  function choosePreset(factor: string, baseline: string, candidate: string) {
    setForm((current) => ({
      ...current,
      factor,
      baseline_value: baseline,
      candidate_value: candidate,
    }));
  }

  async function startRun(event: FormEvent) {
    event.preventDefault();
    setError("");
    try {
      if (!isPositiveFinite(form.deadline_s)) throw new Error("Enter a finite deadline greater than 0 seconds.");
      const response = await fetch("/experiments/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(request),
      });
      const body = await response.json();
      if (!response.ok) {
        throw new Error(typeof body.detail === "string" ? body.detail : "Experiment configuration was rejected.");
      }
      setJob(body);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not start the experiment.");
    }
  }

  async function cancelRun() {
    const response = await fetch("/experiments/jobs/current", { method: "DELETE" });
    if (response.ok) setJob(await response.json());
  }

  async function startSizing(event: FormEvent) {
    event.preventDefault();
    setError("");
    const integerList = (raw: string) => raw.split(",").map((item) => Number(item.trim()));
    try {
      if (!isPositiveFinite(sizing.deadline)) throw new Error("Enter a finite deadline greater than 0 seconds.");
      const candidateList = (raw: string) => raw.split(",").map((item) => item.trim()).filter(Boolean);
      const payload: Record<string, unknown> = {
        focus: sizing.focus,
        arrival: sizing.arrival,
        arrival_rate_rps: Number(sizing.rate),
        concurrency: Number(sizing.concurrency),
        queue_capacity: Number(sizing.queue),
        duration_s: Number(sizing.duration),
        repetitions: Number(sizing.repetitions),
        slo: { deadline_ms: Number(sizing.deadline) * 1000, deadline_goodput_target: Number(sizing.goodput) / 100, max_rejection_fraction: Number(sizing.rejection) / 100, max_admitted_failure_fraction: Number(sizing.failure) / 100 },
      };
      if (sizing.focus === "concurrency") payload.concurrency_candidates = integerList(sizing.concurrencyCandidates);
      if (sizing.focus === "queue_capacity") payload.queue_candidates = integerList(sizing.queueCandidates);
      if (sizing.focus === "rate") payload.arrival_rate_candidates = integerList(sizing.rateCandidates);
      if (sizing.focus === "arrival") payload.arrival_candidates = candidateList(sizing.arrivalCandidates);
      const response = await fetch("/experiments/sizing-studies", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Sizing study configuration was rejected.");
      setReview(null); setSelectedId(""); setDetail(null); setJob(body);
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Could not start SLO sizing study."); }
  }

  async function requestReview() {
    if (!selectedId) return;
    setReviewing(true); setReviewError(""); setError(""); setReview(null);
    try {
      const response = await fetch(`/experiments/runs/${encodeURIComponent(selectedId)}/architecture-review`, { method: "POST" });
      const body = await response.json();
      if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Architecture review failed.");
      setReview(body);
    } catch (caught) { setReviewError(caught instanceof Error ? caught.message : "Architecture review failed."); }
    finally { setReviewing(false); }
  }

  const baselineCondition = detail?.manifest.conditions?.find((item: Record<string, unknown>) => item.name === "baseline");
  const candidateCondition = detail?.manifest.conditions?.find((item: Record<string, unknown>) => item.name === "candidate");
  const factor = String(detail?.manifest.request?.factor ?? "");
  const sizingReport = detail && !Array.isArray(detail.summary) ? detail.summary.sizing_study : undefined;

  return <main className="app-shell experiments-shell">
    <header className="topbar">
      <a className="brand" href="#lab"><span className="brand-mark">A</span><span><strong>AI Systems</strong><small>PERFORMANCE LAB</small></span></a>
      <div className="topbar-right"><span className="backend-pill simulated"><i />Local experiment</span><button className="theme-toggle" type="button" onClick={onToggleTheme} aria-pressed={theme === "dark"} aria-label={`Switch to ${theme === "light" ? "dark" : "light"} theme`}>{theme === "light" ? "Dark" : "Light"}</button></div>
    </header>
    <div className="experiments-layout">
      <aside className="experiment-sidebar">
        <div className="rail-heading">WORKSPACE</div>
        <button className="nav-item" onClick={onBack}><span>◉</span> Case analyst</button>
        <button className="nav-item selected"><span>▤</span> Performance lab</button>
        <div className="experiment-side-note"><strong>Controlled evidence</strong><p>Change one factor, hold the others fixed, and compare matched repetitions.</p></div>
        <div className="scope-caption">LOCAL SIMULATED AGENT · NO AWS CALLS</div>
      </aside>

      <section className="experiment-main">
        <div className="experiment-title">
          <div><div className="eyebrow">STOCHASTIC WORKLOAD ANALYSIS</div><h1>What happens when demand changes?</h1><p>Run a controlled comparison and inspect throughput, queueing, failures, and tail latency.</p></div>
        </div>

        <section className="lab-card design-card">
          <div className="card-heading"><div><div className="step-kicker">Step 1</div><h2>Pick a bottleneck to investigate</h2><p>Choose the system variable you want to test against your SLOs.</p></div><span className="source-badge">LOCAL · SIMULATED</span></div>
          <div className="preset-grid">{presets.map((preset) => <button key={preset.factor} type="button" className={`preset-card ${sizing.focus === preset.factor ? "selected" : ""}`} aria-pressed={sizing.focus === preset.factor} onClick={() => setSizing((current) => ({ ...current, focus: preset.factor }))}><strong>{preset.label}</strong><span>{preset.description}</span></button>)}</div>
          <p className="question-line">The selected variable will be the only setting that changes between candidates.</p>
        </section>

        <section className="lab-card run-control">
          <div className="card-heading"><div><div className="step-kicker">Step 2</div><h2 className="section-title">SLO-driven capacity study</h2><p className="section-subtitle">Find which tested settings meet the targets</p></div></div>
          <p className="form-explainer">This compares candidate settings for <strong>{factorTitles[sizing.focus]}</strong> and holds all other controls fixed. Measurements describe one local process, not AWS capacity.</p>
          <form id="slo-study-form" onSubmit={startSizing}>
            <div className="form-section-title">Workload and service limits</div>
            <div className="lab-three-col">
              {sizing.focus === "arrival" ? <NumberField label="Offered arrival rate (requests/s)" value={sizing.rate} min={1} max={20} step={1} onChange={(value) => setSizing({ ...sizing, rate: value })} /> : sizing.focus === "rate" ? <label className="lab-label">Arrival-rate candidates (requests/s)<input className="lab-input" value={sizing.rateCandidates} onChange={(event) => setSizing({ ...sizing, rateCandidates: event.target.value })} /><small>Comma-separated whole rates, e.g. 8, 12, 16</small></label> : <NumberField label="Offered arrival rate (requests/s)" value={sizing.rate} min={1} max={20} step={1} onChange={(value) => setSizing({ ...sizing, rate: value })} />}
              {sizing.focus === "arrival" ? <label className="lab-label">Arrival patterns<input className="lab-input" value={sizing.arrivalCandidates} onChange={(event) => setSizing({ ...sizing, arrivalCandidates: event.target.value })} /><small>Use both deterministic and poisson</small></label> : <label className="lab-label">Arrival pattern<select className="lab-input" value={sizing.arrival} onChange={(event) => setSizing({ ...sizing, arrival: event.target.value })}><option value="poisson">Poisson · random arrivals</option><option value="deterministic">Deterministic · regular arrivals</option></select></label>}
              {sizing.focus === "concurrency" ? <label className="lab-label">Concurrency candidates<input className="lab-input" value={sizing.concurrencyCandidates} onChange={(event) => setSizing({ ...sizing, concurrencyCandidates: event.target.value })} /><small>Comma-separated integers, e.g. 1, 2, 4</small></label> : <NumberField label="Fixed processing concurrency" value={sizing.concurrency} min={1} max={16} step={1} onChange={(value) => setSizing({ ...sizing, concurrency: value })} />}
              {sizing.focus === "queue_capacity" ? <label className="lab-label">Queue capacity candidates<input className="lab-input" value={sizing.queueCandidates} onChange={(event) => setSizing({ ...sizing, queueCandidates: event.target.value })} /><small>Waiting positions, e.g. 0, 8, 16</small></label> : <NumberField label="Fixed waiting queue positions" value={sizing.queue} min={0} max={64} step={1} onChange={(value) => setSizing({ ...sizing, queue: value })} />}
            </div>
            <div className="form-section-title">Service-level objectives</div>
            <div className="lab-three-col">
              <NumberField label="Deadline target (seconds)" value={sizing.deadline} min={0} step="any" hint="Any positive decimal, for example 4.91 or 5.237." onChange={(value) => setSizing({ ...sizing, deadline: value })} />
              <NumberField label="On-time successes required (%)" value={sizing.goodput} min={0} max={100} step={1} onChange={(value) => setSizing({ ...sizing, goodput: value })} />
              <NumberField label="Maximum rejected / offered (%)" value={sizing.rejection} min={0} max={100} step={0.1} onChange={(value) => setSizing({ ...sizing, rejection: value })} />
              <NumberField label="Maximum failures / admitted (%)" value={sizing.failure} min={0} max={100} step={0.1} onChange={(value) => setSizing({ ...sizing, failure: value })} />
              <NumberField label="Seconds per candidate run" value={sizing.duration} min={1} max={30} step={1} onChange={(value) => setSizing({ ...sizing, duration: value })} />
              <NumberField label="Repeated runs per candidate" value={sizing.repetitions} min={2} max={20} step={1} onChange={(value) => setSizing({ ...sizing, repetitions: value })} />
            </div>
            <p className="form-explainer">Uncertainty is estimated across whole repetitions. At least 10 repetitions are needed for an SLO classification. Total offered workload is capped at 10 minutes.</p>
          </form>
        </section>

        <section className="lab-card run-study-card">
          <div className="card-heading"><div><div className="step-kicker">Step 3</div><h2>Run the study</h2><p>Each candidate receives the same seeded workload for every repeated run.</p></div></div>
          <div className="run-actions"><button className="primary-button" type="submit" form="slo-study-form" disabled={isRunning}>{isRunning ? "Study running…" : "Run SLO study"}</button>{isRunning && <button className="secondary-button" type="button" onClick={() => void cancelRun()}>Cancel</button>}</div>
          {job && <div className={`job-status ${job.status}`}><div><strong>{job.status === "running" ? "Study in progress" : `Last job ${job.status}`}</strong><span>{job.completed_runs}/{job.total_runs} candidate repetitions{job.current ? ` · ${job.current}` : ""}{job.run_id ? ` · ${job.run_id}` : ""}{job.error ? ` · ${job.error}` : ""}</span></div>{job.status === "running" && <div className="progress-track"><i style={{ width: `${(job.completed_runs / Math.max(job.total_runs, 1)) * 100}%` }} /></div>}</div>}
        </section>

        {error && <div className="error-banner" role="alert">{error}</div>}

        <section className="lab-card results-card" aria-live="polite">
          <div className="card-heading"><div><div className="step-kicker">Step 4</div><h2>{detail ? "Review the measured results" : "Results appear here after a run"}</h2></div><span className="source-badge">{detail?.manifest.display_source ?? "NO RUN SELECTED"}</span></div>
          {loadingRun ? <p className="empty-runs">Loading saved experiment…</p> : !detail ? <p className="empty-runs">Choose a question above and run a comparison. Results will include their measurement source and limitations.</p> : <>
            <div className="provenance-strip"><span><b>Source</b> {detail.manifest.display_source ?? detail.manifest.source ?? "Unknown"}</span><span><b>AWS calls</b> {String(detail.manifest.aws_calls ?? "not recorded")}</span><span><b>Bedrock calls</b> {String(detail.manifest.bedrock_calls ?? "not recorded")}</span></div>
            {detail.manifest.method?.includes("SLO sizing") ? <div className="run-context"><strong>{factorTitles[sizingReport?.focus ?? ""] ?? "SLO study"} · {sizingReport?.candidate_results.length ?? 0} tested candidates</strong><span>{detail.manifest.scope ?? "Recorded local experiment"}</span></div> : <div className="run-context"><strong>{factorTitles[factor] ?? "Recorded experiment"}: {baselineCondition?.[factor] ?? "baseline"} → {candidateCondition?.[factor] ?? "candidate"}</strong><span>{detail.manifest.scope ?? "Recorded local experiment"}</span></div>}
            {!detail.manifest.method?.includes("SLO sizing") && <SetupSummary request={detail.manifest.request} conditions={detail.manifest.conditions} />}
            {!detail.manifest.method?.includes("SLO sizing") && <div className="comparison-metrics"><MetricCompare summaries={summaries} paired={paired ?? null} /></div>}
            {!Array.isArray(detail.summary) && detail.summary.sizing_study && <SizingResults report={detail.summary.sizing_study} review={review} reviewing={reviewing} reviewError={reviewError} onReview={() => void requestReview()} />}
            {timeSeries.length > 0 && <>
              <TimeSeriesChart rows={timeSeries} fields={["offered", "admitted", "completed"]} title="Requests over time" description="Cumulative sampled counts · mean across available repetitions · 250 ms bins" />
              <TimeSeriesChart rows={timeSeries} fields={["active", "waiting"]} title="Processing and queue occupancy" description="Instantaneous samples · mean across available repetitions · 250 ms bins" />
            </>}
            {timeSeries.length === 0 && <p className="empty-runs">This saved run predates the time-series samples. Its outcome and latency summaries remain available below.</p>}
            <details className="analysis-details"><summary>Statistical and run details</summary>
              {paired && <PairedTable paired={paired} />}
              <OutcomeTable summaries={summaries} />
              <details className="artifact-details"><summary>Request-level samples ({detail.requests.length} returned; API response is bounded)</summary><div className="table-scroll"><table><thead><tr>{["condition", "repetition", "outcome", "reason", "end_to_end_ms", "queue_wait_ms", "processing_ms", "within_deadline"].map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>{detail.requests.slice(0, 100).map((row, index) => <tr key={`${row.condition}-${row.repetition}-${index}`}>{["condition", "repetition", "outcome", "reason", "end_to_end_ms", "queue_wait_ms", "processing_ms", "within_deadline"].map((key) => <td key={key}>{row[key] ?? "—"}</td>)}</tr>)}</tbody></table></div></details>
              <p className="analysis-caveat">Latency percentiles are calculated from successful requests within each repetition. Paired uncertainty uses repetition-level differences, not requests treated as independent samples. Intervals are descriptive for this small experiment. Sampled occupancy can miss short-lived peaks. These are local single-process measurements, not AWS capacity results.</p>
            </details>
          </>}
        </section>

        <details className="lab-card more-experiments">
          <summary>More experiments · one-factor baseline comparison</summary>
          <p className="form-explainer">Optional diagnostic comparison outside the SLO sizing flow. It uses matched seeds and the same simulated local agent.</p>
          <form onSubmit={startRun}>
            <label className="lab-label">Choose comparison factor<select className="lab-input" value={form.factor} onChange={(event) => { const selected = presets.find((preset) => preset.factor === event.target.value); if (selected) choosePreset(selected.factor, selected.base, selected.candidate); }}>
              {presets.map((preset) => <option key={preset.factor} value={preset.factor}>{preset.label}</option>)}
            </select></label>
            <p className="question-line">{questionFor(form.factor, form.baseline_value, form.candidate_value)}</p>
            <div className="form-section-title">Change one factor</div>
            <div className="lab-two-col">
              <label className="lab-label">Baseline{valueControl(form.factor, "baseline", form.baseline_value, (value) => setForm({ ...form, baseline_value: value }))}</label>
              <label className="lab-label">Candidate{valueControl(form.factor, "candidate", form.candidate_value, (value) => setForm({ ...form, candidate_value: value }))}</label>
            </div>

            <div className="form-section-title">Keep these settings fixed</div>
            <div className="lab-two-col">
              {form.factor !== "arrival" && <label className="lab-label">Arrival pattern<select className="lab-input" value={form.arrival} onChange={(event) => setForm({ ...form, arrival: event.target.value })}><option value="poisson">Poisson · random arrivals</option><option value="deterministic">Deterministic · regular arrivals</option></select></label>}
              {form.factor !== "rate" && <NumberField label="Arrival rate (requests/s)" value={form.rate_rps} min={1} max={50} step={1} onChange={(value) => setForm({ ...form, rate_rps: value })} />}
              {form.factor !== "concurrency" && <NumberField label="Processing slots" value={form.concurrency} min={1} max={16} step={1} onChange={(value) => setForm({ ...form, concurrency: value })} />}
              {form.factor !== "queue_capacity" && <NumberField label="Waiting queue positions" value={form.queue_capacity} min={0} max={128} step={1} onChange={(value) => setForm({ ...form, queue_capacity: value })} />}
            </div>

            <div className="form-section-title">Run and deadline</div>
            <div className="lab-three-col">
              <NumberField label="Seconds per run" value={form.duration_s} min={1} max={30} step={1} onChange={(value) => setForm({ ...form, duration_s: value })} />
              <NumberField label="Matched repetitions" value={form.repetitions} min={1} max={10} step={1} onChange={(value) => setForm({ ...form, repetitions: value })} />
              <NumberField label="Deadline target (seconds)" value={form.deadline_s} min={0} step="any" hint="Enter any finite positive decimal, for example 0.125 or 4.91." onChange={(value) => setForm({ ...form, deadline_s: value })} />
            </div>
            <details className="advanced-settings"><summary>Advanced simulated service delays</summary><div className="lab-two-col"><NumberField label="Tool delay (ms)" value={form.tool_delay_ms} min={0} max={500} step={5} onChange={(value) => setForm({ ...form, tool_delay_ms: value })} /><NumberField label="Model delay (ms)" value={form.model_delay_ms} min={0} max={500} step={5} onChange={(value) => setForm({ ...form, model_delay_ms: value })} /></div></details>
            <p className="form-explainer">Only the selected factor changes. Each baseline/candidate pair shares its random seed. Maximum 10 repetitions and 10 minutes of offered workload.</p>
            <div className="run-actions"><button className="primary-button" type="submit" disabled={isRunning}>{isRunning ? "Experiment running…" : "Run comparison"}</button>{isRunning && <button className="secondary-button" type="button" onClick={() => void cancelRun()}>Cancel</button>}</div>
          </form>
          {job && <div className={`job-status ${job.status}`}><div><strong>{job.status === "running" ? "Experiment in progress" : `Last job ${job.status}`}</strong><span>{job.completed_runs}/{job.total_runs} runs{job.current ? ` · ${job.current}` : ""}{job.run_id ? ` · ${job.run_id}` : ""}{job.error ? ` · ${job.error}` : ""}</span></div>{job.status === "running" && <div className="progress-track"><i style={{ width: `${(job.completed_runs / Math.max(job.total_runs, 1)) * 100}%` }} /></div>}</div>}
        </details>

        <details className="lab-card history-card" onToggle={(event) => { if (event.currentTarget.open && !runsLoaded) void refreshRuns(); }}><summary>{runsLoaded ? `Saved experiments (${runs.length})` : "Browse saved experiments"}</summary><button className="secondary-button history-refresh" type="button" onClick={() => void refreshRuns()}>Refresh list</button>{!runsLoaded ? <p className="empty-runs">Saved runs are not loaded automatically. Open this section to browse them.</p> : runs.length === 0 ? <p className="empty-runs">No saved experiments yet.</p> : <div className="run-list">{runs.map((run) => <button className={`run-row ${run.run_id === selectedId ? "active" : ""}`} key={run.run_id} onClick={() => setSelectedId(run.run_id)}><strong>{run.run_id}</strong><span>{run.method}</span><small>{run.source} | {run.conditions.length} run summaries</small></button>)}</div>}</details>
      </section>
    </div>
  </main>;
}

function SizingResults({ report, review, reviewing, reviewError, onReview }: { report: SizingReport; review: Record<string, any> | null; reviewing: boolean; reviewError: string; onReview: () => void }) {
  const labels: Record<string, string> = { deadline_goodput_fraction: "On-time successes / offered", rejection_fraction: "Rejected / offered", admitted_failure_fraction: "Technical failures / admitted" };
  const targetFor = (key: string) => key === "deadline_goodput_fraction" ? Number(report.slo_profile.deadline_goodput_target) : key === "rejection_fraction" ? Number(report.slo_profile.max_rejection_fraction) : Number(report.slo_profile.max_admitted_failure_fraction);
  const metricValue = (candidate: SizingReport["candidate_results"][number], key: string) => {
    if (key === "mean_successful_goodput_rps") return candidate.diagnostics?.mean_successful_goodput_rps;
    if (key === "mean_of_per_repetition_successful_p95_ms") return candidate.diagnostics?.mean_of_per_repetition_successful_p95_ms;
    return candidate.slo_metrics[key]?.mean;
  };
  const metricLabel: Record<string, string> = { ...labels, mean_successful_goodput_rps: "Successful goodput", mean_of_per_repetition_successful_p95_ms: "Mean of per-run P95 latency" };
  const formatMetric = (key: string, value: number | null | undefined) => value == null ? "Not available" : key.endsWith("fraction") ? `${(value * 100).toFixed(2)}%` : key.endsWith("_ms") ? `${value.toFixed(1)} ms` : `${value.toFixed(2)} req/s`;
  return <section className="paired-panel sizing-results">
    <div className="plot-heading"><div><strong>Do any tested candidates meet every SLO?</strong><small>Classification uses intervals across repeated runs, plus rare-event checks.</small></div></div>
    <p>{report.recommendation.configuration_id ? `Candidate to investigate next: ${report.recommendation.configuration_id}. ` : "No candidate has demonstrated SLO feasibility. "}{report.recommendation.basis}</p>
    <div className="table-scroll sizing-table"><table><thead><tr><th>Candidate</th><th>Settings (c / queue / pattern / rate)</th><th>Result</th>{Object.keys(labels).map((key) => <th key={key}>{labels[key]}<br />mean [interval] · target</th>)}</tr></thead><tbody>{report.candidate_results.map((candidate) => <tr key={candidate.configuration_id}><td><strong>{candidate.configuration_id}</strong></td><td>{candidate.concurrency} / {candidate.queue_capacity} / {candidate.arrival} / {candidate.arrival_rate_rps} req/s</td><td><span className={`classification ${candidate.classification}`}>{candidate.classification.replaceAll("_", " ")}</span>{candidate.rare_event_warning?.length > 0 && <small className="table-note">{candidate.rare_event_warning.map((warning) => warning.replaceAll("_", " ")).join("; ")}</small>}</td>{Object.keys(labels).map((key) => { const metric = candidate.slo_metrics[key]; const ci = metric?.interval; const target = targetFor(key); return <td key={key}><strong>{metric?.mean == null ? "Not available" : `${(metric.mean * 100).toFixed(1)}%`}</strong><small>{ci ? `[${(ci[0] * 100).toFixed(1)}, ${(ci[1] * 100).toFixed(1)}]` : "interval unavailable"} · target {(target * 100).toFixed(1)}%</small></td>; })}</tr>)}</tbody></table></div>
    <p className="analysis-caveat">{report.scope}. This is a local process study; it does not establish AWS capacity or least-cost sizing. {report.recommendation.cost_limitation}</p>
    <section className="ai-review-section">{reviewError && <div className="review-error" role="alert">{reviewError}</div>}<div className="review-action"><div><h3>Ask AI to interpret the evidence</h3><p>One explicit request reviews aggregate study results. The AI cannot change settings or operate AWS.</p></div><button className="primary-button" type="button" onClick={onReview} disabled={reviewing}>{reviewing ? "Reviewing evidence…" : "Interpret this study"}</button></div>
      {review && <ReviewView review={review} report={report} metricLabel={metricLabel} metricValue={metricValue} formatMetric={formatMetric} />}
    </section>
  </section>;
}

function ReviewView({ review, report, metricLabel, metricValue, formatMetric }: { review: Record<string, any>; report: SizingReport; metricLabel: Record<string, string>; metricValue: (candidate: SizingReport["candidate_results"][number], key: string) => number | null | undefined; formatMetric: (key: string, value: number | null | undefined) => string }) {
  const result = review.review;
  const selected = report.candidate_results.find((candidate) => candidate.configuration_id === result?.candidate_id);
  const decisionLabels: Record<string, string> = { candidate_for_next_experiment: "Candidate for next experiment", no_candidate_meets_targets: "No candidate meets targets", more_evidence_needed: "More evidence needed" };
  return <div className="review-card">
    <div className="review-decision"><span className={`decision-badge ${result?.decision}`}>{decisionLabels[result?.decision] ?? "Review result"}</span>{selected && <strong>Candidate: {selected.configuration_id}</strong>}</div>
    <p className="review-summary">{result?.summary}</p>
    <h4>What the evidence supports</h4>
    <div className="review-findings">{result?.findings?.map((finding: any, index: number) => <article key={index}><div className="finding-heading"><strong>{finding.title}</strong><span>{finding.confidence} confidence</span></div><p>{finding.interpretation}</p><ul>{finding.evidence?.map((reference: any, refIndex: number) => { const candidate = report.candidate_results.find((item) => item.configuration_id === reference.candidate_id); const value = candidate ? metricValue(candidate, reference.metric) : null; const interval = candidate?.slo_metrics[reference.metric]?.interval; const evidenceValue = `${formatMetric(reference.metric, value)}${interval ? ` · interval [${formatMetric(reference.metric, interval[0])}, ${formatMetric(reference.metric, interval[1])}]` : ""}`; return <li key={`${reference.candidate_id}-${reference.metric}-${refIndex}`}><strong>{reference.candidate_id}</strong> · {metricLabel[reference.metric] ?? reference.metric}: <b>{evidenceValue}</b></li>; })}</ul></article>)}</div>
    <div className="next-experiment-card"><h4>Suggested next experiment</h4><p><strong>Question:</strong> {result?.next_experiment?.question}</p><p><strong>Change:</strong> {result?.next_experiment?.change}</p><p><strong>Hold fixed:</strong> {result?.next_experiment?.hold_constant?.join(" · ")}</p><p><strong>Look for:</strong> {result?.next_experiment?.success_evidence}</p></div>
    <div className="review-limitations"><h4>Limitations</h4><ul>{result?.limitations?.map((item: string, index: number) => <li key={index}>{item}</li>)}</ul></div>
    <small className="review-usage">{review.review_model} · {review.model_calls ?? 0} model calls · {review.tool_calls ?? 0} tool calls · {review.usage?.input_tokens ?? 0} input tokens · {review.usage?.output_tokens ?? 0} output tokens · outside measured agent traffic</small>
  </div>;
}

function NumberField({ label, value, min, max, step, hint, onChange }: { label: string; value: string; min: number; max?: number; step: number | "any"; hint?: string; onChange: (value: string) => void }) {
  return <label className="lab-label">{label}<input className="lab-input" type="number" min={min} max={max} step={step} value={value} onChange={(event) => onChange(event.target.value)} />{hint && <small>{hint}</small>}</label>;
}

function isPositiveFinite(value: string) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0;
}

function valueControl(factor: string, which: "baseline" | "candidate", value: string, onChange: (value: string) => void) {
  if (factor === "arrival") {
    return <select className="lab-input" value={value} onChange={(event) => onChange(event.target.value)}><option value="deterministic">Deterministic</option><option value="poisson">Poisson</option></select>;
  }
  const bounds = factor === "rate" ? { min: 1, max: 50 } : factor === "concurrency" ? { min: 1, max: 16 } : { min: 0, max: 128 };
  return <input className="lab-input" type="number" min={bounds.min} max={bounds.max} step={1} value={value} onChange={(event) => onChange(event.target.value)} aria-label={`${which} ${factorLabels[factor]}`} />;
}

function questionFor(factor: string, baseline: string, candidate: string) {
  const label = factorLabels[factor] ?? "system factor";
  if (factor === "arrival") return "How does random arrival timing change service performance compared with regular arrivals?";
  return `What changes when ${label} moves from ${baseline} to ${candidate}, with other settings held fixed?`;
}

function SetupSummary({ request, conditions }: { request?: Record<string, any>; conditions?: Record<string, any>[] }) {
  if (!request) return null;
  const base = conditions?.find((item) => item.name === "baseline");
  const candidate = conditions?.find((item) => item.name === "candidate");
  const shared = [
    ["Arrival pattern", request.factor === "arrival" ? "Deterministic vs Poisson" : request.arrival],
    ["Arrival rate", request.factor === "rate" ? `${base?.rate ?? "—"} vs ${candidate?.rate ?? "—"} req/s` : `${request.rate_rps} req/s`],
    ["Processing slots", request.factor === "concurrency" ? `${base?.concurrency ?? "—"} vs ${candidate?.concurrency ?? "—"}` : request.concurrency],
    ["Queue positions", request.factor === "queue_capacity" ? `${base?.queue_capacity ?? "—"} vs ${candidate?.queue_capacity ?? "—"}` : request.queue_capacity],
    ["Deadline", `${request.deadline_ms} ms`],
    ["Duration × repetitions", `${request.duration_s} s × ${request.repetitions}`],
  ];
  return <div className="setup-summary" aria-label="Experiment settings">{shared.map(([label, value]) => <div key={String(label)}><small>{label}</small><strong>{String(value)}</strong></div>)}</div>;
}

const metricDefinitions = [
  { key: "successful_goodput_rps", label: "Successful goodput", unit: "req/s", multiplier: 1, better: "higher" },
  { key: "rejection_fraction", label: "Rejected / offered", unit: "%", multiplier: 100, better: "lower" },
  { key: "deadline_compliance_among_offered", label: "Deadline compliance", unit: "%", multiplier: 100, better: "higher" },
  { key: "successful_end_to_end_p95_ms", label: "Mean per-run end-to-end P95", unit: "ms", multiplier: 1, better: "lower" },
  { key: "queue_wait_p95_ms", label: "Mean per-run queue-wait P95", unit: "ms", multiplier: 1, better: "lower" },
] as const;

function metricValue(row: Summary, key: string): number | null {
  if (key === "successful_goodput_rps") return row.successful_goodput_rps;
  if (key === "rejection_fraction") return row.rejection_fraction;
  if (key === "deadline_compliance_among_offered") return row.deadline_compliance_among_offered;
  if (key === "successful_end_to_end_p95_ms") return row.successful_end_to_end_ms.p95;
  return row.successful_stage_ms.queue_wait.p95;
}

function averageMetric(rows: Summary[], condition: string, key: string) {
  const values = rows.filter((row) => row.condition === condition).map((row) => metricValue(row, key)).filter((value): value is number => value !== null && Number.isFinite(value));
  return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
}

function MetricCompare({ summaries, paired }: { summaries: Summary[]; paired: Record<string, PairedMetric> | null }) {
  const differenceKeys: Record<string, string> = {
    successful_goodput_rps: "successful_goodput_rps",
    rejection_fraction: "rejection_fraction",
    deadline_compliance_among_offered: "deadline_compliance_among_offered",
    successful_end_to_end_p95_ms: "successful_end_to_end_p95_ms",
    queue_wait_p95_ms: "queue_wait_p95_ms",
  };
  return <>{metricDefinitions.map((metric) => {
    const difference = paired?.[differenceKeys[metric.key]];
    const interval = difference?.bootstrap_95_percent_interval;
    const meanDifference = difference?.mean_paired_difference;
    const base = averageMetric(summaries, "baseline", metric.key);
    const candidate = averageMetric(summaries, "candidate", metric.key);
    const format = (value: number | null) => value === null ? "—" : `${(value * metric.multiplier).toFixed(1)} ${metric.unit}`;
    return <article className="comparison-metric" key={metric.key}>
      <span>{metric.label}</span>
      <div className="metric-conditions"><small>Baseline</small><strong>{format(base)}</strong><small>Candidate</small><strong>{format(candidate)}</strong></div>
      <div className="metric-difference"><small>Candidate − baseline</small><strong>{meanDifference == null ? "Not available" : `${meanDifference * metric.multiplier > 0 ? "+" : ""}${(meanDifference * metric.multiplier).toFixed(1)} ${metric.unit}`}</strong>
        <small>{interval ? `95% descriptive interval [${(interval[0] * metric.multiplier).toFixed(1)}, ${(interval[1] * metric.multiplier).toFixed(1)}] ${metric.unit}` : `No interval · ${difference?.paired_repetitions ?? 0} paired runs`}</small>
      </div>
    </article>;
  })}</>;
}

function TimeSeriesChart({ rows, fields, title, description }: { rows: TimePoint[]; fields: ("offered" | "admitted" | "completed" | "active" | "waiting")[]; title: string; description: string }) {
  const definitions: Record<string, { label: string; dash?: string }> = {
    offered: { label: "Offered" }, admitted: { label: "Admitted", dash: "7 4" }, completed: { label: "Completed", dash: "2 3" }, active: { label: "In service" }, waiting: { label: "Waiting", dash: "7 4" },
  };
  const conditions = [...new Set(rows.map((row) => row.condition))];
  const series = conditions.flatMap((condition) => fields.map((field) => ({
    condition,
    field,
    label: `${condition} · ${definitions[field].label}`,
    color: condition === "candidate" ? "var(--series-candidate)" : "var(--series-baseline)",
    dash: definitions[field].dash,
    points: rows.filter((row) => row.condition === condition && typeof row[field] === "number").sort((a, b) => a.offset_s - b.offset_s),
  })).filter((item) => item.points.length));
  if (!series.length) return null;
  const maxX = Math.max(1, Math.ceil(Math.max(...series.flatMap((item) => item.points.map((point) => point.offset_s)))));
  const maxY = Math.max(1, Math.ceil(Math.max(...series.flatMap((item) => item.points.map((point) => point[item.field] ?? 0)))));
  const x0 = 52;
  const y0 = 222;
  const chartWidth = 730;
  const chartHeight = 178;
  const pathFor = (points: TimePoint[], field: typeof fields[number]) => points.map((point, index) => {
    const x = x0 + (point.offset_s / maxX) * chartWidth;
    const y = y0 - ((point[field] ?? 0) / maxY) * chartHeight;
    return `${index ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  return <section className="plot-panel">
    <div className="plot-heading"><div><strong>{title}</strong><small>{description}</small></div></div>
    <div className="chart-legend">{series.map((item) => <span key={item.label}><i style={{ borderTopColor: item.color, borderTopStyle: item.dash ? "dashed" : "solid" }} />{item.label}</span>)}</div>
    <div className="chart-scroll"><svg className="timeseries-svg" viewBox="0 0 800 270" role="img" aria-label={`${title} over elapsed time`}>
      {[0, 1, 2, 3, 4].map((tick) => {
        const value = Math.round(maxY * tick / 4);
        const y = y0 - (tick / 4) * chartHeight;
        return <g key={tick}><line className="chart-grid" x1={x0} x2={x0 + chartWidth} y1={y} y2={y} /><text className="chart-label" x={x0 - 8} y={y + 4} textAnchor="end">{value}</text></g>;
      })}
      <line className="chart-axis" x1={x0} x2={x0 + chartWidth} y1={y0} y2={y0} />
      {[0, 1, 2, 3, 4].map((tick) => <text className="chart-label" key={tick} x={x0 + (tick / 4) * chartWidth} y={y0 + 20} textAnchor="middle">{(maxX * tick / 4).toFixed(maxX < 4 ? 1 : 0)} s</text>)}
      {series.map((item) => <path key={item.label} d={pathFor(item.points, item.field)} fill="none" stroke={item.color} strokeWidth="2.5" strokeDasharray={item.dash} strokeLinejoin="round" strokeLinecap="round" />)}
      <text className="chart-axis-title" transform="translate(15 140) rotate(-90)">Requests</text>
    </svg></div>
    <p className="plot-footnote">Averages are calculated from available repetition samples in each time bin; sampled polling can miss short-lived peaks.</p>
  </section>;
}

function OutcomeTable({ summaries }: { summaries: Summary[] }) {
  if (!summaries.length) return null;
  return <div className="paired-panel"><div className="plot-heading"><div><strong>Outcomes by repetition</strong><small>Unfinished requests remain separate from completed failures.</small></div></div><div className="table-scroll"><table><thead><tr>{["Condition", "Rep", "Offered", "Success", "Rejected", "Failed", "Timed out", "Unfinished"].map((label) => <th key={label}>{label}</th>)}</tr></thead><tbody>{summaries.map((row, index) => <tr key={`${row.condition}-${row.repetition}-${index}`}><td>{row.condition}</td><td>{row.repetition}</td><td>{row.offered}</td><td>{row.success}</td><td>{row.rejected}</td><td>{row.failed}</td><td>{row.timed_out}</td><td>{row.unfinished}</td></tr>)}</tbody></table></div></div>;
}

function PairedTable({ paired }: { paired: Record<string, PairedMetric> }) {
  const labels: Record<string, string> = {
    successful_goodput_rps: "Successful goodput (req/s)",
    rejection_fraction: "Rejected / offered (%)",
    deadline_compliance_among_offered: "Deadline compliance (%)",
    successful_end_to_end_p95_ms: "Mean per-run end-to-end P95 (ms)",
    queue_wait_p95_ms: "Mean per-run queue-wait P95 (ms)",
  };
  const multipliers: Record<string, number> = { rejection_fraction: 100, deadline_compliance_among_offered: 100 };
  return <div className="paired-panel"><div className="plot-heading"><div><strong>Paired difference details</strong><small>Candidate minus baseline; the matched repetition is the analysis unit.</small></div></div><div className="table-scroll"><table><thead><tr><th>Metric</th><th>Mean difference</th><th>95% descriptive interval</th><th>Paired runs</th></tr></thead><tbody>{Object.entries(paired).map(([key, value]) => {
    const multiplier = multipliers[key] ?? 1;
    const interval = value.bootstrap_95_percent_interval;
    return <tr key={key}><td>{labels[key] ?? key}</td><td>{value.mean_paired_difference === null ? "—" : (value.mean_paired_difference * multiplier).toFixed(2)}</td><td>{interval ? `[${(interval[0] * multiplier).toFixed(2)}, ${(interval[1] * multiplier).toFixed(2)}]` : "Not estimated"}</td><td>{value.paired_repetitions}</td></tr>;
  })}</tbody></table></div></div>;
}
