from datetime import datetime
import re

from fastapi import HTTPException
from sqlalchemy import select

from app.copilot.store import Source, Record, digest

POLICY_VERSION = "demo-sop-1"
# Explicit field allowlist: bank/account/location and annotation fields never enter the model.
SAFE_FIELDS = {"订单号", "工单号", "关联订单号", "商品名称", "商品货号", "数量", "实付金额(元)",
               "订单状态", "下单时间", "工单类型", "售后原因", "工单状态", "任务状态", "转账状态",
               "退款问题类型", "退款金额(元)", "问题类型", "退货原因", "创建时间", "完成时间",
               "使用商品", "症状描述", "用后多久出现", "是否停用", "是否就医", "不适部位"}
IDENTITY_FIELDS = {"订单号", "工单号", "关联订单号", "下单时间", "创建时间"}


def sanitize(text):
    text = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[手机号已隐藏]", text)
    return re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[邮箱已隐藏]", text)


def list_sessions(store, query="", scene=""):
    with store.transaction() as db:
        records = db.scalars(select(Source).where(Source.sheet == "聊天记录")).all()
        result = {}
        for row in records:
            p = row.payload
            s = result.setdefault(row.session_id, {"id": row.session_id, "nickname": p["买家昵称"], "store": p["店铺"],
                "scene": p.get("scene_major", ""), "count": 0, "latest": "", "time": "", "orders": set()})
            s["count"] += 1
            if p.get("关联订单号"):
                s["orders"].add(p["关联订单号"])
            if p["发送时间"] >= s["time"]:
                s["time"], s["latest"] = p["发送时间"], sanitize(p["message_text"])
        cases = db.scalars(select(Record).where(Record.kind == "case")).all()
        for s in result.values():
            s["risk_count"] = sum(c.session_id == s["id"] and c.payload["status"] not in {"已关闭", "已排除"} for c in cases)
            s["orders"] = sorted(s["orders"])
        return [s for s in sorted(result.values(), key=lambda s: s["id"]) if
                (not query or query.lower() in (s["id"] + s["nickname"] + " ".join(s["orders"])).lower()) and
                (not scene or s["scene"] == scene)]


def get_context(store, session_id, mode="review", cutoff=None):
    if mode not in {"review", "replay"}:
        raise HTTPException(422, "未知回放模式")
    with store.transaction() as db:
        rows = db.scalars(select(Source).where(Source.session_id == session_id)).all()
        chat = sorted([r for r in rows if r.sheet == "聊天记录"], key=lambda r: (r.payload["发送时间"], int(r.payload["消息序号"])))
        if not chat:
            raise HTTPException(404, "会话不存在")
        if mode == "replay" and (cutoff is None or cutoff < 1 or cutoff > len(chat)):
            raise HTTPException(422, "请选择有效回放位置")
        visible = chat if mode == "review" else chat[:cutoff]
        cutoff_time = visible[-1].payload["发送时间"]
        messages, evidence, facts = [], {}, []
        for r in visible:
            p = r.payload
            text = sanitize(p["message_text"] or "")
            missing_image = p.get("内容类型") == "图片" or bool(p.get("image_path"))
            if missing_image:
                text = "[图片原件未提供]"
            messages.append({"id": r.key, "role": "系统" if p["角色"] == "系统推送" else p["角色"], "text": text, "time": p["发送时间"], "source_type": "official_mock", "image_missing": missing_image})
            evidence[r.key] = {"id": r.key, "value": text, "field": "message_text", **r.provenance}
        for r in rows:
            if r.sheet == "聊天记录":
                continue
            p = r.payload
            created = p.get("下单时间") if r.sheet == "订单" else p.get("创建时间")
            if mode == "replay" and (not created or created > cutoff_time):
                continue
            fields = SAFE_FIELDS if mode == "review" else IDENTITY_FIELDS
            values = {}
            for field in sorted(fields & set(p)):
                if not p[field]:
                    continue
                eid = r.key + "#" + field
                values[field] = {"value": p[field], "evidence_id": eid}
                evidence[eid] = {"id": eid, "value": p[field], "field": field, **r.provenance}
            facts.append({"id": r.key, "type": r.sheet, "fields": values, "note": "导入快照；采集时间未知" if mode == "review" else "该时点状态未知；仅确认记录存在"})
        demo = db.scalars(select(Record).where(Record.kind == "message", Record.session_id == session_id)).all()
        # Demo messages never enter historical replay. Separate branch support is a later increment.
        if mode == "review":
            for row in sorted(demo, key=lambda r: r.payload["time"]):
                messages.append({"id": row.id, "role": "客服", "text": row.payload["text"], "time": row.payload["time"], "source_type": "demo_operation", "image_missing": False})
                evidence[row.id] = {"id": row.id, "field": "text", "value": row.payload["text"], "source_type": "demo_operation"}
        version = digest({"session": session_id, "mode": mode, "cutoff": cutoff if mode == "replay" else None,
                          "sources": [(r.key, r.provenance["batch"]) for r in rows],
                          "messages": [m["id"] for m in messages], "policy": POLICY_VERSION})
        return {"session_id": session_id, "nickname": chat[0].payload["买家昵称"], "store": chat[0].payload["店铺"],
                "mode": mode, "cutoff": cutoff if mode == "replay" else None, "total": len(chat), "version": version,
                "messages": messages, "facts": facts, "evidence": evidence, "data_time": cutoff_time,
                "policy_version": POLICY_VERSION, "warnings": ["官方 AI 生成 MOCK 数据", "昵称可能重名，不自动合并历史", "无正式品牌政策；使用演示规则"]}


def assert_current(store, session_id, mode, cutoff, version):
    context = get_context(store, session_id, mode, cutoff)
    if context["version"] != version:
        raise HTTPException(409, "会话内容已更新，请刷新并重新分析")
    return context
