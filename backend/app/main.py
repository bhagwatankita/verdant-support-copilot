import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from dotenv import load_dotenv
load_dotenv()
from . import llm
from .models import (Agent, Base, Conversation, Customer, Draft, Message,
                     SessionLocal, WebhookEvent, engine, now)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("app")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "verdant-demo-secret-2026")
BRAND_ID = os.getenv("BRAND_ID", "verdant-co")
JWT_SECRET = os.getenv("JWT_SECRET", "dev-only-change-me")
MAX_TEXT = 2000
OPEN_DRAFT = ("pending", "ready", "failed")
# Safety net on top of the prompt: health words always go to a human.
HEALTH_RE = re.compile(r"allerg|rash|reaction|swell|burn|itch|irritat|hives|infect", re.I)

TEST_CUSTOMERS = [
    {"id": "cust_1042", "name": "Aisha Khan", "phone": "+919800000001"},
    {"id": "cust_1043", "name": "Rohan Mehta", "phone": "+919800000002"},
    {"id": "cust_1044", "name": "Priya Nair", "phone": "+919800000003"},
]


# ---------- setup ----------
def hash_pw(pw: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    return salt.hex() + "$" + hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000).hex()


def check_pw(pw: str, stored: str) -> bool:
    salt, _ = stored.split("$")
    return hmac.compare_digest(hash_pw(pw, bytes.fromhex(salt)), stored)


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(engine)
    email, pw = os.getenv("AGENT_EMAIL", "agent@verdant.test"), os.getenv("AGENT_PASSWORD", "change-me")
    with SessionLocal() as db:
        if not db.scalar(select(Agent).where(Agent.email == email)):
            db.add(Agent(email=email, password_hash=hash_pw(pw)))
            db.commit()
        pending = [d for d in db.scalars(select(Draft.id).where(Draft.status == "pending"))]
    # Recover drafts left pending by a crash/restart.
    for d in pending:
        threading.Thread(target=generate_draft, args=(d,), daemon=True).start()
    yield


app = FastAPI(title="Verdant support copilot", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",")],
    allow_methods=["*"], allow_headers=["*"],
)


def get_db():
    with SessionLocal() as db:
        yield db


def iso(dt: datetime) -> str:
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()


# ---------- core: one ingest path for webhook + customer view ----------
def ingest_message(db: Session, *, event_id: str, raw: str, ext_customer_id: str, name: str,
                   phone: str, ext_msg_id: str, text: str) -> tuple[str, int | None]:
    """Save customer message and create a pending draft. Returns (status, draft_id)."""
    try:
        db.add(WebhookEvent(event_id=event_id, payload=raw))
        db.flush()
    except IntegrityError:  # same event delivered twice
        db.rollback()
        return "duplicate", None
    if db.scalar(select(Message.id).where(Message.external_id == ext_msg_id)):
        db.rollback()
        return "duplicate", None

    cust = db.scalar(select(Customer).where(Customer.external_id == ext_customer_id))
    if cust:
        cust.name, cust.phone = name, phone
    else:
        cust = Customer(external_id=ext_customer_id, name=name, phone=phone)
        db.add(cust)
        db.flush()

    conv = db.scalar(select(Conversation).where(Conversation.customer_id == cust.id, Conversation.status == "open"))
    if not conv:
        try:
            with db.begin_nested():
                conv = Conversation(customer_id=cust.id)
                db.add(conv)
                db.flush()
        except IntegrityError:  # concurrent request created it first
            conv = db.scalar(select(Conversation).where(Conversation.customer_id == cust.id, Conversation.status == "open"))

    msg = Message(conversation_id=conv.id, sender="customer", text=text, external_id=ext_msg_id)
    db.add(msg)
    db.flush()
    draft = Draft(message_id=msg.id)
    db.add(draft)
    conv.last_preview, conv.last_sender, conv.last_activity_at = text[:200], "customer", now()
    db.commit()
    return "accepted", draft.id


