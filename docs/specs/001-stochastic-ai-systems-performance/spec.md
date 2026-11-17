# Specification: AI Systems Performance Lab

**Subtitle:** Stochastic Workloads, Agentic AI, and SLO Engineering  
**Status:** Draft for review  
**Spec ID:** 001

## 1. Problem and purpose

Given a variable workload of tool-using AI requests, how should an inference application be designed and sized to meet latency, completion, reliability, and cost objectives? The project will answer this through a real multi-step agent workload, explicit stochastic models, controlled experiments, instrumentation, and an incremental AWS deployment.

The subject of the project is system performance and SLO compliance. The agent is a nontrivial workload that creates realistic variation in model calls, tool calls, and request duration. Commercial-credit review is the current example task for that agent, not the project’s research domain. Agent answer quality is evaluated as a secondary correctness/safety dimension; primary evaluation concerns the system’s behavior under stochastic demand and service.

System performance is observed using request events, metrics, traces, workload generators, and analysis/dashboard tools outside the agent’s decision loop. The agent does not monitor itself.

## 2. Demonstration

In a short interview walkthrough, a user should be able to:

1. Chat with a real Bedrock-backed agent and see its model/tool activity and evidence in the UI.
2. Ask follow-up questions in a stable case-scoped session; use fixture playback when AWS is unavailable.
3. After the agent passes its completion gate, apply deterministic, Poisson, bursty, or recorded arrivals to the bounded service.
4. Observe offered/admitted/rejected/completed work, queue occupancy, concurrency, tool/model latency, failures, and end-to-end latency.
5. Compare configurations under an equivalent offered workload and explain the stochastic model, measured evidence, sizing decision, and cost trade-offs.

The demo must work locally with controlled model/tool substitutes and saved results. A bounded live Bedrock run and AWS measurements are later extensions; no live cloud access is required for recorded-demo playback.

## 3. Goals

- Build a real multi-step agent workload with typed tools and variable execution paths.
- Characterize stochastic arrivals, service-time distributions, tool-call fan-out, retries, queueing, concurrency, throughput, tail latency, and failures.
- Use discrete-event simulation as a controlled model and compare its predictions with local/AWS measurements.
- Define measurable, request-level SLOs and quantify compliance without hiding rejection, failures, or unfinished requests.
- Identify bottlenecks from stage-level timing and traces rather than inference from aggregate CPU alone.
- Size application concurrency, queue capacity, and replica count against workload and SLO requirements.
- Compare fixed and reactive capacity; model detection and startup delays.
- Estimate operating cost and keep cloud experiments bounded.
- Evaluate agent task correctness and safety enough to establish that the workload is meaningful, while keeping this secondary to systems performance.

## 4. Scope and boundaries

### Representative agent workload

The first agent performs an advisory review of synthetic commercial-credit cases. It retrieves case documents and structured values, calls deterministic tools for ratios and stress scenarios, checks a synthetic policy set, and returns an evidence-linked memo for a human. This produces realistic variation in tool count, call order, tool latency, and total service demand. No real borrower data or autonomous credit decisions are in scope.

The agent domain is a workload fixture. The project remains useful if that task is later swapped for another multi-tool agent with comparable request traces and service-demand variation.

### System boundary

The measured path includes ingress, admission, bounded queue, agent orchestration, model-provider calls, tool calls, response completion, and instrumentation. The initial queue is process-local, finite, and nondurable. A local semaphore or queue must not be presented as a distributed admission limit. Bedrock model capacity/quotas and ECS application capacity are separate boundaries.

### Out of scope initially

- Unrestricted autonomous decisions or cloud write actions.
- High-volume real Bedrock load tests.
- Kubernetes/EKS, multi-agent frameworks, and durable distributed workflows before requirements justify them.
- Claims about managed-model internals that cannot be measured.
- Optimizing agent correctness as the central research question.
- Treating simulated or fixture results as cloud measurements.

## 5. Functional requirements

### Agent workload

- FR-01: Implement a bounded multi-step agent using typed tools; the initial task uses synthetic credit cases and read-only evidence/calculation/policy tools.
- FR-02: Support variable tool-call sequences and case complexity; record each model/tool call and its parent request.
- FR-03: Keep deterministic calculations in tools and retain basic evaluation for evidence support, calculation correctness, and prohibited outputs.
- FR-04: Provide a deterministic model/tool fixture path and an interchangeable Amazon Bedrock model adapter.
- FR-05: Enforce per-request deadline, call/retry limits, and explicit outcomes for tool/model timeout, throttling, unavailability, and malformed output.

### Workload and service

- FR-06: Support seeded deterministic and Poisson arrivals first; add bursts/nonstationarity and recorded traces later.
- FR-07: Support constant, exponential, positively skewed, and mixture service durations; optionally model input/output-token-dependent work.
- FR-08: Enforce finite waiting capacity `K` and processing concurrency `c`, where `K` is waiting slots excluding active work.
- FR-09: Track offered, admitted, rejected, completed, successful, failed, timed-out, cancelled, and unfinished requests separately.
- FR-10: Record request-level and stage-level timestamps for queue wait, orchestration, model, each tool, and end-to-end duration.
- FR-11: Keep unfinished requests at the observation boundary as unfinished/censored, not completed failures.

