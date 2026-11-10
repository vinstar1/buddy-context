#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话事件级解析：给"会话明细"视图提供数据。

dashboard.py 里的 parse_file() 产出的是**请求级**的行（一轮对话一行），够算用量，
但画不出瀑布图——那需要**事件级**的粒度（每次思考、每次工具调用及返回）。

一个 jsonl 文件 = 一个会话，每行一个事件：
    message(user/assistant) / reasoning / function_call / function_call_result

三个必须处理的坑：
  1. role=user 混了三种东西：真人提问、后台任务通知、上下文注入
  2. parentId 指向的是「前一个事件」而非逻辑父节点，整条链出度恒为 1
  3. 时间戳会倒挂（异步写入），行序才是逻辑顺序

模块不含第三方依赖，和 dashboard.py 一样只用标准库。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path

WB = Path(os.environ.get("WORKBUDDY_HOME") or (Path.home() / ".workbuddy")).expanduser()
DB_PATH = WB / "workbuddy.db"

MAX_GAP_MS = 3000  # 步骤级瀑布里，相邻两步超过这个时长的空闲按这个算

_WRAP_TAGS = (
    "system-reminder",
    "system_reminder",
    "user_info",
    "identity_context",
    "project_context",
    "project_layout",
    "connector-status",
    "current_time",
    "user-context",
    "additional-data",
)
_QUERY_RE = re.compile(r"<user_query>(.*?)</user_query>", re.S)
_WRAP_RE = re.compile(
    r"<(%s)\b[^>]*>.*?</\1>" % "|".join(re.escape(t) for t in _WRAP_TAGS), re.S
)
_TASK_ID_RE = re.compile(r"<task-id>(.*?)</task-id>", re.S)
_TASK_STATUS_RE = re.compile(r"<status>(.*?)</status>", re.S)
_INJECT_MARKERS = ("<task-notification>", "craft_mode", "<current_time>", "<tool-use-id>")


@dataclass
class Event:
    idx: int
    kind: str  # user / task / inject / assistant / reasoning / call / result
    ts: int
    tool: str = ""
    text: str = ""
    detail: str = ""
    raw: str = ""
    status: str = ""
    call_id: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cached: int = 0
    duration: float = 0.0
    model: str = ""
    preamble: bool = False  # 这条 assistant 消息是不是「调工具前的一句话」

    @property
    def total_tokens(self) -> int:
        return self.tokens_in + self.tokens_out


@dataclass
class Session:
    sid: str
    path: Path
    title: str = ""
    cwd: str = ""
    events: list = field(default_factory=list)

    @property
    def started(self) -> int:
        return self.events[0].ts if self.events else 0

    @property
    def ended(self) -> int:
        return self.events[-1].ts if self.events else 0

    @property
    def tokens(self) -> int:
        return sum(e.total_tokens for e in self.events)

    @property
    def tool_calls(self) -> int:
        return sum(1 for e in self.events if e.kind == "call")

    @property
    def turns(self) -> int:
        return sum(1 for e in self.events if e.kind == "user")


def _join_content(content) -> str:
    if isinstance(content, list):
        return "\n".join(
            str((c or {}).get("text", "")) for c in content if isinstance(c, dict)
        )
    return str(content or "")


