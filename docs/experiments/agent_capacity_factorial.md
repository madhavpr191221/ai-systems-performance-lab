# A controlled experiment on agent capacity

## What we are trying to learn

We want to know how much the API's **processing limit** and **waiting-room
size** change the behavior of the real Bedrock-backed agent.

- Concurrency is the number of agent requests the API can process at the same
  time.
- Queue capacity is the number of extra requests it can hold while those
  processing slots are busy.

CloudWatch can show what happened to Bedrock. This experiment deliberately
changes two API settings, sends a planned workload, and compares the resulting
request outcomes. That gives evidence about these particular settings under
this workload. It does not replace CloudWatch or prove how a multi-task service
will behave.

## Stage 1: confirm the measurement path

Use one short, low-rate run against the AWS-hosted API. Confirm that the saved
request records have request IDs, outcomes, end-to-end time, queue wait, model
and tool call counts, and token counts when Bedrock returns them. Confirm that
the CloudWatch file contains Bedrock metrics or a clear access error.

This is a measurement pilot. It is not enough data to decide whether an SLO is
met. Review the separate cost estimate before sending the run.

## Stage 2: change concurrency and queue size

Use a complete 2 by 2 factorial design on the real Bedrock agent:

| Setting | Low | High |
|---|---:|---:|
| Processing slots (concurrency) | 1 | 4 |
| Waiting positions (queue capacity) | 0 | 8 |

This gives four configurations: `c1-k0`, `c1-k8`, `c4-k0`, and `c4-k8`.
The current `c2-k4` setting can be measured as a reference before or after
this experiment; it is not one of the four factorial cells.

Hold the rest of the experiment fixed: one local API process at a time, one Bedrock model,
one synthetic task mix, one Poisson arrival rate, one deadline, and the same
workload seed within each block. A block is one matched seed. Within each
block, run all four configurations once, in the randomized order written to
the design file. Restart the task between configurations so old in-flight
requests and counters cannot carry over.

Start with ten blocks to estimate run-to-run variation. Ten blocks are not
automatically enough to prove a 1% failure or rejection target. Use the pilot
variation and a smallest engineering-relevant improvement to choose the size
of a later confirmatory study. If the available budget does not support that
size, report the result as uncertain.

Generate a reproducible schedule without contacting AWS:

```powershell
uv run --no-sync python -m ai_systems_performance_lab.experiments.factorial `
  --blocks 10 `
  --seed 20261118 `
  --output results/aws/capacity-design.json
```

The file records each block's randomized treatment order and matched seed.
The local runner starts a fresh API process for every row and applies the
listed concurrency and queue settings. The agent still calls real Bedrock,
DynamoDB, and S3 through the AWS profile. This tests the agent and its local
queue; it does not measure Fargate CPU, memory, networking, or replica scaling.
The runner creates the same reproducible schedule itself from the seed. Preview
the complete study without starting the API or calling AWS:

```powershell
uv run --env-file .env --no-sync python -m ai_systems_performance_lab.experiments.factorial_live `
  --blocks 10 `
  --seed 20261118 `
  --rate 0.1 `
  --duration-s 50
```

If the preview is within the total amount you approve, run the same command
with `--execute --approve-cost-usd <approved-total-ceiling>`. The script will
require `AGENT_BACKEND=bedrock`, use a separate local API port (8011 by
default), run the four configurations in the randomized order, and stop each
API process before starting the next. No browser UI is needed during this
measurement.

```powershell
uv run --env-file .env --no-sync python -m ai_systems_performance_lab.experiments.factorial_live `
  --blocks 10 `
  --seed 20261118 `
  --rate 0.1 `
  --duration-s 50 `
  --execute `
  --approve-cost-usd <approved-total-ceiling>
