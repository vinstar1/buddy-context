#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地用量看板 —— 扫描 WorkBuddy 本机数据，按 DeepSeek 官方价目折算金额，
并提供一个可手动刷新的本地服务。

用法:
    python dashboard.py            # 启动服务并打开浏览器 (http://127.0.0.1:8765)
    python dashboard.py --once     # 只重算并写出一份静态快照 index.html
    python dashboard.py --port 9000

数据来源:
    ~/.workbuddy/projects/*/*.jsonl          每条请求的 rawUsage (token / 缓存 / credit)
    ~/.workbuddy/workbuddy.db                 session_usage.credit_json (积分对账)

计价说明:
    DeepSeek 官方价 (元 / 百万 tokens)，高峰时段为空闲时段的 2 倍。
    高峰 = 北京时间周一至周五 9:00-12:00、14:00-18:00。
    本程序按星期判断工作日，未扣除法定节假日（如需精确请维护 HOLIDAYS）。
    非 DeepSeek 模型没有官方价，金额留空，只展示积分。
"""

import argparse
import collections
import datetime as dt
import glob
import json
import os
import sqlite3
import statistics
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path.home()
# 数据目录：默认 ~/.workbuddy，可用 --data-dir 或环境变量 WORKBUDDY_HOME 覆盖
WB = Path(os.environ.get("WORKBUDDY_HOME") or (HOME / ".workbuddy")).expanduser()
DB_PATH = WB / "workbuddy.db"
CACHE_PATH = HERE / ".scan-cache.json"
TEMPLATE_PATH = HERE / "template.html"
SNAPSHOT_PATH = HERE / "index.html"
STATE_PATH = HERE / "dashboard-data.json"

# ---------- 价格表 ----------
# 元 / 百万 tokens
PRICES = {
    "flash": {
        "label": "deepseek-flash",
        "off":  {"hit": 0.02, "miss": 1.0, "out": 4.0},
        "peak": {"hit": 0.04, "miss": 2.0, "out": 8.0},
    },
    "pro": {
        "label": "deepseek-v4-pro",
        "off":  {"hit": 0.15, "miss": 4.5, "out": 13.5},
        "peak": {"hit": 0.30, "miss": 9.0, "out": 27.0},
    },
}

# 中国法定节假日（仅 2026 年，用于高峰时段判定；留空则只按周末判断）
HOLIDAYS = set()

BJ = dt.timezone(dt.timedelta(hours=8))
CACHE = {"version": 3, "files": {}}
LOCK = threading.Lock()


def family_of(model):
    """把本地模型名映射到价格表；非 DeepSeek 返回 None。"""
    if not model:
        return None
    m = model.lower()
    if "deepseek" not in m:
        return None
    if "pro" in m:
        return "pro"
    if "flash" in m:
        return "flash"
    return None


def is_peak(ts_ms):
    """北京时间工作日 9:00-12:00 / 14:00-18:00 为高峰。"""
    t = dt.datetime.fromtimestamp(ts_ms / 1000, BJ)
    if t.weekday() >= 5:
        return False
    if t.strftime("%Y-%m-%d") in HOLIDAYS:
        return False
    return (9 <= t.hour < 12) or (14 <= t.hour < 18)


def to_ms(value):
    """timestamp 可能是毫秒整数，也可能是 ISO 字符串。"""
    if isinstance(value, (int, float)):
        return int(value if value > 1e11 else value * 1000)
    if isinstance(value, str):
        try:
            s = value.replace("Z", "+00:00")
            return int(dt.datetime.fromisoformat(s).timestamp() * 1000)
        except Exception:
            return None
    return None


def parse_file(path):
    """抽取一个 jsonl 里所有带 rawUsage 的 step。

    注意：conversationRequestId 对应的是「一轮对话」，里面往往包含几十次 API 调用
    （同一个 rid、不同 stepSeq）。所以这里以 (rid, stepSeq) 为单位去重，
    保留 token 最完整的一版，后续再按 rid 汇总成一轮。
    单步时长 = 本条时间 - 上一条事件时间（含客户端与工具回调开销）。
    """
    out = {}
    prev_ts = None
    try:
        fh = open(path, encoding="utf-8", errors="ignore")
    except OSError:
        return out
    with fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except Exception:
                continue
            ts = to_ms(obj.get("timestamp"))
            pd = obj.get("providerData") or {}
            ru = pd.get("rawUsage")
            if not ru:
                if ts:
                    prev_ts = ts
                continue
            rid = pd.get("conversationRequestId")
            if not rid or not ts:
                prev_ts = ts or prev_ts
                continue
            step = pd.get("stepSeq")
            details = ru.get("prompt_tokens_details") or {}
            cd = ru.get("completion_tokens_details") or {}
            dur = None
            if prev_ts and 0.5 < (ts - prev_ts) / 1000.0 < 300:
                dur = round((ts - prev_ts) / 1000.0, 2)
            item = {
                "rid": rid,
                "step": step if step is not None else ts,
                "sid": obj.get("sessionId") or "",
                "model": pd.get("model") or pd.get("requestModelId") or "未知",
                "ts": ts,
                "p": ru.get("prompt_tokens") or 0,
                "c": ru.get("completion_tokens") or 0,
                "ch": ru.get("prompt_cache_hit_tokens") or details.get("cached_tokens") or 0,
                "rs": cd.get("reasoning_tokens") or 0,
                "credit": ru.get("credit"),
                "dur": dur,
            }
            key = "%s#%s" % (rid, item["step"])
            old = out.get(key)
            if not old or (item["p"] + item["c"]) >= (old["p"] + old["c"]):
                if old and old.get("dur") and not dur:
                    item["dur"] = old["dur"]
                out[key] = item
            prev_ts = ts
    return list(out.values())


def scan_requests(force=False):
    """扫描所有会话，带 mtime/size 缓存，刷新时只重解析变化的文件。"""
    if not force and CACHE_PATH.exists():
        try:
            cached = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            if cached.get("version") == CACHE["version"]:
                CACHE["files"] = cached.get("files", {})
        except Exception:
            pass

    files = sorted(glob.glob(str(WB / "projects" / "*" / "*.jsonl")))
    new_files = {}
    merged = {}
    changed = 0
    for path in files:
        try:
            st = os.stat(path)
        except OSError:
            continue
        key = os.path.basename(path) + "@" + str(st.st_size)
        entry = CACHE["files"].get(path)
        if entry and entry.get("sig") == [int(st.st_mtime), st.st_size]:
            items = entry.get("items", [])
        else:
            items = parse_file(path)
            changed += 1
            entry = {"sig": [int(st.st_mtime), st.st_size], "items": items}
        new_files[path] = entry
        for it in items:
            k = "%s#%s" % (it["rid"], it.get("step"))
            old = merged.get(k)
            if not old or (it["p"] + it["c"]) >= (old["p"] + old["c"]):
                merged[k] = it

    CACHE["files"] = new_files
    try:
        CACHE_PATH.write_text(json.dumps(CACHE, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass

    return merged, len(files), changed


def load_credits():
    """从本地数据库取每条请求的积分（credit_json 的键就是 conversationRequestId）。"""
    if not DB_PATH.exists():
        return {}
    uri = "file:%s?mode=ro" % DB_PATH.as_posix()
    con = sqlite3.connect(uri, uri=True)
    out = {}
    try:
        for _, cj in con.execute("select session_id, credit_json from session_usage"):
            try:
                d = json.loads(cj or "{}")
            except Exception:
                continue
            for k, v in d.items():
                out[k] = out.get(k, 0) + float(v)
    finally:
        con.close()
    return out


def add_duration(rows):
    """用同会话内相邻事件的时间差近似单次耗时（含客户端与工具开销）。"""
    by_file = collections.defaultdict(list)
    for r in rows:
        by_file[r["file"]].append(r)
    for path, items in by_file.items():
        items.sort(key=lambda x: x["ts"])
        for i in range(1, len(items)):
            gap = (items[i]["ts"] - items[i - 1]["ts"]) / 1000.0
            if 0.5 < gap < 300:
                items[i]["dur"] = round(gap, 2)


def load_session_titles():
    """会话 id → (标题, 工作目录)，用于请求明细里定位是哪次对话。"""
    if not DB_PATH.exists():
        return {}
    out = {}
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB_PATH.as_posix(), uri=True)
        for sid, title, cwd in con.execute("select id, title, cwd from sessions"):
            out[sid] = (title or "", cwd or "")
        con.close()
    except Exception:
        pass
    return out


def build(force=False):
    """汇总一次，返回可直接喂给前端的字典。"""
    req_map, n_files, changed = scan_requests(force=force)
    credits = load_credits()
    titles = load_session_titles()

    rows = []
    missing = 0
    turns = {}
    for _, s in req_map.items():
        rid = s["rid"]
        t = turns.get(rid)
        if t is None:
            t = turns[rid] = {
                "rid": rid, "sid": s.get("sid") or "", "m": s["model"],
                "fam": family_of(s["model"]), "first": s["ts"], "last": s["ts"],
                "p": 0, "c": 0, "ch": 0, "rs": 0, "steps": 0,
                "raw_credit": 0.0, "cost": 0.0, "priced": False,
                "peak_all": True, "peak_any": False, "step_dur": None,
            }
        t["steps"] += 1
        t["p"] += s["p"]
        t["c"] += s["c"]
        t["ch"] += s["ch"]
        t["rs"] += s.get("rs") or 0
        t["first"] = min(t["first"], s["ts"])
        t["last"] = max(t["last"], s["ts"])
        if s.get("credit") is not None:
            t["raw_credit"] += float(s["credit"])
        if s.get("dur"):
            t["step_dur"] = (t["step_dur"] or 0) + s["dur"]
        fam = family_of(s["model"])
        if fam:
            pk = is_peak(s["ts"])
            t["peak_all"] = t["peak_all"] and pk
            t["peak_any"] = t["peak_any"] or pk
            price = PRICES[fam]["peak" if pk else "off"]
            miss = max(0, s["p"] - s["ch"])
            t["cost"] += (
                s["ch"] / 1e6 * price["hit"]
                + miss / 1e6 * price["miss"]
                + s["c"] / 1e6 * price["out"]
            )
            t["priced"] = True
            t["fam"] = fam

    for rid, t in turns.items():
        cr = credits.get(rid)
        if cr is None and t["raw_credit"] > 0:
            cr = t["raw_credit"]
        if cr is None:
            missing += 1
        sid = t["sid"]
        dur = round((t["last"] - t["first"]) / 1000.0, 2) if t["steps"] > 1 else t["step_dur"]
        rows.append({
            "rid": rid,
            "sid": sid,
            "title": titles.get(sid, ("", ""))[0],
            "cwd": titles.get(sid, ("", ""))[1],
            "ts": t["last"],
            "m": t["m"],
            "fam": t["fam"],
            "steps": t["steps"],
            "p": t["p"],
            "c": t["c"],
            "ch": t["ch"],
            "rs": t["rs"],
            "cr": round(cr, 4) if cr is not None else None,
            "hasCr": cr is not None,
            "cost": round(t["cost"], 6) if t["priced"] else None,
            "peak": t["peak_all"] if t["peak_any"] else False,
            "peakMixed": t["peak_any"] and not t["peak_all"],
            "live": (int(time.time() * 1000) - t["last"]) < 10 * 60 * 1000,
            "dur": dur,
        })

    rows.sort(key=lambda x: -x["ts"])

    def total(k):
        return sum((r[k] or 0) for r in rows)

    priced = [r for r in rows if r["cost"] is not None]
    amount = sum(r["cost"] for r in priced)
    p_tok = total("p")
    c_tok = total("c")
    ch_tok = total("ch")

    # 模型聚合
    agg = collections.defaultdict(lambda: {"req": 0, "steps": 0, "cr": 0.0, "p": 0, "c": 0, "ch": 0,
                                           "amt": 0.0, "dur": 0.0, "priced": 0, "billed": 0})
    for r in rows:
        a = agg[r["m"]]
        a["req"] += 1
        a["steps"] += r["steps"]
        a["cr"] += r["cr"] or 0
        if r["hasCr"]:
            a["billed"] += 1
        a["p"] += r["p"]
        a["c"] += r["c"]
        a["ch"] += r["ch"]
        a["dur"] += r["dur"] or 0
        if r["cost"] is not None:
            a["amt"] += r["cost"]
            a["priced"] += 1
    models = []
    for name, a in sorted(agg.items(), key=lambda x: -x[1]["amt"] if x[1]["priced"] else -x[1]["cr"]):
        models.append({
            "name": name,
            "req": a["req"],
            "steps": a["steps"],
            "billed": a["billed"],
            "cr": round(a["cr"], 2),
            "p": a["p"],
            "c": a["c"],
            "ch": a["ch"],
            "hit": round(a["ch"] / a["p"] * 100, 1) if a["p"] else 0,
            "amt": round(a["amt"], 4) if a["priced"] else None,
            "avg": round(a["cr"] / a["req"], 2),
            "crM": round(a["cr"] / (a["p"] / 1e6), 2) if a["p"] else 0,
            "rmM": round(a["amt"] / ((a["p"] + a["c"]) / 1e6), 4) if a["priced"] and (a["p"] + a["c"]) else None,
            "speed": round(a["c"] / a["dur"], 1) if a["dur"] and a["c"] else 0,
        })

    # 近 30 天日序列（按最新请求日期回推）
    max_ts = max(r["ts"] for r in rows) if rows else int(time.time() * 1000)
    start_day = dt.datetime.fromtimestamp(max_ts / 1000, BJ).date() - dt.timedelta(days=29)
    by_day = collections.defaultdict(lambda: {"cr": 0.0, "req": 0, "tok": 0, "amt": 0.0})
    for r in rows:
        key = dt.datetime.fromtimestamp(r["ts"] / 1000, BJ).strftime("%Y-%m-%d")
        d = by_day[key]
        d["cr"] += r["cr"] or 0
        d["req"] += 1
        d["tok"] += r["p"] + r["c"]
        d["amt"] += r["cost"] or 0
    days = []
    for i in range(30):
        key = (start_day + dt.timedelta(days=i)).strftime("%Y-%m-%d")
        v = by_day.get(key, {"cr": 0, "req": 0, "tok": 0, "amt": 0})
        days.append({"d": key, "cr": round(v["cr"], 2), "req": v["req"],
                     "tok": v["tok"], "amt": round(v["amt"], 4)})

    peak_rows = [r for r in priced if r["peak"]]
    billed_rows = [r for r in rows if r["hasCr"]]
    sessions_meta = {}
    for r in rows:
        if r["sid"] and r["sid"] not in sessions_meta:
            sessions_meta[r["sid"]] = {"t": r["title"], "cwd": r["cwd"]}
    data = {
        "generated": dt.datetime.now(BJ).strftime("%Y-%m-%d %H:%M:%S"),
        "files": n_files,
        "changed": changed,
        "noCredit": missing,
        "credits": round(total("cr"), 2),
        "amount": round(amount, 4),
        "pricedReq": len(priced),
        "peakReq": len(peak_rows),
        "requests": len(rows),
        "stepsTotal": total("steps"),
        "billedRequests": len(billed_rows),
        "p": p_tok,
        "c": c_tok,
        "ch": ch_tok,
        "hit": round(ch_tok / p_tok * 100, 2) if p_tok else 0,
        "tokens": p_tok + c_tok,
        "avgCost": round(amount / len(priced), 4) if priced else 0,
        "avgCr": round(total("cr") / len(billed_rows), 2) if billed_rows else 0,
        "medianDur": round(statistics.median([r["dur"] for r in rows if r["dur"]]), 2)
        if any(r["dur"] for r in rows) else 0,
        "models": models,
        "days": days,
        "rows": [{
            "rid": r["rid"], "sid": r["sid"], "ts": r["ts"], "m": r["m"], "fam": r["fam"],
            "steps": r["steps"], "rs": r["rs"], "p": r["p"], "c": r["c"], "ch": r["ch"],
            "cr": r["cr"], "hasCr": r["hasCr"], "cost": r["cost"], "dur": r["dur"],
            "peak": r["peak"], "peakMixed": r["peakMixed"], "live": r["live"],
        } for r in rows],
        "sessions": sessions_meta,
        "prices": PRICES,
    }
    return data


def write_snapshot(data):
    STATE_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tpl = TEMPLATE_PATH.read_text(encoding="utf-8")
    html = tpl.replace("__DATA__", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    SNAPSHOT_PATH.write_text(html, encoding="utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalUsageDashboard/1.0"

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/api/data"):
            with LOCK:
                data = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else build()
            self._send(200, json.dumps(data, ensure_ascii=False))
            return
        if self.path.startswith("/api/refresh"):
            self._refresh()
            return
        page = SNAPSHOT_PATH.read_text(encoding="utf-8") if SNAPSHOT_PATH.exists() else ""
        if not page:
            data = build()
            write_snapshot(data)
            page = SNAPSHOT_PATH.read_text(encoding="utf-8")
        self._send(200, page, "text/html; charset=utf-8")

    def do_POST(self):
        if self.path.startswith("/api/refresh"):
            self._refresh()
            return
        self._send(404, '{"error":"not found"}')

    def _refresh(self):
        t0 = time.time()
        with LOCK:
            data = build(force=True)
            write_snapshot(data)
        data["elapsed"] = round(time.time() - t0, 2)
        print("  [refresh] %d 条请求 / ¥%.4f / 用时 %.2fs" %
              (data["requests"], data["amount"], data["elapsed"]))
        self._send(200, json.dumps(data, ensure_ascii=False))

    def log_message(self, fmt, *args):
        pass


def main():
    ap = argparse.ArgumentParser(description="本地用量看板")
    ap.add_argument("--once", action="store_true", help="只生成静态快照，不启动服务")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--data-dir", help="WorkBuddy 数据目录，默认 ~/.workbuddy")
    args = ap.parse_args()

    if args.data_dir:
        global WB, DB_PATH
        WB = Path(args.data_dir).expanduser().resolve()
        DB_PATH = WB / "workbuddy.db"

    if not WB.exists():
        print("找不到数据目录：%s" % WB)
        print("请确认已安装并运行过 WorkBuddy 客户端，或用 --data-dir 指定目录。")
        print("仍会生成一个空白看板，方便先看界面。")
    elif not DB_PATH.exists():
        print("警告：%s 里没有 workbuddy.db，积分列会为空，只统计 token。" % WB)

    t0 = time.time()
    data = build(force=args.once)
    write_snapshot(data)
    print("扫描 %d 个会话文件（重解析 %d），%d 轮对话，%.1f 万积分，¥%.4f，用时 %.2fs" %
          (data["files"], data["changed"], data["requests"], data["credits"] / 1e4,
           data["amount"], time.time() - t0))

    if args.once:
        print("已生成 %s" % SNAPSHOT_PATH)
        return

    url = "http://127.0.0.1:%d" % args.port
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("看板已启动: %s   (Ctrl+C 停止)" % url)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        srv.shutdown()


if __name__ == "__main__":
    main()
