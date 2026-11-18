# Measuring the real Bedrock agent

## The question this study asks

When requests arrive at different rates and with different amounts of work, can
our deployed AI agent meet its response-time and reliability targets? If it
cannot, do we see evidence that the limit is the app's queue, the model, or
another part of the request?

This is a small, controlled experiment against the actual Bedrock-backed agent.
For the first live experiment, the API can run on your computer while the agent
uses Bedrock and the existing AWS data. This avoids deploying a container just
to begin measuring stochastic behavior. A later experiment can run the same
API in ECS Fargate. CloudWatch shows AWS service metrics; this study creates a
known workload and records what happened to each request. Together, they help
explain system behavior. Neither the local experiment nor a one-task Fargate
experiment proves how a multi-replica service will behave.

## A plain-language picture

```text
Load generator on your computer
  sends requests on a planned schedule
        |
        v
FastAPI + Strands agent + bounded in-process queue
  local first; one ECS Fargate task later
        |
        +--> Amazon Bedrock model
        +--> synthetic case data in DynamoDB and S3
        |
        v
Request outcomes and timings saved by the experiment runner
CloudWatch provides AWS-side model metrics for the same time period
```

The **load generator** decides when each request should arrive. The schedule is
open-loop: a slow response does not delay the next planned arrival. That is
useful for testing a stated incoming demand, though actual dispatch can lag if
the computer or network cannot keep up. The runner records planned and actual
dispatch times so that difference is visible.

In the first experiment, the **API process** runs on your computer. Its
concurrency limit and waiting queue are local to that process. The agent still
makes real Bedrock requests and reads the real synthetic case data from AWS.
This measures the agent path and its local queue, not AWS compute capacity.

In the later Fargate experiment, one small managed container runs the API and
agent on AWS. It has 0.5 vCPU and 1 GiB memory in the template. Its concurrency
limit and waiting queue are local to that task; they are not shared across
multiple tasks.

## What varies, and what stays fixed

The planned workload compares:

- **Arrival pattern:** regular arrivals at fixed gaps, or Poisson arrivals
  whose gaps are independently exponential with the same average rate.
- **Average rate:** 0.05 or 0.1 requests per second. These are deliberately
  modest rates for a cost-controlled first study.
- **Request type:** a fixed mix of 40% short questions, 40% ordinary summaries,
  and 20% questions likely to use several tools. Every question uses synthetic
  cases.
- **Repetition:** ten runs per arrival-pattern/rate condition, each lasting 60
  seconds. A fixed seed makes the schedule reproducible; a different seed can
  produce a different Poisson arrival count.

The default checked-in seed plans 188 requests in total. The exact plan is
printed by the preflight command before a live run. One agent turn can make up
to three model calls in the cost scenario. Actual tool and model paths may use
fewer calls.

The model is `us.amazon.nova-2-lite-v1:0` by default, through the `us-east-1`
inference profile. Verify current model availability, quotas, and pricing
before running. The 5-second deadline below is a study target, not an assertion
that the current agent can achieve it. An earlier interactive turn took about
14 seconds end to end, so it is plausible that this first system will miss the
target.

## Targets and measured outcomes

The initial targets are:

| Measure | Target | How to read it |
|---|---:|---|
| Successful responses completed within 5 seconds / all offered requests | At least 95% | Includes requests rejected, failed, or too slow in the denominator. |
| Rejected requests / all offered requests | At most 1% | Shows how often the API declines work at admission. |
| Technical failures among admitted requests | At most 1% | Excludes requests rejected before admission; timeouts are counted as failures here. |

For each request, the runner records the planned arrival, actual dispatch,
outcome, end-to-end time, queue wait, processing time, model/tool call counts,
and token counts when returned by the agent. The API records whether the
request was admitted and when its processing stages started and ended.

**End-to-end time** runs from dispatch at the load generator until the response
or failure. It includes network transit, waiting for an app slot, agent work,
model calls, and tools. **Queue wait** is the time spent waiting for the app's
processing slot. **Bedrock invocation latency** is measured by AWS for model
invocations; it does not include the full agent turn or app queue.

These separate clocks help distinguish possible bottlenecks. For example, a
long end-to-end time with a short queue wait points away from the app queue,
while a long Bedrock invocation time suggests the model call took a substantial
part of the request. Such comparisons are evidence for a diagnosis, not proof
of causality by themselves.

