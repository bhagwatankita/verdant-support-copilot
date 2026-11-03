import { useCallback, useEffect, useRef, useState } from "react";
import { api, getToken, setToken } from "./api.js";

const SAMPLES = [
  "Hi, my order #4471 hasn't arrived yet. Can I change the delivery address?",
  "What is your return policy?",
  "Do you deliver to Dubai?",
  "Is cash on delivery available for a ₹3,500 order?",
  "I think I'm allergic to the serum I bought. What should I do?",
];

const fmt = (iso) =>
  new Date(iso).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

/** Re-runs `fn` every `ms` and returns its latest result / error. */
function usePolling(fn, deps, ms = 3000) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const saved = useRef(fn);
  saved.current = fn;
  useEffect(() => {
    let alive = true;
    const run = async () => {
      try {
        const d = await saved.current();
        if (alive) { setData(d); setError(""); }
      } catch (e) {
        if (alive) setError(e.message);
      }
    };
    run();
    const id = setInterval(run, ms);
    return () => { alive = false; clearInterval(id); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return { data, error };
}

export default function App() {
  const [mode, setMode] = useState("customer");
  return (
    <div className="app">
      <header>
        <strong>Verdant &amp; Co · Support</strong>
        <div className="toggle" role="tablist">
          {["customer", "agent"].map((m) => (
            <button key={m} className={mode === m ? "on" : ""} onClick={() => setMode(m)}>
              {m === "customer" ? "Customer view" : "Agent view"}
            </button>
          ))}
        </div>
      </header>
      {mode === "customer" ? <CustomerView /> : <AgentView />}
    </div>
  );
}

/* ---------------- Customer view ---------------- */
function CustomerView() {
  const [tests, setTests] = useState([]);
  const [sel, setSel] = useState("");
  const [custom, setCustom] = useState({ name: "", phone: "" });
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [err, setErr] = useState("");
  const [customId, setCustomId] = useState("");

  useEffect(() => {
    api("/api/customer/test-customers").then((t) => { setTests(t); setSel(t[0]?.id || "custom"); }).catch((e) => setErr(e.message));
  }, []);

  const isCustom = sel === "custom";
  const current = isCustom ? { id: customId, ...custom } : tests.find((t) => t.id === sel);
  const convId = current?.id;
  const { data: messages, error: pollErr } = usePolling(
    () => (convId ? api(`/api/customer/${encodeURIComponent(convId)}/messages`) : Promise.resolve([])),
    [convId]
  );

  const send = async (e) => {
    e.preventDefault();
    if (!current || !text.trim()) return;
    setSending(true); setErr("");
    try {
      const r = await api("/api/customer/messages", {
        method: "POST",
        body: { customer_id: isCustom ? undefined : current.id, name: current.name, phone: current.phone, text },
      });
      if (isCustom) setCustomId(r.customer_id);
      setText("");
    } catch (e2) { setErr(e2.message); } finally { setSending(false); }
  };

  return (
    <main className="card narrow">
      <h2>Chat as a customer</h2>
      <div className="row">
        <select value={sel} onChange={(e) => setSel(e.target.value)}>
          {tests.map((t) => <option key={t.id} value={t.id}>{t.name} ({t.phone})</option>)}
          <option value="custom">Other (type name and phone)…</option>
        </select>
        {isCustom && (
          <>
            <input placeholder="Name" value={custom.name} onChange={(e) => setCustom({ ...custom, name: e.target.value })} />
            <input placeholder="+919800000009" value={custom.phone} onChange={(e) => setCustom({ ...custom, phone: e.target.value })} />
          </>
        )}
      </div>

      <div className="thread">
        {(messages || []).length === 0 && <p className="muted">No messages yet. Send one below.</p>}
        {(messages || []).map((m) => (
          <div key={m.id} className={`bubble ${m.sender === "customer" ? "me" : "them"}`}>
            <div>{m.text}</div>
            <small>{m.sender === "agent" ? "Verdant support · " : ""}{fmt(m.created_at)}</small>
          </div>
        ))}
      </div>

      <div className="samples">
        {SAMPLES.map((s, i) => <button key={i} type="button" className="chip" onClick={() => setText(s)}>{s}</button>)}
      </div>
      <form onSubmit={send} className="row">
        <textarea rows={2} maxLength={2000} placeholder="Type a message…" value={text} onChange={(e) => setText(e.target.value)} />
        <button disabled={sending || !text.trim() || !current || (isCustom && (!custom.name.trim() || !custom.phone.trim()))}>
          {sending ? "Sending…" : "Send"}
        </button>
      </form>
      {(err || pollErr) && <p className="error">{err || pollErr}</p>}
    </main>
  );
}

/* ---------------- Agent view ---------------- */
function AgentView() {
  const [signedIn, setSignedIn] = useState(!!getToken());
  useEffect(() => {
    const h = () => setSignedIn(false);
    window.addEventListener("signed-out", h);
    return () => window.removeEventListener("signed-out", h);
  }, []);
  return signedIn ? <Inbox onSignOut={() => { setToken(null); setSignedIn(false); }} /> : <Login onDone={() => setSignedIn(true)} />;
}

function Login({ onDone }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (e) => {
    e.preventDefault(); setBusy(true); setErr("");
    try {
      const r = await api("/api/auth/login", { method: "POST", body: { email, password } });
      setToken(r.token); onDone();
    } catch (e2) { setErr(e2.message); } finally { setBusy(false); }
  };
  return (
    <main className="card narrow">
      <h2>Agent sign in</h2>
      <form onSubmit={submit} className="col">
        <input type="email" placeholder="Email" value={email} onChange={(e) => setEmail(e.target.value)} required />
        <input type="password" placeholder="Password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        <button disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
        {err && <p className="error">{err}</p>}
      </form>
    </main>
  );
}

function Inbox({ onSignOut }) {
  const [openId, setOpenId] = useState(null);
  const { data: convs, error } = usePolling(() => api("/api/agent/conversations", { auth: true }), []);
  return (
    <div className="split">
      <aside className={`card list ${openId ? "hide-sm" : ""}`}>
        <div className="row between"><h2>Conversations</h2><button className="link" onClick={onSignOut}>Sign out</button></div>
        {error && <p className="error">{error}</p>}
        {convs && convs.length === 0 && <p className="muted">No conversations yet. Send a message from the Customer view.</p>}
        {(convs || []).map((c) => (
          <button key={c.id} className={`item ${openId === c.id ? "active" : ""}`} onClick={() => setOpenId(c.id)}>
            <div className="row between">
              <strong>{c.customer_name}</strong><small>{fmt(c.last_activity_at)}</small>
            </div>
            <div className="preview">{c.last_sender === "agent" ? "You: " : ""}{c.preview}</div>
            <div className="tags">
              {c.awaiting_reply && <span className="tag">Awaiting reply</span>}
              {c.needs_attention && <span className="tag warn">Needs attention</span>}
            </div>
          </button>
        ))}
      </aside>
      <section className={`${openId ? "" : "hide-sm"}`}>
        {openId ? <Conversation id={openId} onBack={() => setOpenId(null)} /> : <div className="card"><p className="muted">Select a conversation.</p></div>}
      </section>
    </div>
  );
}

function Conversation({ id, onBack }) {
  const { data, error } = usePolling(() => api(`/api/agent/conversations/${id}`, { auth: true }), [id], 2500);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const draft = data?.draft;

  // Only reset the editor when a different draft arrives or it becomes ready; never while the agent is typing.
  useEffect(() => { setText(draft?.text || ""); setErr(""); }, [draft?.id, draft?.status]); // eslint-disable-line

  const act = async (fn) => {
    setBusy(true); setErr("");
    try { await fn(); } catch (e) { setErr(e.message); } finally { setBusy(false); }
  };
  const approve = () => act(() => api(`/api/agent/drafts/${draft.id}/approve`, { method: "POST", body: { text }, auth: true }));
  const retry = () => act(() => api(`/api/agent/drafts/${draft.id}/retry`, { method: "POST", auth: true }));

  if (!data) return <div className="card"><p className="muted">{error || "Loading…"}</p></div>;
  return (
    <div className="card">
      <div className="row between">
        <h2>{data.customer.name} <small className="muted">{data.customer.phone}</small></h2>
        <button className="link show-sm" onClick={onBack}>Back</button>
      </div>
      <div className="thread">
        {data.messages.map((m) => (
          <div key={m.id} className={`bubble ${m.sender === "customer" ? "them" : "me"}`}>
            <div>{m.text}</div><small>{m.sender === "agent" ? "Agent · " : ""}{fmt(m.created_at)}</small>
          </div>
        ))}
      </div>

      {draft ? (
        <div className="draft">
          <label><strong>AI draft</strong> (edit before approving. It is never sent automatically)</label>
          {draft.status === "pending" && <p className="muted">Drafting a reply…</p>}
          {draft.status === "failed" && (
            <div className="banner warn">
              The AI draft could not be generated, so please write a reply manually or retry.
              <div><small>{draft.error}</small></div>
              <button onClick={retry} disabled={busy}>Retry draft</button>
            </div>
          )}
          {draft.status === "ready" && draft.needs_human && (
            <div className="banner warn"><strong>Needs a human decision.</strong> {draft.reason}</div>
          )}
          {draft.status === "ready" && !draft.needs_human && draft.reason && <p className="muted"><small>{draft.reason}</small></p>}
          <textarea rows={5} maxLength={2000} value={text} disabled={draft.status === "pending"} onChange={(e) => setText(e.target.value)} />
          <button onClick={approve} disabled={busy || !text.trim() || draft.status === "pending"}>{busy ? "Saving…" : "Approve & save reply"}</button>
        </div>
      ) : (
        <p className="muted">All caught up. A new customer message will bring a new draft.</p>
      )}
      {(err || error) && <p className="error">{err || error}</p>}
    </div>
  );
}
