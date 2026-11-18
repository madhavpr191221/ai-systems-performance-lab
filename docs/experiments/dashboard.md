# Performance dashboard: stages, evidence, and AWS boundary

## Purpose

The dashboard is an experiment workbench for the agent service. It gives the
operator control over a defined workload and application configuration, then
shows request outcomes, occupancy, and paired comparisons. The agent does not
observe or control these measurements. The workload is a simulated tool-using
agent served through the local HTTP API; these results are not measurements of
Amazon Bedrock or an AWS deployment.

## Implemented stages

### Dashboard workflow and themes

The main performance-lab path is: choose a bottleneck, configure workload and
SLO targets, run the study, then optionally ask the AI reviewer to interpret
the saved results. The SLO candidate sweep changes only the selected factor:
queue capacity, concurrency, arrival variability, or offered rate. Candidate
lists are separate from the fixed controls. A collapsed “More experiments”
section keeps the older matched baseline comparison available for diagnostics.
Saved history is loaded only when explicitly opened.

Results show condition averages and candidate-minus-baseline paired
differences, with descriptive intervals when at least ten paired repetitions
are available. Two time plots show cumulative offered/admitted/completed
requests and instantaneous active/waiting occupancy. Values are averaged
within each repetition and then across repetitions in 250 ms bins; polling can
miss short-lived peaks. Historical runs without the required labels or samples
remain browsable but do not receive a time plot.

The agent and experiment screens share a light/dark theme switch. Light is the
default unless a theme has already been saved in this browser. The selection
persists across reloads. The theme preference is local presentation state and
does not affect experiment artifacts or API behavior.

### Stage 1 — Browse saved runs

- List run folders under `results/local` and open a selected run.
- Show its manifest, provenance, configuration, summary, sampled occupancy,
  and a bounded sample of request rows.
- Normalize the earlier summary-list format for display without rewriting
  the original results.
- Reject run IDs that could escape the configured results directory.

### Stage 2 — Run a bounded one-factor comparison

The user chooses one factor to change between a baseline and candidate:

- Arrival process: deterministic or Poisson.
- Offered rate: 1 to 50 whole requests per second.
- Processing concurrency: 1 to 16.
- Waiting capacity: 0 to 128 positions.

The remaining shared controls include arrival process and rate, concurrency,
queue capacity, deadline, and simulated tool/model delay. Deadlines are entered
in seconds as any finite positive decimal (for example, 4.91); the UI converts
the value to milliseconds for the API and saved artifacts. Each repetition uses
the same seed for both configurations, so pairs share their Poisson arrival
schedule when arrival type is held fixed, as well as the synthetic case order.
In an arrival-process comparison, deterministic and Poisson schedules differ by
design; the seed makes the Poisson schedule reproducible. This is a matched
experiment, not perfect control of all nondeterminism: process startup,
wall-clock scheduling, machine contention, and request timing still vary.

The API permits one interactive run at a time. Bounds are enforced server-side
as well as in the form: 1–10 repetitions, 1–30 seconds per run, and at most 600
seconds of offered workload across both conditions. Arrival rate, concurrency,
queue capacity and delays have explicit limits; a deadline must be finite and
greater than zero. Experiments force
the simulated backend and strip AWS configuration from child processes. A
process restart loses the current in-memory job status, while completed result
artifacts remain on disk.

The instantaneous agent-state endpoint includes cumulative completed turns in
addition to offered, admitted, and rejected counts. Each experiment snapshots
these cumulative counters relative to its own start, samples them alongside
active/waiting occupancy, and appends a final observation-boundary sample after
the response wait. The run-detail
API returns bounded 250 ms means for plotting, with at most 240 time bins per
condition. The raw occupancy artifact remains available for inspection.

### Stage 3 — Paired statistical summary

For each repetition, the dashboard computes candidate-minus-baseline
differences for successful goodput, rejection fraction, deadline compliance,
successful-request end-to-end P95, and queue-wait P95. The repetition is the
analysis unit. Requests in one run share workload and service conditions, so
the dashboard does not treat them as independent samples for interval
estimation.

With at least ten valid paired repetitions, the API calculates a seeded,
run-level percentile bootstrap interval for the mean paired difference using
10,000 resamples. With fewer pairs, it displays the differences and mean but
does not estimate an interval. Ten pairs are still a small sample; the interval
is descriptive and does not establish a production guarantee. It also does
not cover machine-to-machine or day-to-day variation.

Latency percentiles are computed from successful request-level observations
within each repetition. Rejections, failures, timeouts, and unfinished
requests remain visible as separate outcomes. Unfinished requests are not
converted to failures at the observation boundary.

## What this dashboard adds beyond CloudWatch

CloudWatch is the AWS service for observing deployed resources, service
metrics, logs, alarms, and operational time series. It answers questions such
as whether ECS tasks are healthy, CPU or memory is elevated, or aggregate
request/error metrics are changing. The local dashboard answers controlled
comparison questions: what changed when queue capacity moved from zero to 16
under a matched generated workload, how much run-to-run variation was present,
which requests waited or were rejected, and whether a larger queue moved
failures into deadline misses.

