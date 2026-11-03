# Verdant & Co - Support Copilot

Customer messages arrive by webhook, Gemini drafts a reply from the knowledge base, an agent edits and approves it.

**Stack:** FastAPI + SQLAlchemy + PostgreSQL (SQLite locally) · React (Vite) · Gemini API · deployed on Render (API), Vercel (web), Supabase (DB).
Chosen because FastAPI is quick to write and test, and Postgres lets the DB itself enforce the rules (see below).

## Run locally
```bash
# API
cd backend && python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # set GEMINI_API_KEY (free key: aistudio.google.com/apikey), AGENT_PASSWORD, JWT_SECRET
set -a && source .env && set +a
uvicorn app.main:app --reload          # http://localhost:8000
pytest                                  # 6 tests, LLM mocked

# Web
cd frontend && cp .env.example .env && npm install && npm run dev   # http://localhost:5173
```
Test the webhook:
```bash
BODY='{"event_id":"evt_1","type":"message.received","brand_id":"verdant-co","timestamp":"2026-11-03T10:15:22Z","customer":{"id":"cust_1042","name":"Aisha Khan","phone":"+919800000001"},"message":{"id":"msg_1","type":"text","text":"What is your return policy?"}}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac 'verdant-demo-secret-2026' | awk '{print $2}')
curl -X POST $API/webhook -H "Content-Type: application/json" -H "X-Signature: sha256=$SIG" -d "$BODY"
```

## Main decisions
- **One ingest function** serves both the webhook and the Customer view, so they behave identically.
- **Message first, AI second.** The message is committed before the LLM is called; the draft is generated in the background. If Gemini fails, the agent still sees the message plus a "draft failed" banner and a Retry button. Drafts left `pending` by a crash are re-queued on startup.
- **Idempotent webhook:** HMAC-SHA256 on the raw body (constant-time compare); unique `event_id` and message id, so retried deliveries are no-ops. Unsupported event types return 200 so the sender does not retry forever.
- **Data integrity in the DB:** a partial unique index guarantees one open conversation per customer even under concurrent requests. Approving locks the draft row, so a double click cannot send two replies.
- **Knowledge base in the prompt** (7 rows). Stops working when the KB no longer fits the context or gets expensive per call; then use embeddings (pgvector) and retrieve top-k.
- **Grounding:** the prompt allows only the KB, forbids stating order status, and returns `needs_human` + reason. Questions the KB cannot answer (Dubai) and health messages are flagged for the agent. A regex also forces `needs_human` for health words, because prompts are not a guarantee.
- **Cut for time:** login rate limiting, agent roles/password reset, replying without a draft, websockets (UI polls every 3s), migrations (tables are created on startup; I'd use Alembic), metrics/alerting.

## AI usage
Claude helped write the code and tests. **Where it was wrong:** its first draft saver called `db.refresh()` before saving results, which silently threw away the error message and draft text it had just set. A test (failed draft must show its error) caught it; the fix is to compute the result, re-read the status from the DB, then write.

## Questions
1. **2 a.m. failure.** The customer message is already saved, so the agent sees the conversation marked "Needs attention" with either a "draft failed" banner (reason shown) and a Retry button, or a draft left pending that is re-queued on restart. I'd find the cause in the structured logs (the draft's id and the exception) and in the `drafts.error` and `webhook_events` rows; in production I'd add error alerting (e.g. Sentry) so I find out before the agents do.
2. **20 brands.** First add `brand_id` to every table (customers, conversations, agents, knowledge base) and enforce isolation with Postgres row-level security or a mandatory tenant filter, then move each brand's KB and webhook secret into the DB. Isolation is the hardest thing to retrofit and a cross-brand data leak is the worst failure.