def generate_draft(draft_id: int) -> None:
    """Runs in the background. Never raises: failure is stored on the draft for the agent to see."""
    with SessionLocal() as db:
        d = db.get(Draft, draft_id)
        if not d or d.status not in ("pending", "failed"):
            return
        msg = db.get(Message, d.message_id)
        cust = db.get(Customer, db.get(Conversation, msg.conversation_id).customer_id)
        rows = db.scalars(select(Message).where(Message.conversation_id == msg.conversation_id, Message.id <= msg.id)
                          .order_by(Message.id.desc()).limit(8)).all()[::-1]
        history = [(m.sender, m.text) for m in rows]
        attempts = d.attempts + 1
        result: dict = {}
        try:
            out = llm.generate(cust.name, history)
            result = {"text": out["reply"], "needs_human": out["needs_human"], "reason": out["reason"],
                      "error": "", "status": "ready"}
            if HEALTH_RE.search(msg.text):
                result["needs_human"] = True
                result["reason"] = (result["reason"] + " [auto-flagged: possible health issue]").strip()
        except Exception as e:  # noqa: BLE001
            log.exception("draft %s failed", draft_id)
            result = {"error": str(e)[:500], "status": "failed"}
        # Re-read status from the DB: the agent may have replied while the LLM was running.
        current = db.scalar(select(Draft.status).where(Draft.id == draft_id))
        if current in ("pending", "failed"):
            for k, v in result.items():
                setattr(d, k, v)
            d.attempts = attempts
            db.commit()


