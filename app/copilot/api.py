from datetime import datetime, timedelta
import secrets
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.copilot.store import Source, Record, Audit, audit, digest
from app.copilot.context import assert_current, get_context, list_sessions
from app.copilot.analysis import analyze, rule_signals, validate_draft
from app.copilot.importer import import_workbook, ImportFailure

router = APIRouter(prefix="/api")
TOKENS = {}
ROLES = {"客服 A": "agent", "客服 B": "agent", "主管": "supervisor", "管理员": "admin"}


def store(request):
    return request.app.state.store


def actor(request: Request):
    identity = TOKENS.get(request.headers.get("authorization", "").removeprefix("Bearer "))
    if not identity or identity["expires"] < datetime.now():
        raise HTTPException(401, "请先选择演示身份")
    return identity


def supervisor(who):
    if who["role"] not in {"supervisor", "admin"}:
        raise HTTPException(403, "此操作需要主管或管理员身份")


class Login(BaseModel):
    name: Literal["客服 A", "客服 B", "主管", "管理员"]


@router.post("/demo/login")
def login(body: Login, request: Request):
    if request.client and request.client.host not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(403, "演示身份仅允许本机登录")
    token = secrets.token_urlsafe(32)
    TOKENS[token] = {"name": body.name, "role": ROLES[body.name], "expires": datetime.now() + timedelta(hours=8)}
    return {"token": token, "name": body.name, "role": ROLES[body.name]}


@router.get("/status")
def status(request: Request, who=Depends(actor)):
    settings = get_settings()
    with store(request).transaction() as db:
        sources = db.scalars(select(Source)).all()
        counts = {}
        for row in sources:
            counts[row.sheet] = counts.get(row.sheet, 0) + 1
    return {"provider": settings.ai_provider, "model": settings.ollama_model if settings.ai_provider == "ollama" else settings.openai_model,
            "counts": counts, "sessions": len({r.session_id for r in sources}), "source": "官方 MOCK",
            "startup_error": request.app.state.import_error}


@router.post("/imports/official")
def import_official(request: Request, who=Depends(actor)):
    if who["role"] != "admin":
        raise HTTPException(403, "需要管理员身份")
    try:
        result = import_workbook(store(request), request.app.state.source_path)
        request.app.state.import_error = None
        return result
    except ImportFailure as exc:
        raise HTTPException(422, exc.errors)
    except OSError:
        raise HTTPException(422, "找不到官方数据文件，请检查启动说明中的位置")


@router.get("/sessions")
def sessions(request: Request, query: str = "", scene: str = "", who=Depends(actor)):
    return list_sessions(store(request), query, scene)


@router.get("/sessions/{sid}/context")
def context(sid: str, request: Request, mode: str = "review", cutoff: int | None = None, who=Depends(actor)):
    ctx = get_context(store(request), sid, mode, cutoff)
    ctx["risk_candidates"], ctx["promise_candidates"] = rule_signals(ctx)
    return ctx


class ContextRequest(BaseModel):
    mode: Literal["review", "replay"] = "review"
    cutoff: int | None = None
    version: str


@router.post("/sessions/{sid}/analyses")
async def analysis(sid: str, body: ContextRequest, request: Request, who=Depends(actor)):
    ctx = assert_current(store(request), sid, body.mode, body.cutoff, body.version)
    result = await analyze(store(request), ctx, who["name"])
    fresh = get_context(store(request), sid, body.mode, body.cutoff)
    if fresh["version"] != ctx["version"]:
        result["status"], result["draft"] = "stale", ""
        result["error"] = "分析期间会话已更新，请重新生成"
    return result


class Send(ContextRequest):
    text: str = Field(min_length=1, max_length=1200)
    run_id: str | None = None
    request_id: str = Field(min_length=8, max_length=80)