## Why use both request records and CloudWatch?

CloudWatch's `AWS/Bedrock` metrics include invocation counts, invocation
latency, token counts, throttles, and errors. They are aggregated by model and
time period (typically one minute); they do not identify each request in this
study. Some errors that happen before AWS identifies the model might not appear
in model-specific error metrics.

The experiment's request records answer questions CloudWatch's aggregate view
cannot answer on its own: which scheduled requests were rejected, how long a
request waited in the API queue, whether it met the five-second target, and how
the outcome related to the request type. CloudWatch adds AWS-side evidence
about the model service. Neither source alone explains every delay.

## Statistical interpretation

The request is the unit for describing an individual response. The repeated
run is the unit for comparing workload conditions. Requests within one run
share the same process, time period, and changing system state, so do not treat
all requests as independent experimental replicates.

For each condition, the summary reports run-level bootstrap intervals for the
SLO fractions. In plain terms, it resamples the ten run-level values to show
how much the average could vary across repetitions. These intervals describe
this experiment; they are not production guarantees. An SLO classification is
withheld unless there are at least ten valid repetitions. If no rare adverse
event is observed, the result is marked inconclusive for that rare-event
target; observing zero events in a small study does not prove the true rate is
zero or below 1%.

Pooled P95 is reported as a descriptive request-level statistic with its sample
count. It should not be interpreted as a confidence interval or as independent
replication. The run-level summaries and the request-level latency distribution
answer different questions.

## First live experiment: local API, real Bedrock

This is the quickest way to begin collecting measurements from the real agent.
The AWS profile already has access to the Bedrock model and synthetic data. The
local API uses that profile through the normal AWS SDK credential chain. Keep
AWS credentials in the AWS CLI profile; do not copy access keys into `.env`.

The Strands Bedrock client uses the streaming `ConverseStream` path. The IAM
identity therefore needs both `bedrock:InvokeModel` and
`bedrock:InvokeModelWithResponseStream` on the selected inference profile and
its allowed foundation-model resources. Having a valid SSO session or seeing
`bedrock:InvokeModel` in the policy is not enough by itself. If the API log says
`not authorized to perform: bedrock:InvokeModelWithResponseStream`, add that
action to the existing model-invocation statement, keeping the same resource
scope; do not broaden the policy to `Resource: "*"`.

Set these values in the project's `.env` file, replacing the existing
`AGENT_BACKEND=simulated` line and filling in the shown AWS resource names:

```dotenv
AGENT_BACKEND=bedrock
AWS_PROFILE=ai-systems-performance-lab-sso
AWS_REGION=us-east-1
BEDROCK_MODEL_ID=us.amazon.nova-2-lite-v1:0
DYNAMODB_CASES_TABLE=ai-systems-performance-lab-cases
AWS_DATA_BUCKET=asp-lab-demo-6fe09accf18c
AGENT_SESSION_S3_BUCKET=asp-lab-demo-6fe09accf18c
LAB_CONCURRENCY=2
LAB_QUEUE_CAPACITY=4
AGENT_MAX_REQUESTS_PER_PROCESS=20
AGENT_MAX_MODEL_TURNS=3
AGENT_MAX_TOOL_CALLS=6
AGENT_MAX_OUTPUT_TOKENS=400
BEDROCK_MAX_TOKENS=512
```

The request limit is a per-process guard, not a dollar cap. Before starting the
server, run the preflight below and set this limit to at least the planned turn
count. Restart the API after changing `.env`; the process reads configuration
when it starts.

In PowerShell, refresh the SSO session and start the local API and UI:

```powershell
aws sso login --profile ai-systems-performance-lab-sso
uv run --env-file .env lab
```

Leave that terminal running. The API is on `http://127.0.0.1:8003` with the
current `.env` defaults. In a second PowerShell window, first preview a small
pilot. It compares regular and Poisson arrivals at the same 0.05 requests per
second, for three 30-second repetitions per arrival pattern:

```powershell
uv run --no-sync python -m ai_systems_performance_lab.experiments.aws_live `
  --base-url http://127.0.0.1:8003 `
  --hosting local `
  --aws-profile ai-systems-performance-lab-sso `
  --rates 0.05 `
  --arrivals deterministic poisson `
  --duration-s 30 `
  --repetitions 3
```

