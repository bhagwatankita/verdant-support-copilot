import hashlib, hmac, json, os, tempfile

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/t.db"
os.environ["AGENT_EMAIL"], os.environ["AGENT_PASSWORD"] = "a@x.test", "pw"

import pytest
from fastapi.testclient import TestClient
from app import llm, main

SECRET = main.WEBHOOK_SECRET


def event(eid="evt_1", mid="msg_1", text="What is your return policy?", cid="cust_1"):
    return {"event_id": eid, "type": "message.received", "brand_id": "verdant-co", "timestamp": "2026-11-03T10:15:22Z",
            "customer": {"id": cid, "name": "Aisha Khan", "phone": "+919800000001"},
            "message": {"id": mid, "type": "text", "text": text}}


def post(c, body, secret=SECRET):
    raw = json.dumps(body).encode()
    sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return c.post("/webhook", content=raw, headers={"X-Signature": sig, "Content-Type": "application/json"})


@pytest.fixture()
def c(monkeypatch):
    monkeypatch.setattr(llm, "generate", lambda name, hist: {"reply": f"Hi {name}, draft", "needs_human": False, "reason": ""})
    with TestClient(main.app) as client:
        yield client


def auth(c):
    t = c.post("/api/auth/login", json={"email": "a@x.test", "password": "pw"}).json()["token"]
    return {"Authorization": f"Bearer {t}"}


def test_bad_signature(c):
    assert post(c, event(), secret="wrong").status_code == 401


def test_flow_and_idempotency(c):
    assert post(c, event()).json()["status"] == "accepted"
    assert post(c, event()).json()["status"] == "duplicate"
    h = auth(c)
    convs = c.get("/api/agent/conversations", headers=h).json()
    assert len(convs) == 1
    conv = c.get(f"/api/agent/conversations/{convs[0]['id']}", headers=h).json()
    assert len(conv["messages"]) == 1 and conv["draft"]["status"] == "ready"
    # second message joins the same open conversation
    post(c, event("evt_2", "msg_2", "Another q"))
    assert len(c.get("/api/agent/conversations", headers=h).json()) == 1
    d = c.get(f"/api/agent/conversations/{convs[0]['id']}", headers=h).json()["draft"]
    assert c.post(f"/api/agent/drafts/{d['id']}/approve", json={"text": "Edited reply"}, headers=h).status_code == 200
    assert c.post(f"/api/agent/drafts/{d['id']}/approve", json={"text": "again"}, headers=h).status_code == 409
    msgs = c.get("/api/customer/cust_1/messages").json()
    assert msgs[-1] == {**msgs[-1], "sender": "agent", "text": "Edited reply"}
    assert "draft" not in json.dumps(msgs).lower()


def test_agent_endpoints_need_auth(c):
    assert c.get("/api/agent/conversations").status_code == 401


def test_llm_failure_keeps_message(monkeypatch):
    def boom(*a): raise llm.LLMError("quota")
    monkeypatch.setattr(llm, "generate", boom)
    with TestClient(main.app) as c:
        assert post(c, event("evt_f", "msg_f", cid="cust_f")).status_code == 200
        h = auth(c)
        rows = c.get("/api/agent/conversations", headers=h).json()
        convs = [c.get(f"/api/agent/conversations/{r['id']}", headers=h).json() for r in rows]
        failed = [x for x in convs if x["draft"] and x["draft"]["status"] == "failed"]
        assert failed and failed[0]["messages"] and "quota" in failed[0]["draft"]["error"]


def test_health_text_forces_human(c):
    post(c, event("evt_h", "msg_h", "I think I'm allergic to the serum", cid="cust_h"))
    h = auth(c)
    rows = c.get("/api/agent/conversations", headers=h).json()
    assert any(r["needs_attention"] for r in rows)


def test_customer_view_same_path(c):
    r = c.post("/api/customer/messages", json={"name": "Web User", "phone": "+911234567890", "text": "hello"})
    assert r.status_code == 200 and r.json()["status"] == "accepted"
