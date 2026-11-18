# Implementation checklist

## Agent build before stochastic analysis

- [x] Keep synthetic commercial-credit analysis as the workload, not project subject.
- [x] Keep service metrics outside the agent's decision loop.
- [x] Preserve the deterministic simulated `/review` workload.
- [x] Add a multi-turn chat UI and case-bound session/turn API.
- [x] Add Strands model-selected bounded tools for Bedrock mode.
- [x] Add exact-key DynamoDB case reads and bounded S3 evidence/policy reads.
- [x] Add S3-backed conversation sessions and optional same-case AgentCore Memory.
- [x] Add synthetic data fixtures and a dry-run-first AWS seed utility.
- [x] Add a non-root container handoff for a short private Fargate run.
- [x] Add mocked tests for tool selection, data adapters, session binding, streamed UI path, and existing queue accounting.
- [ ] Select an account-enabled Bedrock model after regional access/quota/pricing review.
- [ ] Provision synthetic AWS resources only after cost/security review and explicit authorization.
- [ ] Execute one small Bedrock integration within the $5 target and verify memory/session behavior.
- [ ] Build the container and run a separate, authorized private Fargate smoke test.
- [ ] Verify failure behavior against live AWS errors and record limitations.

## Gate to stochastic work

- [ ] Bedrock chat works through the UI with a model-selected tool path.
- [ ] Same-case follow-up session persists; optional AgentCore Memory is supplemental and isolated.
- [ ] Evidence citations and deterministic calculations can be verified against source records.
- [ ] Error paths and service timing are visible; simulated-agent mode works offline.
- [ ] Live integration evidence is labeled accurately, with actual spend recorded.

## Stochastic work begins after the gate

- [x] Specify the first local workload study, hypotheses, 500 ms diagnostic deadline, observation window, and reproducible run settings. (This is not a production SLO.)
- [x] Compare deterministic and Poisson arrivals at matched nominal mean rates.
- [x] Compare finite queue sizes and application concurrency using the simulated agent.
- [x] Verify sample-path conservation and report rejection, failure, unfinished work, and latency populations separately.
- [ ] Compare empirically calibrated variable inference/service-time distributions; the current simulated workload matrix is an initial controlled study.
- [ ] Repeat the study with a small, explicitly budgeted real-Bedrock sample; do not use Bedrock for load generation.