This preview does not contact the API or send Bedrock requests. Check the
planned turn count and estimated token cost. It excludes Fargate and public-IP
charges because the API is local; it does not exclude Bedrock, DynamoDB, S3,
CloudWatch, or other account charges. The estimate is not a spending cap.

If the preview is acceptable, make sure `AGENT_MAX_REQUESTS_PER_PROCESS` is at
least the planned turn count, restart the API if you changed `.env`, then run
the same command with `--execute --approve-cost-usd <reviewed ceiling>`. The
ceiling must be at least the printed scenario estimate. It is a runner check,
not an AWS billing cap. Do not add `--execute` until you have reviewed and
explicitly approved the estimate. Keep the API process fresh: the runner
refuses an API that has already handled agent turns.

The runner collects CloudWatch Bedrock metrics if its AWS profile has
`cloudwatch:GetMetricData`. If that read is denied, the run still saves the
request-level results and records the CloudWatch read error. You can also
inspect the model metrics in the AWS console for the same time window.

This pilot checks that the real call path, request timings, and result files
work end to end. Three repetitions are not enough to establish rare-event SLOs;
use the pilot to check the measurement process, then design a larger study.

## Before any live run: preview cost and workload

Run this from the repository root. It prints the planned workload and a
token-based cost scenario. It does not contact the app, Bedrock, or AWS.

```powershell
uv run --no-sync python -m ai_systems_performance_lab.experiments.aws_live `
  --base-url http://example.invalid `
  --hosting fargate
```

The default preview assumes at most three model calls per request, up to 10,000
input tokens per call, and up to 400 output tokens per call. At the prices
currently entered in the experiment code ($0.33 per million input tokens and
$2.75 per million output tokens), 188 planned requests produce a conservative
scenario of about $2.48 for Bedrock. The scenario also includes two hours of
one 0.5-vCPU/1-GiB Linux/x86 Fargate task and one public IPv4 address, estimated
at about $0.06. The displayed subtotal is therefore about $2.54.

This is an estimate, not a spending cap. The 10,000-token input assumption is
not enforced by the API, and actual charges depend on actual calls and token
usage. Prices can change. The subtotal excludes ECR image storage, CloudWatch
log and metric ingestion/storage, data transfer, taxes, and unrelated account
usage. Recheck [Bedrock pricing](https://aws.amazon.com/bedrock/pricing/),
[Fargate pricing](https://aws.amazon.com/fargate/pricing/), and [public IPv4
pricing](https://aws.amazon.com/vpc/pricing/) before a run. AWS Budgets can send
alerts, but it is not a guaranteed hard spending limit.

The live runner requires both `--execute` and an explicit
`--approve-cost-usd` value at least as large as the printed estimate. This is
an extra software check, not an AWS billing limit. Do not deploy the service or
send live requests until the estimate, duration, required IAM permissions, and
teardown steps have been reviewed and the live experiment has been explicitly
approved.

## What the AWS setup would create

The performance-study templates are separate from the existing synthetic-data
stack because the container image must be published before ECS can start it.
The planned steps are:

1. Check for an existing VPC and public subnet with an Internet Gateway route.
   Find your current public IPv4 address and restrict inbound access to that
   address as a `/32` range.
2. Review and deploy `infrastructure/aws/agent-ecr.yaml`. ECR is a private
   registry that stores the container image. Build and push one image tagged
   with the reviewed source commit.
3. Review and deploy `infrastructure/aws/agent-fargate.yaml`, supplying the
   image, subnet/VPC, synthetic-data table and bucket, allowed Bedrock model
   resources, and your `/32` address.
4. ECS runs one Fargate task from that image. Its task role is limited to the
   specified synthetic data, session path, and Bedrock resources. Its execution
   role pulls the image and writes the task's logs.
5. Check the task's health endpoint and use its temporary public IP as the
   browser UI's API target. The endpoint has no application login; network
   access is restricted to the specified client IP. Remove the stack after
   the study.

The current assumed AWS role previously lacked some infrastructure-read
permissions. Deployment may need narrowly scoped permissions for inspecting
VPC/subnets, pushing to this ECR repository, managing this ECS task and its
network interface/security group, passing the two task roles, reading the
project's logs/metrics, and deleting the study stacks. If AWS returns
`AccessDenied`, identify the specific missing action and grant only the
necessary scope. Do not add administrator access to work around a denial.

## Later experiment: run the API on ECS Fargate

The ECR and Fargate deployment below is a later architecture comparison. It
adds a real AWS-hosted API process, and therefore requires deployment
permissions and incurs compute/public-IP charges while the task is running.
The local Bedrock pilot above does not require this deployment.

## How to run the Fargate experiment after deployment and approval

First run the preview again with the real task URL and review the planned
request and token bounds. Then run the approved experiment. Replace the sample
IP and cost ceiling with the values reviewed for your run:

```powershell
uv run --no-sync python -m ai_systems_performance_lab.experiments.aws_live `
  --base-url http://TASK_PUBLIC_IP:8000 `
  --hosting fargate `
  --aws-profile ai-systems-performance-lab-sso `
  --execute `
  --approve-cost-usd 3.00
```