@router.post("/sessions/{sid}/demo-messages")
def send(sid: str, body: Send, request: Request, who=Depends(actor)):
    if body.mode != "review":
        raise HTTPException(422, "逐轮回放只读，请切换全量复盘后发送演示消息")
    repo = store(request)
    key = "send:" + digest([who["name"], body.request_id])
    fingerprint = digest(body.model_dump())
    with repo.transaction() as db:
        # Acquire a writer before version checking. All sends use this transaction boundary.
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        old = db.get(Record, key)
        if old:
            if old.payload["fingerprint"] != fingerprint or old.session_id != sid:
                raise HTTPException(409, "幂等键不能用于不同消息")
            return old.payload
        ctx = assert_current(repo, sid, body.mode, body.cutoff, body.version)
        errors = validate_draft(body.text, ctx)
        if errors:
            raise HTTPException(422, "；".join(errors))
        if body.run_id:
            run = db.get(Record, body.run_id)
            if not run or run.kind != "analysis" or run.session_id != sid or run.payload["context_version"] != body.version or run.payload["status"] != "validated":
                raise HTTPException(409, "草稿无效或已过期，请重新分析")
        previous = db.scalars(select(Record).where(Record.kind == "message", Record.session_id == sid)).all()
        latest = max([ctx["data_time"]] + [r.payload["time"] for r in previous])
        msg = {"text": body.text.strip(), "time": (datetime.fromisoformat(latest) + timedelta(seconds=1)).isoformat(sep=" "),
               "actor": who["name"], "fingerprint": fingerprint, "source_type": "demo_operation", "run_id": body.run_id}
        db.add(Record(id=key, kind="message", session_id=sid, payload=msg))
        audit(db, who["name"], "demo_send", key, run_id=body.run_id)
        return msg


class Feedback(BaseModel):
    outcome: Literal["采用", "拒用", "纠正"]
    reason: str = Field(min_length=1, max_length=600)


@router.post("/analyses/{rid}/feedback")
def feedback(rid: str, body: Feedback, request: Request, who=Depends(actor)):
    with store(request).transaction() as db:
        row = db.get(Record, rid)
        if not row or row.kind != "analysis":
            raise HTTPException(404, "分析不存在")
        audit(db, who["name"], "feedback", rid, **body.model_dump())
    return {"saved": True}


class Followup(ContextRequest):
    session_id: str
    type: str = Field(min_length=1, max_length=80)
    content: str = Field(min_length=1, max_length=1200)
    priority: Literal["高", "常规"] = "常规"
    evidence_ids: list[str] = Field(min_length=1)
    owner: str = ""
    due_at: str | None = None


def valid_due(value):
    if value:
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is not None:
                raise ValueError()
            return parsed.isoformat(sep=" ")
        except ValueError:
            raise HTTPException(422, "期限需为有效的演示本地时间")
    return None


@router.post("/followups")
def followup(body: Followup, request: Request, who=Depends(actor)):
    ctx = assert_current(store(request), body.session_id, body.mode, body.cutoff, body.version)
    if not set(body.evidence_ids) <= set(ctx["evidence"]):
        raise HTTPException(422, "证据不存在或在该时点不可见")
    if body.owner and body.owner not in ROLES:
        raise HTTPException(422, "未知责任人")
    if body.owner and body.owner != who["name"]:
        supervisor(who)
    key = "case:" + digest([body.session_id, body.mode, body.cutoff, body.type, sorted(body.evidence_ids)])
    payload = {"type": body.type, "content": body.content, "priority": body.priority,
               "evidence": [ctx["evidence"][e] for e in body.evidence_ids], "owner": body.owner,
               "status": "处理中" if body.owner else "待认领", "due_at": valid_due(body.due_at), "clock": ctx["data_time"],
               "mode": body.mode, "cutoff": body.cutoff, "source_type": "demo_operation", "result": "", "completion_evidence": ""}
    try:
        with store(request).transaction() as db:
            existing = db.get(Record, key)
            if existing:
                return {"id": key, "version": existing.version, **existing.payload, "duplicate": True}
            db.add(Record(id=key, kind="case", session_id=body.session_id, payload=payload))
            audit(db, who["name"], "create_followup", key, content=body.content)
    except IntegrityError:
        raise HTTPException(409, "另一位客服刚创建同一事项，请刷新")
    return {"id": key, "version": 1, **payload}


