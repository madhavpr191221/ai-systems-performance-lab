# Performance dashboard: stages, evidence, and AWS boundary

## Purpose

The dashboard is an experiment workbench for the agent service. It gives the
operator control over a defined workload and application configuration, then
shows request outcomes, occupancy, and paired comparisons. The agent does not
observe or control these measurements. The workload is a simulated tool-using
agent served through the local HTTP API; these results are not measurements of
Amazon Bedrock or an AWS deployment.

## Implemented stages

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
- Offered rate: 0.1 to 50 requests per second.
- Processing concurrency: 1 to 16.
- Waiting capacity: 0 to 128 positions.

The remaining shared controls include arrival process and rate, concurrency,
queue capacity, deadline, and simulated tool/model delay. Each repetition uses
the same seed for both configurations, so pairs share their Poisson arrival
schedule when arrival type is held fixed, as well as the synthetic case order.
In an arrival-process comparison, deterministic and Poisson schedules differ by
design; the seed makes the Poisson schedule reproducible. This is a matched
experiment, not perfect control of all nondeterminism: process startup,
wall-clock scheduling, machine contention, and request timing still vary.

The API permits one interactive run at a time. Bounds are enforced server-side
as well as in the form: 1–10 repetitions, 1–30 seconds per run, and at most 600
seconds of offered workload across both conditions. Arrival rate, concurrency,
queue capacity, delays, and deadline have explicit limits. Experiments force
the simulated backend and strip AWS configuration from child processes. A
process restart loses the current in-memory job status, while completed result
artifacts remain on disk.

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
