import { useEffect, useRef, useState, type FormEvent } from "react";
import Markdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkMath from "remark-math";
import Experiments from "./Experiments";

type Backend = "simulated" | "bedrock" | "offline";
type Metrics = {
  offered: number;
  admitted: number;
  rejected: number;
  active: number;
  waiting: number;
  latency_ms?: { end_to_end?: { p95?: number | null } };
};
type AgentResult = {
  request_id: string;
  conversation_id: string;
  summary: string;
  execution_source: string;
  model_calls: number;
  tool_calls: number;
  timing_ms: { queue_wait: number; processing: number; end_to_end: number };
  evidence: string[];
  tool_events: { name: string; duration_ms: number; outcome: string }[];
};
type ChatMessage = { role: "user" | "assistant"; text: string; result?: AgentResult };
type Activity = { id: number; label: string; detail?: string; kind: string };
type DemoCase = { case_id: string; label: string };

const initialMetrics: Metrics = { offered: 0, admitted: 0, rejected: 0, active: 0, waiting: 0 };
const suggestedQuestions = [
  "Summarize this case and cite the supporting evidence.",
  "Calculate the DSCR and explain which inputs determine it.",
  "How does the moderate downside scenario change the findings?",
  "What does the synthetic policy say, and what are its limits?",
];

function titleForEvent(event: Record<string, unknown>): Activity {
  const type = String(event.type ?? "event");
  const name = String(event.name ?? "");
  const call = event.call ? ` · model call ${event.call}` : "";
  const labels: Record<string, string> = {
    offered: "Request arrived",
    admitted: "Admitted to service",
    agent_started: "Agent started work",
    model_call_started: "Calling the model",
    model_call_completed: "Model response received",
    model_call_failed: "Model call failed",
    tool_started: `Using tool · ${name}`,
    tool_completed: `Tool completed · ${name}`,
    tool_failed: `Tool failed · ${name}`,
    memory_unavailable: `Memory ${String(event.operation ?? "operation")} unavailable`,
    token_usage: "Token usage recorded",
    request_completed: "Request completed",
    request_failed: "Request failed",
    rejected: "Request rejected",
  };
  return {
    id: Date.now() + Math.random(),
    label: labels[type] ?? type,
    detail: type.startsWith("model_call")
      ? call.trim()
      : type === "token_usage"
        ? `input ${String(event.input_tokens ?? "?")} · output ${String(event.output_tokens ?? "?")}`
        : undefined,
    kind: type,
  };
}