```

This schedule has 40 short runs. At the example settings it offers about 120
requests total. At the recorded example seed, the preflight plans 204
requests. Under the runner's three-call, 10,000-input-token and 400-output-token
assumptions, that scenario is about $2.69 for Bedrock. It
does not include Fargate because the API runs locally. This estimate is not a
billing cap; actual token use and prices can differ. The script prints the
exact schedule-specific estimate before any process or request starts.

The local runner sets each process's request limit to that row's planned
arrivals. A new process avoids mixing counters or requests from the previous
configuration. The same seed within a block gives all four settings matching
arrival times and task ordering.

## What we measure

The primary first-stage outcome is the share of offered requests that finish
successfully within the chosen deadline. Rejected, failed, and late requests
remain in the denominator. Also report:

- rejection fraction among offered requests;
- technical failure fraction among admitted requests;
- successful-request latency, including P50 and P95;
- queue wait and sampled queue occupancy;
- model calls, tool calls, and input/output tokens;
- Bedrock invocation latency, throttles, errors, and tokens from CloudWatch;
- estimated model cost per successful on-time response.

Once answer scoring is implemented, the main outcome becomes **acceptable
answer completed within the deadline per offered request**. This avoids
calling a fast but incorrect answer a successful architecture.

## How we compare the four settings

For each block, calculate the run-level outcome for each of the four
configurations. Compare those four values within the same block, then summarize
the block-level differences across blocks. The analysis reports:

- the average change from concurrency 1 to 4;
- the average change from queue size 0 to 8;
- the interaction: whether a larger queue behaves differently at concurrency
  4 than at concurrency 1;
- a descriptive bootstrap interval that resamples whole blocks.

Requests from one run are not treated as independent repetitions. They shared
the same process, configuration, time window, and system conditions. In the
2 by 2 table, an effect is a difference in the run-level response between the
high and low settings. The interaction is a difference between those
differences.

The live runner writes a study folder containing the design, an execution
ledger, API logs, per-setting run records, and an `analysis.json` file. If the
run is interrupted or a provider call fails, the ledger shows which treatments
completed and which did not. To re-run the analysis from saved run folders:

```powershell
uv run --no-sync python -m ai_systems_performance_lab.experiments.factorial `
  --analyze results/aws/factorial/STUDY_ID/runs/RUN_C1_K0 `
            results/aws/factorial/STUDY_ID/runs/RUN_C1_K8 `
            results/aws/factorial/STUDY_ID/runs/RUN_C4_K0 `
            results/aws/factorial/STUDY_ID/runs/RUN_C4_K8 `
  --response on_time_success_fraction_offered `
  --output results/aws/capacity-analysis.json
```

The run summaries must use the supplied `--block-id` (or matching repetition
numbers when each treatment has multiple repetitions) and contain all four
settings for each included block. Incomplete blocks are listed and excluded.
The interval is descriptive evidence from these runs; it is not a
production guarantee. Do not claim an SLO is met just because no bad event
appeared in a small sample.

## Next experiment: agent execution policy

After the capacity study, study choices inside the agent. Do not combine every
setting into one enormous experiment. A useful screening design can test four
two-level choices:

1. tool deadline: no special deadline versus a bounded deadline;
2. maximum model calls: lower versus higher;
3. output-token limit: lower versus higher;
4. tool execution: sequential versus concurrent when the agent requests
   independent tools together.

An eight-run Resolution IV fractional factorial can screen these four choices
with fewer runs than all sixteen combinations. It cannot separate every
two-factor interaction, so any promising or suspicious interaction needs a
follow-up experiment. The exact levels must be selected from a pilot and a
separate cost estimate before this stage is run.

## Next stage: answer-quality scoring

Latency and completion alone do not measure answer quality. The current runner
does not save answers or call a judge. The next stage is a separate OpenAI
judge that scores saved synthetic-case answers after the Bedrock runs; its time
and cost stay outside the measured Bedrock request path. The judge must not
see the configuration name, run order, queue size, or concurrency. Its output
schema is:

```json
{
  "rubric_version": "credit-agent-answer-v1",
  "factual_correctness": 0,
  "evidence_support": 0,
  "completeness": 0,
  "safety_boundary": 0,
  "acceptable": false,
  "issues": ["short, observable issue"]
}
```

Each dimension is scored 0, 1, or 2. An answer is acceptable only when factual
correctness and safety boundary are both 2, and evidence support and
completeness are at least 1. The evaluation set contains the same task and
case in every treatment. Deterministic checks verify numeric facts and required
citations where possible. The judge is an imperfect measurement instrument:
keep the rubric fixed, hide treatment labels, record the judge model and
rubric version, and report disagreement or missing scores. Do not treat judge
scores as ground truth.

When implemented, the judge will be a separate paid OpenAI call. Its maximum
answer count and token-cost scenario must be previewed and approved separately.
It must never be called silently as part of the agent workload.

## What a defensible conclusion sounds like

> Under this one local API process and this synthetic Poisson workload, raising
> concurrency from 1 to 4 changed the average on-time success share by X
> percentage points across N complete matched blocks. The interval was [L, U].
> Queue size changed the result by Y, with evidence of / no clear evidence of
> interaction. Bedrock throttling and answer-quality results were Z. This is
> evidence about the tested setup, not a multi-replica capacity guarantee.

We will only fill in that statement from saved results. If the interval is wide,
the correct conclusion is that this experiment has not yet distinguished the
settings clearly.