@router.get("/cases")
def cases(request: Request, who=Depends(actor)):
    with store(request).transaction() as db:
        records = db.scalars(select(Record).where(Record.kind == "case")).all()
        result = []
        for r in records:
            p = r.payload
            logs = db.scalars(select(Audit).where(Audit.target == r.id).order_by(Audit.id)).all()
            result.append({"id": r.id, "session_id": r.session_id, "version": r.version, **p,
                           "overdue": bool(p["due_at"] and p["due_at"] < p["clock"] and p["status"] not in {"已关闭", "已排除"}),
                           "history": [{"actor": a.actor, "action": a.action, "detail": a.detail, "at": a.created_at} for a in logs]})
        return result


class CaseAction(BaseModel):
    version: int
    action: Literal["claim", "progress", "wait", "callback", "close", "exclude", "reopen", "assign", "deadline", "advance", "note"]
    note: str = Field(default="", max_length=1200)
    evidence: str = Field(default="", max_length=1200)
    owner: str = ""
    due_at: str | None = None


@router.post("/cases/{cid}/actions")
def case_action(cid: str, body: CaseAction, request: Request, who=Depends(actor)):
    with store(request).transaction() as db:
        row = db.get(Record, cid)
        if not row or row.kind != "case":
            raise HTTPException(404, "事项不存在")
        if row.version != body.version:
            raise HTTPException(409, "事项已被其他人更新，请刷新")
        p, action = dict(row.payload), body.action
        if action in {"close", "exclude", "reopen", "assign", "deadline", "advance"}:
            supervisor(who)
        elif action != "claim" and p["owner"] != who["name"]:
            supervisor(who)
        transitions = {"claim": ({"待认领"}, "处理中"), "progress": ({"待外部反馈", "待回访", "重新打开"}, "处理中"),
                       "wait": ({"处理中"}, "待外部反馈"), "callback": ({"处理中", "待外部反馈"}, "待回访"),
                       "close": ({"处理中", "待外部反馈", "待回访"}, "已关闭"), "exclude": ({"待认领"}, "已排除"), "reopen": ({"已关闭"}, "重新打开")}
        if action in transitions:
            allowed, target = transitions[action]
            if p["status"] not in allowed:
                raise HTTPException(422, "当前状态不允许此操作")
            if action in {"close", "exclude", "reopen"} and not body.note.strip():
                raise HTTPException(422, "请填写处理结果或原因")
            if action in {"close", "reopen"} and not body.evidence.strip():
                raise HTTPException(422, "请补充处理结果证据或重开证据")
            p["status"] = target
        if action == "claim":
            p["owner"] = who["name"]
        if action == "close":
            p["result"], p["completion_evidence"] = body.note, body.evidence
        if action == "assign":
            if body.owner not in ROLES or not body.note.strip():
                raise HTTPException(422, "改派需有效责任人和原因")
            p["owner"] = body.owner
        if action == "deadline":
            if not body.due_at or not body.note.strip():
                raise HTTPException(422, "调整期限需填写时间和原因")
            p["due_at"] = valid_due(body.due_at)
        if action == "note" and not body.note.strip():
            raise HTTPException(422, "跟进记录不能为空")
        if action == "advance":
            p["clock"] = (datetime.fromisoformat(p["clock"]) + timedelta(days=1)).isoformat(sep=" ")
        changed = db.execute(update(Record).where(Record.id == cid, Record.version == body.version).values(payload=p, version=body.version + 1))
        if changed.rowcount != 1:
            raise HTTPException(409, "更新冲突，请刷新")
        audit(db, who["name"], action, cid, before=row.payload, after=p, note=body.note, evidence=body.evidence)
        return {"id": cid, "version": body.version + 1, **p}