### Observability and evaluation

- FR-12: Instrument independently of the agent with correlated request/trace IDs and structured events.
- FR-13: Report SLO compliance, rejection, failure, queue, throughput/goodput, latency, resource, and estimated cost metrics with explicit populations and denominators.
- FR-14: Compare configurations under equivalent offered workloads and reproducible seeds.
- FR-15: Label each result as analytical prediction, simulation, local measurement, or AWS measurement.
- FR-16: Make recorded results available without AWS credentials or live model access.

## 6. SLO contract and metric definitions

Each experiment defines its workload, observation window, deadline `d`, success condition, and target before execution. Avoid an SLO that can appear healthy merely because rejected or failed requests disappear from the latency sample.

The core request-level compliance measure is:

`deadline_goodput = count(successful requests completed within d) / count(offered requests)`.

Rejected, failed, timed-out, and unfinished offered requests do not count as compliant in this measure. Report alongside it:

- Rejection fraction = rejected / offered.
- Post-admission failure fraction, with its denominator explicitly defined.
- Latency quantiles for successful completed requests, clearly labeled as conditional diagnostics.
- Offered/admitted/completed rates; throughput and successful goodput.
- Queue waiting, processing, model, tool, and end-to-end request latency.
- P50/P95/P99 from request-level observations; never add stage quantiles to estimate end-to-end quantiles.
- Queue occupancy, active concurrency, replica count, provider errors/throttling, retries, and cost.

Scenario-specific targets are selected before experiments. Numerical targets are not universal project constants.

## 7. Mathematical and experimental requirements

Let `C_o(t)`, `C_a(t)`, and `C_d(t)` be cumulative offered, admitted, and completed counts. Let `N(t)=N_q(t)+N_p(t)`, with `N_p(t)<=c` and `N_q(t)<=K`. For a run without cancellation/abandonment, verify `N(t)=N(0)+C_a(t)-C_d(t)` on every sample path; extend the accounting explicitly when other departures exist.

The primary controlled model is discrete-event. State changes occur at arrivals, admission/rejection, service starts/completions, timeouts/cancellations, and later replica activation/removal. Analytical queueing results are comparisons with documented assumptions, not substitutes for simulation or measurement.

Agent requests may contain multiple model/tool calls. Preserve request-level traces and call-level durations; do not assume request service times are exponential or independent. Repeated runs and scenario-level uncertainty are required when outcomes share overload periods or common provider states.

## 8. Acceptance criteria for the first useful release

1. The React UI supports follow-up turns in a stable case-bound conversation.
2. In Bedrock mode, Strands lets the model select among bounded read-only tools; no fixed tool order is imposed.
3. Current case facts come from DynamoDB; S3 evidence/policy has source IDs; calculations remain deterministic.
4. Conversation state is persisted to S3 and optional AgentCore Memory is same-case supplemental context.
5. Model/tool limits, request timing, failure outcomes, and external service metrics are visible without exposing hidden reasoning.
6. Mocked tests pass for case isolation, missing sources, partial failures, and tool/model budgets; the fixture path works offline.
7. The agent completion gate in `plan.md` passes before stochastic workload experiments begin.

## 9. Assumptions and open decisions

- The initial agent task is a synthetic multi-tool commercial-credit review, used to create realistic request paths; task-quality evaluation is secondary.
- Strands Agents owns the model-driven tool loop; the deterministic fixture remains separate for reproducible/high-volume performance experiments. Application admission, request budgets, and telemetry remain outside agent decisions.
- Deterministic fixtures are the default for repeatable/high-volume runs. Live Bedrock calls are low-volume and require current quota/pricing review and explicit authorization.
- Selected AWS path: Bedrock for model calls; DynamoDB for synthetic case facts; S3 for evidence, policy, and session state; optional AgentCore Memory for same-case recall; ECS Fargate for a short private runtime check. CloudWatch/OpenTelemetry instrumentation follows with the performance work.
- No AWS resources are provisioned by this specification.
- The demo runs locally and uses the user's AWS profile for low-volume real calls. High-volume load tests use fixtures/replay. The initial Bedrock/Memory integration targets a $5 ceiling; Fargate requires a separate estimate and authorization.
- Stochastic analysis starts after the agent completion gate, not before it.

## 10. Principal risks

- Mistaking request-level agent fan-out for independent fixed-duration work.
- Survivorship bias if rejected, failed, or unfinished work is excluded from SLO reporting.
- Confounding case complexity with provider latency or application queueing.
- Correlated overload outcomes invalidating iid confidence intervals.
- Generalizing process-local limits to a multi-replica service.
- Cloud charges from idle resources, logs, networking, and model tokens.
- A live-service-dependent demo failing during an interview.

Mitigate through request/call traces, explicit outcome accounting, seeded scenarios, repeated runs, source labels, saved results, cost bounds, and teardown verification.