export default function App() {
  const [theme, setTheme] = useState<"light" | "dark">(() =>
    document.documentElement.dataset.theme === "dark" ? "dark" : "light",
  );
  const [screen, setScreen] = useState<"agent" | "experiments">("agent");
  const [backend, setBackend] = useState<Backend>("offline");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [activity, setActivity] = useState<Activity[]>([]);
  const [metrics, setMetrics] = useState<Metrics>(initialMetrics);
  const [cases, setCases] = useState<DemoCase[]>([]);
  const [caseId, setCaseId] = useState("demo-case-01");
  const [conversationId, setConversationId] = useState("");
  const [question, setQuestion] = useState(suggestedQuestions[0]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [startingSession, setStartingSession] = useState(false);
  const activityEnd = useRef<HTMLDivElement>(null);

  function toggleTheme() {
    const next = theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = next;
    try {
      window.localStorage.setItem("ai-systems-performance-lab-theme", next);
    } catch {
      // Keep the selection for this session if browser storage is unavailable.
    }
    setTheme(next);
  }

  const refreshSystem = async () => {
    try {
      const [healthResponse, metricsResponse, casesResponse] = await Promise.all([
        fetch("/health"),
        fetch("/agent/metrics"),
        fetch("/agent/cases"),
      ]);
      if (!healthResponse.ok || !metricsResponse.ok || !casesResponse.ok) throw new Error("API is not ready");
      const health = await healthResponse.json();
      setBackend(health.inference === "bedrock" ? "bedrock" : "simulated");
      setMetrics(await metricsResponse.json());
      const availableCases = (await casesResponse.json()) as DemoCase[];
      setCases(availableCases);
      if (availableCases.length && !availableCases.some((item) => item.case_id === caseId)) {
        setCaseId(availableCases[0].case_id);
      }
    } catch {
      setBackend("offline");
      setConversationId("");
    }
  };

  async function startSession(selectedCase: string) {
    if (backend === "offline") return;
    setStartingSession(true);
    setError("");
    try {
      const response = await fetch("/agent/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ case_id: selectedCase }),
      });
      if (!response.ok) throw new Error("Could not start a conversation session.");
      const body = (await response.json()) as { conversation_id: string };
      setConversationId(body.conversation_id);
      sessionStorage.setItem(`agent-session:${selectedCase}`, body.conversation_id);
      sessionStorage.setItem(`agent-history:${body.conversation_id}`, "[]");
      setMessages([]);
      setActivity([]);
    } catch (caught) {
      setConversationId("");
      setError(caught instanceof Error ? caught.message : "Could not start a conversation.");
    } finally {
      setStartingSession(false);
    }
  }

  useEffect(() => {
    void refreshSystem();
  }, []);

  useEffect(() => {
    activityEnd.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [activity]);

  useEffect(() => {
    if (backend !== "offline" && !conversationId) {
      const stored = sessionStorage.getItem(`agent-session:${caseId}`);
      if (stored) {
        setConversationId(stored);
        try {
          setMessages(JSON.parse(sessionStorage.getItem(`agent-history:${stored}`) ?? "[]") as ChatMessage[]);
        } catch {
          setMessages([]);
        }
      }
      else void startSession(caseId);
    }
  }, [backend, caseId, conversationId]);

  useEffect(() => {
    if (conversationId) {
      sessionStorage.setItem(`agent-history:${conversationId}`, JSON.stringify(messages));
    }
  }, [conversationId, messages]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy || backend === "offline" || !question.trim()) return;
    setError("");
    setBusy(true);
    setActivity([]);
    const submittedQuestion = question.trim();
    setMessages((current) => [...current, { role: "user", text: submittedQuestion }]);

    try {
      let activeConversation = conversationId;
      if (!activeConversation) {
        const sessionResponse = await fetch("/agent/sessions", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ case_id: caseId }),
        });
        if (!sessionResponse.ok) throw new Error("Could not start the agent session.");
        activeConversation = ((await sessionResponse.json()) as { conversation_id: string }).conversation_id;
        setConversationId(activeConversation);
        sessionStorage.setItem(`agent-session:${caseId}`, activeConversation);
        sessionStorage.setItem(`agent-history:${activeConversation}`, "[]");
      }
      const response = await fetch(`/agent/sessions/${activeConversation}/turns/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ case_id: caseId, question: submittedQuestion }),
      });
      if (!response.ok || !response.body) throw new Error(`Request failed (${response.status})`);

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let receivedResult = false;
      let receivedStreamError = false;

      const handleFrame = (frame: string) => {
        const data = frame.split("\n").filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trim()).join("\n");
        if (!data) return;
        const message = JSON.parse(data) as { type: string; data?: AgentResult | { reason?: string } } & Record<string, unknown>;
        if (message.type === "result" && message.data && "summary" in message.data) {
          receivedResult = true;
          const result = message.data;
          setMessages((current) => [...current, { role: "assistant", text: result.summary, result }]);
          void refreshSystem();
        } else if (message.type === "error") {
          receivedStreamError = true;
          const reason = (message.data as { reason?: string } | undefined)?.reason;
          setError(reason === "queue_full"
            ? "The service queue is full. Try again shortly."
            : reason === "usage_budget_exhausted"
              ? "The configured Bedrock demo turn cap is reached. Review recorded token usage before restarting the API."
              : reason === "agent_timeout"
                ? "The agent exceeded its request deadline. Its stages are recorded as a failed turn."
                : reason === "provider_throttled"
                  ? "Amazon Bedrock throttled this request. Check model quotas and retry after a pause."
                  : reason === "provider_account_verification"
                    ? "AWS is still verifying this account for Bedrock. Wait for verification to finish, then restart the API before trying again."
                    : reason === "provider_access_denied"
                      ? "Amazon Bedrock denied access. Check account verification, model availability, and IAM invocation permissions."
                  : reason === "provider_unavailable"
                    ? "An AWS inference service was unavailable. Check the API logs and AWS service status."
              : "The agent request failed. Check the API terminal for details.");
        } else {
          setActivity((current) => [...current, titleForEvent(message)].slice(-40));
        }
      };

      while (true) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value, { stream: !done });
        const frames = buffer.split("\n\n");
        buffer = frames.pop() ?? "";
        frames.forEach(handleFrame);
        if (done) break;
      }
      if (buffer.trim()) handleFrame(buffer);
      if (!receivedResult && !receivedStreamError) setError("The stream ended before a result arrived.");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Unable to reach the API.");
    } finally {
      setBusy(false);
      void refreshSystem();
    }
  }

  const backendLabel = backend === "bedrock" ? "Bedrock · configured" : backend === "simulated" ? "Simulated agent · no LLM" : "API offline";

  if (screen === "experiments") return <Experiments onBack={() => setScreen("agent")} theme={theme} onToggleTheme={toggleTheme} />;

  return (
    <main className="app-shell">
      <header className="topbar">
        <a className="brand" href="#top" aria-label="AI Systems Performance Lab home">
          <span className="brand-mark">A</span><span><strong>AI Systems</strong><small>PERFORMANCE LAB</small></span>
        </a>
        <div className="topbar-right"><span className={`backend-pill ${backend}`}><i />{backendLabel}</span><button className="theme-toggle" type="button" onClick={toggleTheme} aria-pressed={theme === "dark"} aria-label={`Switch to ${theme === "light" ? "dark" : "light"} theme`}>{theme === "light" ? "Dark" : "Light"}</button><span className="region-label">INTERACTIVE AGENT</span></div>
      </header>

      <section className="workspace" id="top">
        <aside className="left-rail">
          <div className="rail-heading">WORKSPACE</div>
          <button className="nav-item selected" onClick={() => setScreen("agent")}><span>◉</span> Case analyst</button>
          <button className="nav-item" onClick={() => setScreen("experiments")}><span>⌁</span> Performance lab</button>
          <div className="rail-card"><div className="rail-card-top"><span className="tiny-dot" /> CONVERSATION</div><strong>Evidence review</strong><p>Ask follow-up questions and watch the agent choose its tools.</p></div>
          <div className="rail-note"><span className="note-icon">↗</span><div><strong>Systems lab</strong><p>Service metrics are measured outside the agent's decision loop.</p></div></div>
          <div className="rail-footer">SYNTHETIC DATA · NO CREDIT DECISIONS</div>
        </aside>

        <section className="conversation-panel" aria-label="Agent conversation">
          <div className="conversation-heading">
            <div><div className="eyebrow">SYNTHETIC CASE WORKFLOW</div><h1>Ask the review agent</h1></div>
            <span className="status-chip"><i /> {backend === "bedrock" ? "BEDROCK MODE" : backend === "simulated" ? "SIMULATED MODE" : "WAITING FOR API"}</span>
          </div>

          <div className="chat-scroll">
            {messages.length === 0 ? (
              <div className="welcome-card">
                <div className="welcome-icon">✳</div><div className="eyebrow">A MULTI-TOOL AGENT DEMO</div>
                <h2>See the work behind the answer.</h2>
                <p>Ask about a synthetic case. The model can retrieve facts, search evidence, calculate ratios, stress assumptions, or read policy as needed.</p>
                <div className="suggestions">{suggestedQuestions.map((suggestion) => <button key={suggestion} onClick={() => setQuestion(suggestion)}>{suggestion}</button>)}</div>
              </div>
            ) : messages.map((message, index) => (
              <article className={`message ${message.role}`} key={`${message.role}-${index}`}>
                <div className="avatar">{message.role === "user" ? "Y" : "A"}</div>
                <div className="message-content"><div className="message-name">{message.role === "user" ? "YOU" : "REVIEW AGENT"}</div>{message.role === "user" ? <p>{message.text}</p> : <div className="message-markdown"><Markdown remarkPlugins={[remarkMath]} rehypePlugins={[rehypeKatex]} skipHtml>{message.text}</Markdown></div>}{message.result && <ResultCard result={message.result} />}</div>
              </article>
            ))}
            {busy && <article className="message assistant pending"><div className="avatar">A</div><div className="message-content"><div className="message-name">REVIEW AGENT</div><p className="working-text">Working through the request<span className="pulse-dots">•••</span></p></div></article>}
          </div>

          {error && <div className="error-banner" role="alert">{error}</div>}
          <form className="composer" onSubmit={submit}>
            <label htmlFor="question">YOUR QUESTION</label>
            <textarea id="question" value={question} onChange={(event) => setQuestion(event.target.value)} maxLength={1200} rows={2} disabled={busy || backend === "offline" || startingSession} />
            <div className="composer-bottom"><span>{startingSession ? "Starting case session…" : "Ask a follow-up in the same conversation"}</span><button className="send-button" type="submit" disabled={busy || backend === "offline" || startingSession || !question.trim()}>{busy ? <><span className="button-spinner" /> Working</> : <>Run agent <span>↑</span></>}</button></div>
          </form>
          <div className="privacy-line">Synthetic values only · Agent output is advisory · Simulated mode makes no model calls</div>
        </section>

        <aside className="right-rail">
          <section className="side-section">
            <div className="side-title"><span>CASE</span><span className="tiny-label">SYNTHETIC</span></div>
            <label className="field-label" htmlFor="case-id">Select case</label>
            <select id="case-id" value={caseId} onChange={(event) => { const next = event.target.value; setCaseId(next); setConversationId(""); setMessages([]); setActivity([]); }} disabled={busy || startingSession}>
              {cases.map((item) => <option key={item.case_id} value={item.case_id}>{item.label}</option>)}
            </select>
            <button className="reset-session" type="button" onClick={() => { sessionStorage.removeItem(`agent-session:${caseId}`); setConversationId(""); setMessages([]); setActivity([]); }} disabled={busy || startingSession || backend === "offline"}>Start a fresh conversation</button>
          </section>

          <section className="side-section activity-section">
            <div className="side-title"><span>LIVE AGENT ACTIVITY</span><span className={busy ? "live-tag active" : "live-tag"}>{busy ? "LIVE" : "IDLE"}</span></div>
            {activity.length === 0 ? <div className="activity-empty"><span className="activity-empty-icon">⌁</span><p>Model calls and selected tool activity appear here. Hidden reasoning is not exposed.</p></div> : <ol className="activity-list" aria-live="polite">{activity.map((item) => <li key={item.id} className={`activity-item ${item.kind}`}><span className="activity-node" /><div><span>{item.label}</span>{item.detail && <small>{item.detail}</small>}</div></li>)}<div ref={activityEnd} /></ol>}
          </section>

          <section className="side-section metrics-section">
            <div className="side-title"><span>SERVICE SNAPSHOT</span><button className="refresh-button" onClick={() => void refreshSystem()} title="Refresh metrics" aria-label="Refresh metrics">↻</button></div>
            <div className="metric-grid"><Metric label="OFFERED" value={metrics.offered} /><Metric label="IN SERVICE" value={metrics.active} /><Metric label="WAITING" value={metrics.waiting} /><Metric label="REJECTED" value={metrics.rejected} warning={metrics.rejected > 0} /></div>
            <div className="p95-row"><span>Observed end-to-end P95</span><strong>{metrics.latency_ms?.end_to_end?.p95 == null ? "—" : `${metrics.latency_ms.end_to_end.p95.toFixed(1)} ms`}</strong></div>
            <div className="scope-caption">PROCESS-LOCAL · COMPLETED TURNS</div>
          </section>

          <div className="mode-note"><strong>{backend === "bedrock" ? "Live model path" : "Safe demo path"}</strong><p>{backend === "bedrock" ? "Bedrock calls may incur token charges. AWS tools are read-only." : backend === "simulated" ? "The simulated agent follows a controlled workflow without calling an LLM or AWS." : "Start the FastAPI server, then refresh."}</p></div>
        </aside>
      </section>
    </main>
  );
}

function ResultCard({ result }: { result: AgentResult }) {
  return <div className="result-card"><div className="result-badges"><span className="result-label">AGENT RESPONSE</span><span className="outcome-badge">{result.execution_source.includes("bedrock") ? "BEDROCK" : "SIMULATED"}</span></div><details><summary>Evidence, tools, and execution details</summary><p className="source-label">{result.execution_source} · {result.model_calls} model calls · {result.tool_calls} tool calls</p><ul>{result.evidence.map((item) => <li key={item}>{item}</li>)}</ul><div className="tool-summary">{result.tool_events.map((event, index) => <span key={`${event.name}-${index}`}>{event.name} <b>{event.duration_ms.toFixed(1)} ms</b></span>)}</div><div className="timing-breakdown"><span>Queue {result.timing_ms.queue_wait.toFixed(1)} ms</span><span>Processing {result.timing_ms.processing.toFixed(1)} ms</span><span>End to end {result.timing_ms.end_to_end.toFixed(1)} ms</span></div></details></div>;
}

function Metric({ label, value, warning = false }: { label: string; value: number; warning?: boolean }) {
  return <div className={`metric ${warning ? "warning" : ""}`}><span>{label}</span><strong>{value}</strong></div>;
}
