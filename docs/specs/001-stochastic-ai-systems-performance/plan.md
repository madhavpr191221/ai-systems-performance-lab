# Staged plan: AI Systems Performance Lab

**Status:** Agent-first implementation in progress. The agent is a real
workload; stochastic systems behavior and SLO compliance remain the research
focus.

## Build the agent first

### Iteration 1 — Interaction contract and demo data

Define the case analyst's question types, case/evidence schema, source citation
format, tool permissions, session boundary, UI events, and failure language.
Use only seeded synthetic data. The agent cannot make a real credit decision or
write business records.

### Iteration 2 — Model-driven local agent

Run Strands with Amazon Bedrock, allow the model to choose bounded tools, and
stream observable model/tool status into the existing React UI. Keep the
process-local admission queue outside the agent. Apply model-turn, output-token,
tool-call, request-timeout, and user-request bounds.

### Iteration 3 — AWS data tools and memory

Read current case values from DynamoDB; read cited evidence and policy from S3;
calculate ratios/stresses in deterministic Python; persist conversation history
to an S3 prefix; add optional AgentCore Memory for same-case context. Use
least-privilege roles and never allow model-selected table names or S3 keys.

### Iteration 4 — Failure handling and interview readiness

Exercise missing cases, unavailable evidence/policy, provider throttling,
timeouts, memory degradation, and exhausted budgets. Show failed stages and
partial answers honestly. Keep the simulated-agent path and browser transcript fallback
so the demo remains usable without AWS connectivity.

### Iteration 5 — Fargate package and controlled smoke path

Build the same API as a non-root container. Prepare one private, short-lived
ECS Fargate task path with task-role permissions and CloudWatch logs. No public
endpoint or always-on service. Estimate all charges and obtain separate
authorization before running it.

## Agent completion gate

Stochastic analysis starts after all of the following are true:

1. The React UI supports follow-up questions in a stable case-bound session.
2. In Bedrock mode, the model selects tools based on the question rather than
   following a fixed scripted order.
3. Case facts come from DynamoDB; S3 evidence/policy returns stable citations;
   deterministic calculations are independently verifiable.
4. S3 session state restores the conversation and optional AgentCore Memory
   remains same-case, supplemental context.
5. The UI streams tool activity without exposing hidden reasoning.
6. Mocked tests cover source failures, case isolation, and tool/model budgets;
   a small authorized live check is separately cost-estimated and labeled.
7. The simulated-agent path works offline and the container builds without credentials.

## Stochastic analysis after the gate

1. **Arrival variability:** deterministic versus Poisson arrivals at matched
   mean rate and equivalent request paths; compare occupancy, rejection,
   deadline-goodput, and end-to-end latency.
2. **Agent service variability:** short/long case mixtures and variable tool
   paths under the same offered trace; measure tail latency and bottleneck
   location.
3. **Finite capacity and burst:** vary concurrency `c`, waiting slots `K`, and
   burst intensity; quantify when more buffering changes rejection into
   excessive waiting.

Each experiment records question, hypothesis, seed, workload, controls,
configuration, deadline/SLO, observation boundary, results, uncertainty, and
the resulting architecture decision. Keep simulated, local, and AWS evidence
clearly distinguished.

## AWS and cost gates

- The first small Bedrock/Memory integration targets the agreed $5 ceiling;
  calculate expected model token and memory usage for the selected model first.
- Model availability, quotas, region, and current pricing are checked before
  selecting a model or issuing calls.
- ECS Fargate is a separate cost estimate and deployment authorization.
- No public ingress, always-on infrastructure, or high-volume paid inference
  is included in this stage.
- No AWS resources are provisioned by this plan or its local tests.