def _strip_wrappers(raw: str) -> str:
    text = _WRAP_RE.sub("", raw)
    text = re.sub(r"<[^>]{1,40}>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def classify_user(content) -> tuple[str, str]:
    """role=user 里其实有三种东西，混在一起会让轮次切分失真。"""
    raw = _join_content(content)

    m = _QUERY_RE.search(raw)
    if m:
        return "user", m.group(1).strip()

    if "<task-notification>" in raw:
        tid = _TASK_ID_RE.search(raw)
        st = _TASK_STATUS_RE.search(raw)
        name = tid.group(1).strip() if tid else "?"
        status = st.group(1).strip() if st else ""
        return "task", f"后台任务 {name} {status}".strip()

    cleaned = _strip_wrappers(raw)
    if cleaned and not any(mark in raw for mark in _INJECT_MARKERS):
        return "user", cleaned
    return "inject", cleaned


def _output_text(o: dict) -> str:
    out = o.get("output")
    if isinstance(out, dict):
        return str(out.get("text") or "")
    if isinstance(out, str):
        return out
    return ""


def _call_brief(o: dict) -> str:
    pd = o.get("providerData") or {}
    disp = pd.get("argumentsDisplayText")
    if disp:
        return str(disp).replace("\n", " ")[:200]

    raw = o.get("arguments")
    try:
        d = json.loads(raw) if isinstance(raw, str) else (raw or {})
        if isinstance(d, dict):
            for k in ("command", "description", "file_path", "path", "pattern",
                      "query", "prompt", "url"):
                if d.get(k):
                    return str(d[k]).replace("\n", " ")[:200]
    except Exception:
        pass
    return str(raw or "").replace("\n", " ")[:200]


def _usage(o: dict) -> tuple:
    u = (o.get("providerData") or {}).get("rawUsage") or {}
    try:
        return (
            int(u.get("prompt_tokens") or 0),
            int(u.get("completion_tokens") or 0),
            int(u.get("prompt_cache_hit_tokens") or 0),
        )
    except (TypeError, ValueError):
        return (0, 0, 0)


def _assistant_text(o: dict) -> str:
    parts = []
    for c in o.get("content") or []:
        if isinstance(c, dict) and c.get("type") in ("output_text", "text"):
            parts.append(str(c.get("text") or ""))
    return "\n".join(p for p in parts if p).strip()


def _reasoning_text(o: dict) -> str:
    parts = []
    for c in o.get("rawContent") or []:
        if isinstance(c, dict) and c.get("type") == "reasoning_text":
            parts.append(str(c.get("text") or ""))
    return "\n".join(p for p in parts if p).strip()


def load_session(path: Path, title: str = "", cwd: str = "") -> Session:
    """读一个 jsonl，产出 Session。坏行跳过，不中断。"""
    sid = path.stem
    events: list = []
    pending: dict = {}

    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue

            t = o.get("type")
            ts = int(o.get("timestamp") or 0)
            pd = o.get("providerData") or {}

            if t == "session-meta":
                if not cwd:
                    cwd = str(o.get("cwd") or "")
                continue
            if t == "file-history-snapshot":
                continue
            if t == "ai-title":
                if not title:
                    title = str(o.get("aiTitle") or "")
                continue

            if t == "message":
                if o.get("role") == "user":
                    kind, text = classify_user(o.get("content"))
                    raw = _join_content(o.get("content"))
                    if kind == "inject" and not text and not raw:
                        continue
                    events.append(Event(
                        idx=len(events), kind=kind, ts=ts,
                        tool="后台任务" if kind == "task" else "",
                        text=text[:4000], detail=text, raw=raw[:20000],
                        status=str(o.get("status") or ""),
                        model=str(pd.get("model") or ""),
                    ))
                else:
                    text = _assistant_text(o)
                    tin, tout, hit = _usage(o)
                    events.append(Event(
                        idx=len(events), kind="assistant", ts=ts,
                        text=text[:4000], detail=text, status=str(o.get("status") or ""),
                        tokens_in=tin, tokens_out=tout, cached=hit,
                        model=str(pd.get("model") or ""),
                    ))
                continue

            if t == "reasoning":
                text = _reasoning_text(o)
                tin, tout, hit = _usage(o)
                events.append(Event(
                    idx=len(events), kind="reasoning", ts=ts,
                    text=text[:4000], detail=text,
                    tokens_in=tin, tokens_out=tout, cached=hit,
                    model=str(pd.get("model") or ""),
                ))
                continue

            if t == "function_call":
                tin, tout, hit = _usage(o)
                call_id = str(o.get("callId") or "")
                ev = Event(
                    idx=len(events), kind="call", ts=ts,
                    tool=str(o.get("name") or "?"),
                    text=_call_brief(o), detail=str(o.get("arguments") or ""),
                    call_id=call_id, tokens_in=tin, tokens_out=tout, cached=hit,
                    model=str(pd.get("model") or ""),
                )
                events.append(ev)
                if call_id:
                    pending[call_id] = ts
                continue

            if t == "function_call_result":
                out = _output_text(o)
                call_id = str(o.get("callId") or "")
                ev = Event(
                    idx=len(events), kind="result", ts=ts,
                    tool=str(o.get("name") or "?"),
                    text=out.replace("\n", " ")[:200], detail=out,
                    status=str(o.get("status") or ""), call_id=call_id,
                )
                events.append(ev)
                if call_id in pending:
                    ev.duration = round(max(0.0, (ts - pending.pop(call_id)) / 1000.0), 3)
                continue

    if not title:
        title = path.parent.name

    # 区分「过程说明」和「最终答复」。agent 每步决策都会产出一段文字 + 零或多个工具调用，
    # 落在日志里文字是独立的 message，看起来就像流程中间插了一条回复——那其实是
    # 给下一步动作铺垫的话，后面紧跟工具调用。
    for i, ev in enumerate(events):
        if ev.kind == "assistant":
            nxt = events[i + 1] if i + 1 < len(events) else None
            ev.preamble = bool(nxt and nxt.kind == "call")

    return Session(sid=sid, path=path, title=title, cwd=cwd, events=events)


def load_session_titles() -> dict:
    """会话 id → (标题, 工作目录)。数据库读不到就当没有。"""
    if not DB_PATH.exists():
        return {}
    out = {}
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB_PATH.as_posix(), uri=True)
        try:
            for sid, title, cwd in con.execute("select id, title, cwd from sessions"):
                out[str(sid)] = (str(title or ""), str(cwd or ""))
        finally:
            con.close()
    except Exception:
        pass
    return out


