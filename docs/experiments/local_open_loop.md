# Local open-loop agent workload experiment

## Purpose and status

This experiment measures how variability in offered requests, a finite FIFO
waiting queue, and bounded processing concurrency affect a multi-tool agent
workload. It sends HTTP requests through the same local session and streaming
API used by the demo. It forces the child API process to use the deterministic
simulated agent and removes AWS credential/configuration variables from that
process. It makes no AWS or Bedrock calls.

All outputs are **local simulated measurements**, not AWS measurements. Synthetic
case content is workload input only. The 500 ms deadline is a diagnostic
threshold for comparing cells, not a production SLO.

## Experimental design

### Questions and hypotheses

1. **Arrival variability:** at the same nominal mean rate, does Poisson traffic
   produce more queueing and worse tail latency than deterministic traffic?
   **Hypothesis:** at $\rho=0.7$, both should usually complete without
   overload, but Poisson arrival bursts should raise queue-wait tails; at
   $\rho=1.2$, both exceed the normalizer and Poisson bursts should produce
   more transient rejection or deadline misses.
2. **Queue capacity:** does a larger queue help users, or mainly postpone
   overload? **Hypothesis:** for $\lambda>\widehat{\mu}$, increasing $K$
   reduces immediate rejection and leaves long-run service capacity nearly
   unchanged, while increasing waiting time and deadline misses.
3. **Concurrency:** does increasing application concurrency improve goodput?
**Hypothesis:** on this asynchronous simulated workload, increasing $c$ raises
   service capacity and reduces queueing until the client, event loop, or some
   other modeled stage becomes limiting. It says nothing by itself about
   Bedrock serving capacity.

The controlled factors are simulation implementation, configured tool/model delay,
case selection trace seed, offered-rate normalization, and duration within each
comparison. Queue capacity and concurrency change only in their respective
experiments. The runner preserves each request's scheduler lag so a load
generator bottleneck can be detected.

### Acceptance checks

- Deterministic schedule spacing is exact; Poisson schedules are seeded and
  have exponential gaps.
- Request dispatch remains open-loop even when preceding responses are slow.
- Every generated arrival schedule has exactly one recorded outcome. A request
  that could not be dispatched before the drain boundary remains in the planned
  offered denominator and is distinguished by a missing dispatch timestamp.
- Planned arrivals, HTTP dispatch attempts, and API-received requests are
  reported separately; API sample paths satisfy both conservation identities
  at the observation boundary.
- For each process run, server counters satisfy
  $C_o=C_a+C_r$ and $C_a=C_c+N$ at the observation boundary, where
  $C_r$ is rejected, $C_c$ completed, and $N$ remains in flight.
- The simulated process is configured without AWS credentials and reports
  `inference=simulated` before measurement.
- The output manifest identifies simulated provenance and declares that no AWS
  or Bedrock calls were made.
- The runner writes request-level data, occupancy samples, summaries, and a
  noninteractive plot.

The sequential baseline measures simulated processing time and normalizes load:

$$
\widehat{\mu}_{\mathrm{capacity}} = c_0 / \bar{s},
$$

where $c_0=2$ is the baseline concurrency and $\bar{s}$ is the mean
processing duration from ten sequential simulated turns. This is a capacity
normalizer for the experiment, not a guaranteed service rate. It omits
contention, queueing, HTTP client limits, and run-to-run variability.

The default matrix contains eight unique cells, each repeated three times:

1. **Arrival variability:** deterministic and Poisson arrivals at offered rates
   $0.7\widehat{\mu}_{\mathrm{capacity}}$ and
   $1.2\widehat{\mu}_{\mathrm{capacity}}$, with $c=2,K=4$.
2. **Queue capacity:** Poisson arrivals at 1.2 times normalized capacity, with
   $c=2,K\in\{0,16\}$. The $K=4$ comparison is shared with experiment 1.
3. **Concurrency:** the same Poisson offered rate, with $c\in\{1,4\},K=4$.
   The $c=2$ comparison is shared with experiment 1.

Default duration is 30 seconds per repetition. Repetitions use distinct seeds
derived from the recorded base seed. They are repeated observations, not
independent samples guaranteed by design; report their spread descriptively,
not as confidence intervals.

## Request scheduling and outcomes

The runner generates the full arrival schedule before sending turns. The
planned offered workload includes every generated arrival; the API's offered
count includes only requests received at the service. Planned arrivals, client
dispatch attempts, and API receipts are reported distinctly, including arrivals
the generator could not dispatch or the service did not receive.
For a Poisson schedule, interarrival times are iid exponential with configured rate;
deterministic arrivals are evenly spaced. Each request has a pre-created
session, then an independent asyncio task waits for its scheduled wall-clock
time and issues the stream request. It does not wait for the preceding turn to
finish before scheduling a later turn (open-loop generation).