Before sending requests, the runner checks that the API reports Bedrock mode,
the configured model matches the model used for pricing and CloudWatch lookup,
and the API process has not already handled agent requests. It also checks the
planned total against its request limit. Each request gets a fresh,
S3-backed conversation session. The runner does not save prompts or model
answers.

The run writes these files under `results/aws/<experiment-id>/`:

- `manifest.json`: workload, configuration, provenance, and cost assumptions.
- `requests.csv`: request-level dispatch times, outcomes, latency, and usage.
- `summary.json`: results for each repetition.
- `study_summary.json`: comparisons and run-level uncertainty summaries.
- `cloudwatch_bedrock_metrics.json`: AWS model metrics, or a note if they
  could not be read.

The API writes prompt-free JSON records to the project log group
`/ai-systems-performance-lab/agent`, tagged with the experiment ID. Keep
request records and CloudWatch graphs aligned to the same experiment time.
CloudWatch metrics can arrive late. If a CloudWatch permission/read fails, the
runner preserves the request-level experiment results and records the failure.

## Interview explanation

> I sent a preplanned open-loop workload to one real container running our
> multi-tool agent. I compared regular and Poisson arrivals at the same average
> rates, while keeping the request mix fixed. For each request I measured
> admission, queue wait, end-to-end time, outcome, and token use. I compared
> those request-level results with Bedrock's model-level CloudWatch metrics.
> Repeated runs, rather than individual requests, are my units for uncertainty
> because requests in one run share system conditions. The result tells me how
> this one-task configuration behaved under this tested workload; it does not
> establish multi-task capacity or prove a production SLO.

The strongest conclusion is a specific, bounded observation, for example:
“At this offered rate and request mix, this one-task configuration met or
missed the stated targets across these repetitions, with these measured delays
and outcomes.” Do not claim a general capacity limit or production guarantee
from this first study.

## Stop the service and remove experiment resources

After saving the results, set the ECS service desired count to zero or delete
the Fargate stack. If the experiment created session history you want removed,
inspect only the `agent-sessions/` prefix in the existing demo bucket first.
The following commands are examples; verify the bucket and listed keys before
deleting anything:

```powershell
aws s3 ls "s3://$bucket/agent-sessions/" --recursive --profile $profile --region $region
aws s3 rm "s3://$bucket/agent-sessions/" --recursive --profile $profile --region $region
```

To delete the ECR stack, remove its remaining images first; ECR will not delete
a non-empty repository. Confirm that the task, security group, log group, and
CloudFormation stacks are gone. ECR image storage or CloudWatch data can remain
billable until deleted or expired. Keep the existing synthetic-data stack
unless you separately choose to remove it. Check Billing later because usage
data may be delayed.

## What is in scope after this first study

This first deployment has one task, one process-local queue, and a short
controlled workload. A later study could compare multiple task counts or
scaling policies, but that requires a different experiment design: requests
may land on different tasks, each task has its own local queue, startup takes
time, and Bedrock may become the limiting service. Those results must be
measured separately; they cannot be inferred from this one-task experiment.

For metric definitions, see [Bedrock runtime metrics](https://docs.aws.amazon.com/bedrock/latest/userguide/monitoring-runtime-metrics.html).