# ---------- webhook ----------
class WCustomer(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    phone: str = Field(min_length=3, max_length=30)


class WMessage(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    type: str
    text: str | None = None


class WebhookIn(BaseModel):
    event_id: str = Field(min_length=1, max_length=100)
    type: str
    brand_id: str
    timestamp: str | None = None
    customer: WCustomer
    message: WMessage


@app.post("/webhook")
async def webhook(request: Request, bg: BackgroundTasks):
    raw = await request.body()
    expected = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    given = request.headers.get("x-signature", "")
    if not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(401, "invalid signature")
    try:
        ev = WebhookIn.model_validate_json(raw)
    except ValidationError as e:
        raise HTTPException(400, f"invalid payload: {e.errors()[0]['loc']}")
    if ev.brand_id != BRAND_ID:
        raise HTTPException(400, "unknown brand_id")
    text = (ev.message.text or "").strip()
    if ev.type != "message.received" or ev.message.type != "text" or not text:
        return {"status": "ignored"}  # 2xx so the sender doesn't retry forever
    if len(text) > MAX_TEXT:
        raise HTTPException(400, f"text longer than {MAX_TEXT} characters")

    def work() -> tuple[str, int | None]:
        with SessionLocal() as db:
            return ingest_message(db, event_id=ev.event_id, raw=raw.decode(errors="replace"),
                                  ext_customer_id=ev.customer.id, name=ev.customer.name.strip(),
                                  phone=ev.customer.phone.strip(), ext_msg_id=ev.message.id, text=text)

    status, draft_id = await run_in_threadpool(work)
    if draft_id:
        bg.add_task(generate_draft, draft_id)
    return {"status": status}


# ---------- customer view (no auth; test tool) ----------
class CustomerSend(BaseModel):
    customer_id: str | None = None
    name: str = Field(min_length=1, max_length=200)
    phone: str
    text: str = Field(min_length=1, max_length=MAX_TEXT)


@app.get("/api/customer/test-customers")
def test_customers():
    return TEST_CUSTOMERS


@app.post("/api/customer/messages")
def customer_send(body: CustomerSend, bg: BackgroundTasks, db: Session = Depends(get_db)):
    phone = body.phone.strip()
    if not re.fullmatch(r"\+?\d{7,15}", phone):
        raise HTTPException(400, "phone must be 7-15 digits, optional leading +")
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "empty message")
    ext_id = body.customer_id or "cust_web_" + re.sub(r"\D", "", phone)
    status, draft_id = ingest_message(
        db, event_id="web_" + uuid.uuid4().hex, raw="{}", ext_customer_id=ext_id, name=body.name.strip(),
        phone=phone, ext_msg_id="web_" + uuid.uuid4().hex, text=text)
    if draft_id:
        bg.add_task(generate_draft, draft_id)
    return {"status": status, "customer_id": ext_id}


@app.get("/api/customer/{customer_id}/messages")
def customer_messages(customer_id: str, db: Session = Depends(get_db)):
    """Messages only. Drafts are never returned here."""
    cust = db.scalar(select(Customer).where(Customer.external_id == customer_id))
    if not cust:
        return []
    rows = db.scalars(select(Message).join(Conversation).where(Conversation.customer_id == cust.id)
                      .order_by(Message.id)).all()
    return [{"id": m.id, "sender": m.sender, "text": m.text, "created_at": iso(m.created_at)} for m in rows]


# ---------- agent auth + inbox ----------
class Login(BaseModel):
    email: str
    password: str


@app.post("/api/auth/login")
def login(body: Login, db: Session = Depends(get_db)):
    a = db.scalar(select(Agent).where(Agent.email == body.email.strip().lower()))
    if not a or not check_pw(body.password, a.password_hash):
        raise HTTPException(401, "wrong email or password")
    token = jwt.encode({"sub": str(a.id), "exp": now() + timedelta(hours=12)}, JWT_SECRET, algorithm="HS256")
    return {"token": token, "email": a.email}


def current_agent(authorization: str = Header(default=""), db: Session = Depends(get_db)) -> Agent:
    try:
        claims = jwt.decode(authorization.removeprefix("Bearer "), JWT_SECRET, algorithms=["HS256"])
        a = db.get(Agent, int(claims["sub"]))
    except Exception:  # noqa: BLE001
        a = None
    if not a:
        raise HTTPException(401, "not signed in")
    return a


def draft_json(d: Draft) -> dict:
    return {"id": d.id, "message_id": d.message_id, "text": d.text, "status": d.status,
            "needs_human": d.needs_human, "reason": d.reason, "error": d.error}


@app.get("/api/agent/conversations")
def list_conversations(_: Agent = Depends(current_agent), db: Session = Depends(get_db)):
    convs = db.execute(select(Conversation, Customer).join(Customer).order_by(Conversation.last_activity_at.desc()).limit(200)).all()
    open_drafts = db.execute(
        select(Message.conversation_id, Draft.status, Draft.needs_human).join(Message, Draft.message_id == Message.id)
        .where(Draft.status.in_(OPEN_DRAFT))).all()
    flag: dict[int, dict] = {}
    for cid, status, nh in open_drafts:
        f = flag.setdefault(cid, {"awaiting_reply": True, "needs_attention": False})
        f["needs_attention"] |= status == "failed" or nh
    return [{"id": c.id, "customer_name": cu.name, "preview": c.last_preview, "last_sender": c.last_sender,
             "last_activity_at": iso(c.last_activity_at),
             **flag.get(c.id, {"awaiting_reply": False, "needs_attention": False})} for c, cu in convs]


@app.get("/api/agent/conversations/{conv_id}")
def get_conversation(conv_id: int, _: Agent = Depends(current_agent), db: Session = Depends(get_db)):
    conv = db.get(Conversation, conv_id)
    if not conv:
        raise HTTPException(404, "conversation not found")
    cust = db.get(Customer, conv.customer_id)
    msgs = db.scalars(select(Message).where(Message.conversation_id == conv_id).order_by(Message.id)).all()
    draft = db.scalar(select(Draft).join(Message, Draft.message_id == Message.id)
                      .where(Message.conversation_id == conv_id, Draft.status.in_(OPEN_DRAFT))
                      .order_by(Draft.id.desc()).limit(1))
    return {"id": conv.id, "customer": {"name": cust.name, "phone": cust.phone},
            "messages": [{"id": m.id, "sender": m.sender, "text": m.text, "created_at": iso(m.created_at)} for m in msgs],
            "draft": draft_json(draft) if draft else None}


class Approve(BaseModel):
    text: str = Field(max_length=MAX_TEXT)


@app.post("/api/agent/drafts/{draft_id}/approve")
def approve(draft_id: int, body: Approve, _: Agent = Depends(current_agent), db: Session = Depends(get_db)):
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "reply cannot be empty")
    d = db.scalar(select(Draft).where(Draft.id == draft_id).with_for_update())
    if not d:
        raise HTTPException(404, "draft not found")
    if d.status in ("used", "superseded"):
        raise HTTPException(409, f"draft already {d.status}")
    src = db.get(Message, d.message_id)
    conv = db.get(Conversation, src.conversation_id)
    db.add(Message(conversation_id=conv.id, sender="agent", text=text))
    d.status = "used"
    # Older unanswered drafts in this conversation are now moot.
    for o in db.scalars(select(Draft).join(Message, Draft.message_id == Message.id).where(
            Message.conversation_id == conv.id, Message.id < src.id, Draft.status.in_(OPEN_DRAFT))):
        o.status = "superseded"
    conv.last_preview, conv.last_sender, conv.last_activity_at = text[:200], "agent", now()
    db.commit()
    return {"status": "used"}


@app.post("/api/agent/drafts/{draft_id}/retry")
def retry(draft_id: int, bg: BackgroundTasks, _: Agent = Depends(current_agent), db: Session = Depends(get_db)):
    d = db.get(Draft, draft_id)
    if not d:
        raise HTTPException(404, "draft not found")
    if d.status != "failed":
        raise HTTPException(409, "only failed drafts can be retried")
    bg.add_task(generate_draft, d.id)
    return {"status": "retrying"}


@app.get("/health")
def health(db: Session = Depends(get_db)):
    db.execute(select(1))
    return {"ok": True}