Every scheduled arrival is retained in the planned offered denominator.
Outcomes are classified as success, queue-full rejection, other failure, client timeout, or
unfinished at the fixed ten-second drain boundary. Successful end-to-end latency
is measured from scheduled arrival to completion, which includes scheduler lag;
queue waiting and processing times are also taken from the service response.
The run records successful P50/P95/P99 from request-level end-to-end samples,
deadline compliance among all offered turns, successful goodput, deadline
goodput, server counters, and periodic process-local occupancy snapshots.
The report provides an overall rejection fraction over planned offers and an
API rejection fraction over actual API offers, so generator non-dispatch is
not mistaken for service rejection.

The local service has one process-local queue. Its occupancy is not distributed
across replicas. The experiment does not model autoscaling, network variability,
Bedrock quotas, model-serving contention, retries, or correlated AWS failures.
Cancelled turns at the drain boundary are right-censored as unfinished, not
counted as completed failures. A queue-full response is an SSE application
event, even though the HTTP response status is 200.

## Run

From the repository root:

```powershell
uv sync --group dev
uv run --env-file .env --no-sync python -m ai_systems_performance_lab.experiments.local_http --scenario all
```

The runner ignores the selected `.env` backend in its child process and forces
simulated mode (`AGENT_BACKEND=simulated`). Existing `.env` settings are not edited. Useful shorter runs:

```powershell
uv run --env-file .env --no-sync python -m ai_systems_performance_lab.experiments.local_http --scenario arrivals --duration-s 5 --repetitions 1
uv run --env-file .env --no-sync python -m ai_systems_performance_lab.experiments.local_http --scenario queue --duration-s 10 --repetitions 1
```

On Windows, stop an existing `uv run lab` launcher with Ctrl+C before syncing
if it is holding `.venv\Scripts\lab.exe` open. The `--no-sync` module command
then runs the experiment through the already-synced project environment.

Use Ctrl+C to stop. Each run creates a new timestamped directory under the
ignored `results/local/` path containing:

- `manifest.json`: source, configuration, seeds, baseline and provenance labels.
- `summary.json`: per-cell and per-repetition measurements.
- `requests.csv`: one row per offered request, including unfinished requests.
- `queue_occupancy.csv`: sampled process-local active/waiting occupancy.
- `baseline_requests.csv`: sequential simulated baseline sample.
- `comparison.png`: successful end-to-end P95 and rejection fraction by cell.

## Interpretation limits

- The capacity normalization is estimated from a short sequential simulated run;
  it is not a queueing-theory prediction or a cloud capacity guarantee.
- Matched mean offered rate does not make a short deterministic trace equivalent
  to a Poisson trace; finite-window realized counts differ.
- P99 is exploratory when successful sample counts are small. Always report the
  sample count and inspect the per-request data before making a tail claim.
- Latency quantiles are calculated from successful requests only. Deadline
  compliance uses all offered requests, so rejection and unfinished requests
  count as misses there.
- The application reports queue and processing stages; end-to-end latency is
  calculated per request. Stage quantiles are never added to estimate end-to-end
  quantiles.
- The default number of concurrent client connections is finite. Scheduler lag
  is retained so load-generator limitations are visible rather than silently
  mistaken for service queue time.

## Smoke-run observation

A one-second queue-scenario pilot on the local simulated workload ran on 2026-11-17 with
one repetition and 18 planned arrivals per cell. The sequential baseline
estimated 13.58 turns/s; the resulting nominal offered rate was 16.30 turns/s.
With $K=0$, 9 turns succeeded and 9 were rejected; successful P95 was
197 ms and 50.0% of planned turns met the diagnostic deadline. With $K=16$,
all 18 were admitted, successful P95 was 726 ms, and 44.4% met the deadline.
These are local measurements from a short engineering smoke run, not a stable
benchmark or a claim about AWS. The sample is far too small for reliable tail
estimates; its purpose was to check scheduling, overload accounting, artifact
generation, and the expected queue/rejection trade-off. The generated files are
ignored by Git under `results/local/`.

## Questions to defend

- Why use open-loop scheduling? It prevents slow responses from reducing the
  offered rate and hiding overload, while scheduler lag reveals generator
  limitations.
- Why finite queue sizes? A larger queue can reduce immediate rejection while
  increasing waiting and deadline misses; the measurements compare both effects.
- Why use simulation rather than Bedrock for this load test? It gives a controlled,
  reproducible service-time mechanism without uncontrolled paid calls. Bedrock
  integration needs separate, bounded experiments and cannot be represented as
  high-volume inference capacity from this result.
- What changes the recommendation? A measured production workload trace,
  explicit business SLOs, empirically measured model token/service distributions,
  provider quota evidence, and multi-replica tests with queue ownership defined.