# ---------- 切分与时间轴 ----------


def group_turns(events: list) -> list:
    """按真人提问切分轮次。task / inject 归入当前轮，不另起一轮。"""
    groups: list = []
    current = None

    for ev in events:
        if ev.kind == "user" or current is None:
            current = {"turn": len(groups), "events": [], "prompt": ""}
            groups.append(current)
        current["events"].append(ev)
        if ev.kind == "user" and not current["prompt"]:
            current["prompt"] = ev.text

    for g in groups:
        evs = g["events"]
        g["calls"] = sum(1 for e in evs if e.kind == "call")
        g["tokens_in"] = sum(e.tokens_in for e in evs)
        g["tokens_out"] = sum(e.tokens_out for e in evs)
        g["cached"] = sum(e.cached for e in evs)
        g["duration"] = round((evs[-1].ts - evs[0].ts) / 1000.0, 2) if evs else 0.0
        g["tools"] = sorted({e.tool for e in evs if e.kind == "call" and e.tool})
        g["failed"] = sum(
            1 for e in evs
            if e.kind == "result" and e.status not in ("completed", "", "success")
        )
        g["start_ts"] = evs[0].ts if evs else 0
        g["end_ts"] = evs[-1].ts if evs else 0
        g["raw_dur"] = g["duration"]
    return groups


def _unit_from_event(ev: Event, idx: int) -> dict:
    return {
        "turn": idx,
        "events": [ev],
        "prompt": ev.text if ev.kind == "user" else "",
        "calls": 1 if ev.kind == "call" else 0,
        "tokens_in": ev.tokens_in,
        "tokens_out": ev.tokens_out,
        "cached": ev.cached,
        "duration": ev.duration,
        "raw_dur": ev.duration,
        "tools": [ev.tool] if ev.kind == "call" and ev.tool else [],
        "failed": 1 if ev.kind == "result" and ev.status not in ("completed", "", "success") else 0,
        "start_ts": ev.ts,
        "end_ts": ev.ts,
    }


