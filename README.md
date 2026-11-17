# AI Systems Performance Lab

**Interactive agent architecture, stochastic workloads, and SLO engineering**

This project uses a real multi-tool agent to create a variable AI workload, then
measures how requests, tool/model latency, queues, failures, and capacity interact.
Synthetic commercial-credit cases are a demonstration setting only. The project
does not make credit decisions; its research focus is AI system behavior.

## Current state

The local UI supports multi-turn chat, a streamed activity panel, and a bounded
per-process conversation service. Fixture mode makes no model or AWS calls.
The AWS data layer has been provisioned and seeded with 1,002 synthetic cases
in DynamoDB and 1,003 S3 objects. Bucket privacy/encryption and the table's
on-demand mode were verified. A bounded Bedrock request later completed with
Nova Lite; its model/tool activity and token totals appeared in the UI and
CloudWatch Model Invocations. This was a functional integration check, not a
performance benchmark. The UI distinguishes account, IAM, and provider errors.

The UI's case picker contains the two hand-written examples plus a reproducible
set of 1,000 generated examples. Generated inputs are test data, not a model
quality claim. The stochastic performance experiments begin after the live
agent interaction has passed its acceptance gate. The separate `/review`
endpoint remains a deterministic workload fixture for later load tests.

## Run the local interactive demo

From the project root, start both the API and browser UI with one command:

```powershell
uv sync --group dev
uv run --env-file .env lab
```

The launcher opens <http://127.0.0.1:5174>, starts the API on port 8003, and
stops both processes when you press Ctrl+C in that PowerShell window. It uses
the checked-in UI lockfile and runs `npm ci` automatically only if UI packages
are missing. `VITE_API_URL` is set in `.env` so the UI reaches the API.

Edit `.env` to change host/port values. The launcher reports occupied ports
without terminating unrelated processes. Use `AGENT_BACKEND=fixture` in `.env`
for a local demo that makes no cloud calls.

## Use the Bedrock agent

The Bedrock path requires a Bedrock model ID and seeded DynamoDB/S3 data. The
first AWS deployment created only a private S3 bucket and an on-demand
DynamoDB table; the app and UI continue to run locally. Follow the
[AWS learning runbook](infrastructure/aws/README.md) to inspect the resources,
review costs, seed the records, run one bounded agent turn after AWS account
verification, and tear everything down. AgentCore Memory remains optional and
is not part of the first pilot.

The project-root `.env` is configured for the existing AWS profile, model,
DynamoDB table, and S3 bucket. It contains configuration only, never
credentials, and is ignored by Git. AWS CLI credentials continue to come from
the standard profile chain. Edit the relevant `.env` value and restart the
launcher to select a different profile or resource.

The model is selected by Bedrock configuration; Strands chooses among the
case-bound tools. There is no OpenAI-key path in this service. Never paste AWS
credentials into `.env`, source code, logs, or the UI.

Bedrock mode allows at most five requests per API process by default, six model
calls per request, eight tool calls, and a bounded recent-message window.
These are guardrails, not a guaranteed dollar ceiling; verify the selected
model's current input/output prices before live calls. The request guard resets
when the API restarts.

The seeder defaults to a no-write preview. It combines two unchanged examples
with 1,000 generated cases (seed `20261117`) and writes only when `--apply` is
present:

```powershell
uv run python scripts/seed_aws_demo.py --profile ai-systems-performance-lab `
  --region us-east-1 --table ai-systems-performance-lab-cases --bucket <bucket-output-from-cloudformation>
```

Review the CloudFormation change set before creating resources. The data layer
is already created in the original demo account. The local request, model-call,
tool-call, and token limits can be lowered for a live pilot. These controls
reduce accidental usage but are not a hard AWS spending cap. No Fargate
deployment or AgentCore Memory resource is included in this first step.

## Agent boundaries

- Read-only tools: exact-key DynamoDB case lookup, S3 evidence search, fixed S3
  policy read, deterministic DSCR/stress calculations, and a synthetic policy
  evaluator.
- Evidence and policy are treated as untrusted data. The agent cannot approve
  or deny a case and cannot choose arbitrary case IDs or S3 keys through tool
  arguments.
- The UI displays model/tool activity, evidence, and request timing. It does
  not display hidden chain-of-thought.
- AgentCore Memory is supplemental same-case context; current case values and
  calculations remain authoritative in DynamoDB and deterministic tools.
- The local FIFO queue and metrics are per process, not distributed across
  ECS tasks.

See [agent architecture](docs/agent_architecture.md), [AWS learning runbook](infrastructure/aws/README.md), [system specification](docs/specs/001-stochastic-ai-systems-performance/spec.md),
and [staged plan](docs/specs/001-stochastic-ai-systems-performance/plan.md).

## Checks

```powershell
uv run pytest
uv run ruff check .
uv run --env-file .env lab
```

The combined command is the local run path; the launcher manages Node/Vite, so
you do not need to run npm manually.

Fixture tests and recorded results are the repeatable/high-volume path. Real
Bedrock requests remain small, cost-bounded integration checks and must never
be used as uncontrolled load generation.