These uses complement each other. CloudWatch cannot replace this controlled
experiment record and paired statistical analysis. The experiment dashboard
cannot replace AWS operational monitoring, resource health, alarms, or live
service telemetry. The local results should not be presented as CloudWatch or
AWS measurements.

## SLO study and architecture review

The SLO form runs a bounded one-factor sweep against a fixed workload and
synthetic service-delay configuration. The selected bottleneck determines the
candidate axis: concurrency, queue positions, whole offered requests per
second, or deterministic versus Poisson arrivals. All other settings are
stored as fixed study inputs. The saved manifest and report retain the chosen
focus and each candidate's full configuration.

It reports each candidate's deadline goodput, rejection fraction, and admitted
technical-failure fraction. The default targets are illustrative and editable:
95% of offered turns complete within five seconds, no more than 1% of offered
turns are rejected, and no more than 1% of admitted turns fail.
Deadline targets are entered as any finite positive decimal number of seconds;
the saved SLO profile stores the equivalent value in milliseconds.

The request-level definitions are:

- Deadline goodput = successful completions within deadline / offered turns.
- Rejection fraction = rejected turns / offered turns.
- Admitted failure fraction = failed or timed-out turns known to have been
  admitted / admitted turns.

Candidate classification uses a percentile bootstrap across whole repetitions.
It requires at least ten repetitions; fewer runs are labelled insufficient
evidence. A candidate is called feasible only when the lower confidence bound
for deadline goodput clears its target and the upper bounds for rejection and
admitted failures stay below their limits. These intervals summarize this
experiment's repeatability; they are not a production guarantee. The configured
confidence level is visible in the saved SLO profile.

If a candidate shows no deadline misses, rejections, or admitted technical
failures, it is labelled inconclusive for rare-event evidence even if the
bootstrap interval is degenerate at zero or one. Resampling identical
repetitions cannot establish a useful upper bound on a rare failure rate; the
next study should increase offered sample volume or introduce an explicitly
modeled failure mechanism.

This local sizing sweep cannot claim least-cost AWS sizing. Each candidate uses
one local API process; changing concurrency and queue size does not change a
real replica count or Bedrock quota. The report therefore identifies a
candidate to investigate rather than an optimized AWS architecture. A later
AWS stage must add a documented price model and validated multi-replica
measurements before making a cost-minimization claim.

The optional AI architecture reviewer runs only after the user clicks its
button. It is outside the agent workload measurement, uses an OpenAI key from
the environment, has a four-tool-call limit, and can only inspect aggregate
saved evidence. It does not receive request-level samples, case IDs, prompts,
or credentials. Its structured response contains a decision, summary, optional
candidate ID, evidence-linked findings, one controlled next experiment
(question, change, held-constant settings, and success evidence), and
limitations. Candidate IDs and evidence metric names are constrained by the
saved report schema. The decision is one of `candidate_for_next_experiment`,
`no_candidate_meets_targets`, or `more_evidence_needed`. Each finding has a
title, interpretation, confidence, and one or more `{candidate_id, metric}`
references. A next-experiment object contains `question`, `change`,
`hold_constant`, and `success_evidence`. The interface fills displayed
measurements and intervals from the artifact, rather than generated AI text.
The reviewer cannot launch a study,
change runtime capacity, or deploy resources. The review request may incur
OpenAI charges. No OpenAI call is made when the app starts or when a local
experiment is run.

## Later AWS telemetry stage — not part of this implementation

After local runs and their statistical interpretation are reviewed, a later
stage can add a short-lived ECS Fargate deployment and compare local results
with real AWS observations. The proposed division is:

- CloudWatch native ECS/service metrics for resource and service health.
- A small set of low-cardinality application metrics for offered, admitted,
  rejected, completed, failed, in-flight, and queue-wait behavior.
- Local experiment artifacts for request-level outcomes, matched-run analysis,
  workload definitions, and reproducibility.
- Bedrock invocation telemetry for provider-side model calls, latency, and
  token usage, kept distinct from application-tier scaling and capacity.

Do not send request IDs, conversation IDs, case IDs, or other high-cardinality
values as metric dimensions. Keep request-level data in bounded local artifacts
or a separately costed trace/log design. Before the AWS stage, document the
expected Fargate, load balancer, CloudWatch log/metric, data transfer, and
Bedrock costs; define resource and token limits; confirm teardown; then obtain
explicit authorization for provisioning or paid load/inference experiments.
No such AWS provisioning or paid test is performed here.

## Review checklist for interpreting a run

1. Confirm source labels and manifest say local simulated HTTP measurements.
2. Check the matched seeds and all shared configuration values.
3. Compare the full outcome accounting, including unfinished requests.
4. Inspect occupancy and request-level queue/processing durations.
5. Treat request-level percentiles as within-run descriptions; use run-level
   paired differences to reason about uncertainty.
6. Check scheduler lag and sampled occupancy limitations before diagnosing a
   bottleneck.
7. State which conclusions apply only to this local single-process workload.
8. Record the architecture decision and the next experiment that could falsify
   it.