def to_units(sess: Session, granularity: str = "turn") -> list:
    if granularity == "step":
        units = [_unit_from_event(ev, i) for i, ev in enumerate(sess.events)]

        # 时间戳单调化：异步写入的事件可能比后一条还晚，行序才是逻辑顺序
        raw = []
        prev = units[0]["start_ts"]
        for u in units:
            t = max(u["start_ts"], prev)
            raw.append(t)
            prev = t

        # 压缩长空闲：会话横跨几小时，不压缩的话秒级步骤在轴上就是看不见的线
        comp = []
        shift = 0
        for i, t in enumerate(raw):
            if i > 0:
                gap = t - raw[i - 1]
                if gap > MAX_GAP_MS:
                    shift += gap - MAX_GAP_MS
            comp.append(t - shift)

        for i, u in enumerate(units):
            u["start_ts"] = comp[i]
            u["end_ts"] = comp[i + 1] if i + 1 < len(units) else comp[i] + 1
            if u["end_ts"] <= u["start_ts"]:
                u["end_ts"] = u["start_ts"] + 1
            u["raw_dur"] = round((raw[i + 1] - raw[i]) / 1000.0, 3) if i + 1 < len(raw) else 0.001
        return units

    return group_turns(sess.events)


# ---------- 索引与载荷 ----------


class SessionIndex:
    """会话列表索引。构建一次约 7 秒，所以第一次请求时才建，之后走缓存。"""

    _lock = threading.Lock()
    _index = None

    @classmethod
    def get(cls, force: bool = False) -> list:
        with cls._lock:
            if cls._index is not None and not force:
                return cls._index
            titles = load_session_titles()
            rows = []
            paths = sorted(WB.glob("projects/*/*.jsonl"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            for p in paths:
                t, c = titles.get(p.stem, ("", ""))
                try:
                    s = load_session(p, title=t, cwd=c)
                except Exception:
                    continue
                if not s.events:
                    continue
                rows.append({
                    "sid": s.sid,
                    "title": s.title or "(无标题)",
                    "cwd": s.cwd,
                    "events": len(s.events),
                    "turns": s.turns,
                    "calls": s.tool_calls,
                    "tokens": s.tokens,
                    "start": s.started,
                    "end": s.ended,
                })
            rows.sort(key=lambda r: r["start"], reverse=True)
            cls._index = rows
            return cls._index


def _cache_session(sid: str):
    hits = list(WB.glob(f"projects/*/{sid}*.jsonl"))
    if not hits:
        return None
    exact = [p for p in hits if p.stem == sid]
    path = (exact or hits)[0]
    titles = load_session_titles()
    title, cwd = titles.get(path.stem, ("", ""))
    return load_session(path, title=title, cwd=cwd)


def session_payload(sid: str, granularity: str = "turn", detail_limit: int = 8000):
    """单会话的完整载荷。找不到返回 None。"""
    sess = _cache_session(sid)
    if sess is None:
        return None

    units = to_units(sess, granularity)
    out = []
    for u in units:
        evs = []
        for e in u["events"]:
            evs.append({
                "kind": e.kind,
                "tool": e.tool,
                "text": e.text[:600],
                "detail": (e.detail or "")[:detail_limit],
                "raw": (e.raw or "")[:detail_limit],
                "status": e.status,
                "tokens_in": e.tokens_in,
                "tokens_out": e.tokens_out,
                "cached": e.cached,
                "duration": e.duration,
                "preamble": e.preamble,
                "ts": e.ts,
                "model": e.model,
            })
        out.append({
            "turn": u["turn"],
            "prompt": u["prompt"][:600],
            "calls": u["calls"],
            "tokens_in": u["tokens_in"],
            "tokens_out": u["tokens_out"],
            "cached": u["cached"],
            "duration": u["duration"],
            "raw_dur": u.get("raw_dur", u["duration"]),
            "tools": u["tools"],
            "failed": u["failed"],
            "start_ts": u["start_ts"],
            "end_ts": u["end_ts"],
            "events": evs,
        })

    return {
        "sid": sess.sid,
        "title": sess.title or "(无标题)",
        "cwd": sess.cwd,
        "granularity": granularity,
        "compressed": granularity == "step",
        "units": out,
        "totals": {
            "events": len(sess.events),
            "turns": sess.turns,
            "calls": sess.tool_calls,
            "tokens": sess.tokens,
            "failed": sum(
                1 for e in sess.events
                if e.kind == "result" and e.status not in ("completed", "", "success")
            ),
            "start": sess.started,
            "end": sess.ended,
        },
    }
