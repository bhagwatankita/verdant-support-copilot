# Verdant & Co - Support Copilot

Customer messages arrive by webhook, Gemini drafts a reply from the brand's policies, and a human agent edits and approves it. Nothing is ever sent automatically.

**Live app:** https://verdant-support-beta.vercel.app (Customer view / Agent view toggle at the top)
**Webhook:** `POST https://verdant-support-copilot.onrender.com/webhook` (header `X-Signature: sha256=<hex>`, HMAC-SHA256 of the raw body, secret `verdant-demo-secret-2026`)
**Agent login:** given in the submission form.
> Hosted on free tiers: if nobody has used it for ~15 minutes, the API sleeps and the first request can take about 50 seconds.

**Stack:** FastAPI + SQLAlchemy + PostgreSQL (Supabase; SQLite locally) · React (Vite) · Gemini API · Render (API), Vercel (web). I chose FastAPI because it is quick to write and test, and Postgres so the database itself can enforce the rules below.

## Run locally
```bash
# API (Windows: .\venv\Scripts\activate)
cd backend && python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # set GEMINI_API_KEY (free key from aistudio.google.com/apikey), AGENT_PASSWORD, JWT_SECRET
uvicorn app.main:app --reload      # http://localhost:8000  (.env is loaded automatically)
python -m pytest                   # 6 tests, LLM mocked

# Web
cd frontend && cp .env.example .env && npm install && npm run dev   # http://localhost:5173
```
Test the webhook (PowerShell; replace `$api` with the live URL or `http://localhost:8000`, and use a new event/message id each time):
```powershell
$api="http://localhost:8000"; $secret="verdant-demo-secret-2026"
$body='{"event_id":"evt_1","type":"message.received","brand_id":"verdant-co","timestamp":"2026-11-03T10:15:22Z","customer":{"id":"cust_1042","name":"Aisha Khan","phone":"+919800000001"},"message":{"id":"msg_1","type":"text","text":"What is your return policy?"}}'
$bytes=[Text.Encoding]::UTF8.GetBytes($body)
$h=New-Object System.Security.Cryptography.HMACSHA256; $h.Key=[Text.Encoding]::UTF8.GetBytes($secret)
$sig="sha256="+(($h.ComputeHash($bytes)|%{$_.ToString("x2")}) -join "")
Invoke-RestMethod -Method Post -Uri "$api/webhook" -ContentType "application/json" -Headers @{"X-Signature"=$sig} -Body $bytes
```
Same call in bash: `SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')`, then `curl -X POST $API/webhook -H "X-Signature: sha256=$SIG" -H "Content-Type: application/json" -d "$BODY"`. Sending the same event again returns `duplicate`.

## Main decisions
- **One ingest function** serves both the webhook and the Customer view, so they behave identically.
- **Message first, AI second.** The message is committed before the LLM is called; the draft is generated in the background. If Gemini fails, the agent still sees the message plus a "draft failed" banner and a Retry button. Drafts left `pending` by a restart are re-queued on startup.
- **Idempotent, verified webhook:** HMAC-SHA256 on the raw body (constant-time compare); unique `event_id` and message id make retried deliveries no-ops. Unsupported event types return 200 so the sender does not retry forever.
- **Integrity in the DB:** a partial unique index guarantees one open conversation per customer under concurrent requests. Approving locks the draft row, so a double click cannot create two replies.
- **Security:** Row Level Security is enabled on all Supabase tables (the API connects directly as the owner, so Supabase's public Data API is locked out). Customers can only read their own messages, never drafts.
- **Knowledge base in the prompt** (7 rows, no retrieval). This stops working when the KB no longer fits the context window or gets expensive per call; then I would use embeddings (pgvector) and retrieve the top few policies.
- **Grounding:** the prompt allows only the KB, forbids stating order status, and returns structured output (`reply`, `needs_human`, `reason`). Questions the KB cannot answer (e.g. Dubai) and health messages are flagged for a human. A regex also forces `needs_human` for health words, because prompts are not a guarantee.
- **LLM resilience:** low thinking level for speed, a short timeout, one quick retry on overload (503/429), then a fallback model.
- **Cut for time:** login rate limiting, agent roles and password reset, replying without a draft, websockets (UI polls every few seconds), a durable job queue (drafts use FastAPI background tasks), migrations (tables are created on startup; I would use Alembic), metrics and alerting.

## AI usage
Claude helped write the code, tests and deployment steps. Where it was wrong:
1. Its first draft saver called `db.refresh()` before saving results, silently discarding the error message and draft text it had just set. A test (a failed draft must show its error) caught it; the fix is to compute the result, re-read the status from the DB, then write.
2. It suggested `gemini-2.5-flash`, which Google no longer offers to new API users (found via the error shown in the agent UI). I switched to `gemini-3.8-flash` with a fallback model. It also assumed `.env` loaded automatically (it did not) and gave bash-only test commands that failed on Windows PowerShell.

## Questions
1. **2 a.m. failure.** The customer message is already saved, so the agent sees the conversation marked "Needs attention" with either a "draft failed" banner (reason shown) and a Retry button, or a draft left pending that is re-queued on restart. I would find the cause in the logs (draft id and exception, with timing per Gemini call) and in the `drafts.error` and `webhook_events` rows; in production I would add error alerting (e.g. Sentry) so I find out before the agents do.
2. **20 brands.** First add `brand_id` to every table (customers, conversations, agents, knowledge base) and enforce isolation with Postgres row-level security or a mandatory tenant filter, then move each brand's KB and webhook secret into the database. Isolation is the hardest thing to retrofit, and a cross-brand data leak is the worst failure.