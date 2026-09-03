"""Claude Code 사용량 실시간 대시보드.

**원천이 둘이다.**

1. **OpenTelemetry (실시간·권위 있음).** Claude Code 는 `claude_code.token.usage`,
   `claude_code.cost.usage` 같은 지표를 OTLP 로 내보낸다. 이건 **Claude Code 자신이
   계산한 값** 이라 추정이 없다. `http/json` 프로토콜을 지원하므로 표준 라이브러리만으로
   받을 수 있다. 이 서버가 곧 수신기다.

2. **세션 기록 (과거·근사).** `~/.claude/projects/**/*.jsonl` 을 증분으로 읽는다.
   텔레메트리를 켜기 *전* 의 사용량은 여기에만 있다. 다만 정확하지 않다 --
   한 API 요청이 여러 레코드로 쪼개져 나오고 각각이 같은 usage 를 달고 있어서
   requestId 로 중복을 제거해야 하고, 그러고도 `/usage` 가 보고하는 수치와
   3배쯤 어긋난다. 원인은 확인하지 못했다. 그래서 **화면에서 두 원천을 섞지 않고
   따로 보여준다.** 섞으면 어느 쪽도 못 믿는다.

텔레메트리 켜기 (이 서버를 띄운 뒤, 새 터미널에서):

    export CLAUDE_CODE_ENABLE_TELEMETRY=1
    export OTEL_METRICS_EXPORTER=otlp
    export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
    export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:8770
    export OTEL_METRIC_EXPORT_INTERVAL=10000

사용:

    python tools/usage_server.py [--port 8770] [--no-open]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import webbrowser
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

PAGE = Path(__file__).with_name("usage.html")
CACHE = Path(__file__).with_name(".usage_cache.json")
OTEL_LOG = Path(__file__).with_name(".usage_otel.jsonl")
ROOT = Path.home() / ".claude" / "projects"

SESSION_WINDOW_H = 5            # 세션 한도의 굴림 창
BIG_CONTEXT = 150_000           # `/usage` 의 ">150k context" 기준
LONG_SESSION_H = 8              # "8+ hours" 기준
LIMIT_RE = re.compile(r"hit your (?:session|usage) limit[^\"]*")

# 백만 토큰당 달러. **세션 기록 쪽에만 쓴다** -- 텔레메트리는 자기 비용을 갖고 온다.
# haiku-4-5 실측(54.5k 입력·2.7k 출력·웹검색 5회 = $0.1181)에 맞춘 값이다.
PRICES = {
    "claude-opus-5":    (5.0, 25.0, 0.50, 6.25, 10.0),
    "claude-opus-4":    (15.0, 75.0, 1.50, 18.75, 30.0),
    "claude-sonnet-5":  (3.0, 15.0, 0.30, 3.75, 6.0),
    "claude-haiku-4-5": (1.0, 5.0, 0.10, 1.25, 2.0),
    "claude-fable-5":   (5.0, 25.0, 0.50, 6.25, 10.0),
}
WEB_SEARCH_PER_CALL = 0.01
DEFAULT_PRICE = PRICES["claude-opus-5"]


def price_of(model: str) -> tuple:
    """모델 이름에 날짜가 붙기도 한다 (`claude-haiku-4-5-20251001`).
    없으면 가장 비싼 쪽으로 가정한다 -- 비용을 낮게 보여주는 쪽으로 틀리면 안 된다."""
    if model in PRICES:
        return PRICES[model]
    pre = [k for k in PRICES if model.startswith(k)]
    return PRICES[max(pre, key=len)] if pre else DEFAULT_PRICE


def _utc(ts: str):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


# --------------------------------------------------------------------------
# 원천 1: OpenTelemetry 수신
# --------------------------------------------------------------------------

class Telemetry:
    """Claude Code 가 밀어주는 지표. 누적 카운터라 **차분** 을 사건으로 바꾼다.

    OTLP 카운터는 프로세스 시작부터의 누계다. 그대로 더하면 갱신할 때마다
    같은 값을 다시 세게 된다. (지표 이름, 속성) 조합마다 마지막 값을 들고 있다가
    늘어난 만큼만 기록한다.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last: dict[str, float] = {}
        self.events: list[dict] = []
        self.seen_metrics: set[str] = set()

    def ingest(self, payload: dict) -> int:
        n = 0
        with self.lock:
            for rm in payload.get("resourceMetrics", []):
                res = _attrs((rm.get("resource") or {}).get("attributes", []))
                for sm in rm.get("scopeMetrics", []):
                    for metric in sm.get("metrics", []):
                        n += self._metric(metric, res)
            if self.events:
                self.events = self.events[-20000:]
        return n

    def _metric(self, metric: dict, res: dict) -> int:
        name = metric.get("name", "")
        self.seen_metrics.add(name)
        data = metric.get("sum") or metric.get("gauge") or {}
        n = 0
        for pt in data.get("dataPoints", []):
            attrs = _attrs(pt.get("attributes", []))
            val = pt.get("asDouble")
            if val is None:
                val = float(pt.get("asInt") or 0)
            key = name + "|" + "|".join(f"{k}={v}" for k, v in sorted(attrs.items()))
            prev = self.last.get(key)
            delta = val if prev is None else val - prev
            self.last[key] = val
            if delta <= 0:
                continue
            ts = pt.get("timeUnixNano")
            when = (datetime.fromtimestamp(int(ts) / 1e9, timezone.utc).isoformat(
                timespec="seconds") if ts else datetime.now(timezone.utc).isoformat(
                timespec="seconds"))
            self.events.append({
                "ts": when, "metric": name, "value": delta,
                "model": attrs.get("model", ""),
                "type": attrs.get("type", ""),
                "session": attrs.get("session.id", "") or attrs.get("session_id", ""),
                "org": res.get("service.name", ""),
            })
            n += 1
        return n

    def snapshot(self, hours: float | None = None) -> dict:
        with self.lock:
            evs = list(self.events)
        if hours is not None:
            lo = datetime.now(timezone.utc) - timedelta(hours=hours)
            evs = [e for e in evs if (_utc(e["ts"]) or lo) >= lo]
        tok = defaultdict(float)
        cost = 0.0
        by_model = defaultdict(lambda: defaultdict(float))
        for e in evs:
            if e["metric"] == "claude_code.token.usage":
                tok[e["type"] or "?"] += e["value"]
                by_model[e["model"] or "?"][e["type"] or "?"] += e["value"]
            elif e["metric"] == "claude_code.cost.usage":
                cost += e["value"]
        return {
            "live": bool(self.events),
            "events": len(evs),
            "tokens": {k: int(v) for k, v in tok.items()},
            "total_tokens": int(sum(tok.values())),
            "cost": round(cost, 4),
            "by_model": {m: {k: int(v) for k, v in d.items()} for m, d in by_model.items()},
            "metrics_seen": sorted(self.seen_metrics),
            "sessions": len({e["session"] for e in evs if e["session"]}),
        }


def _attrs(items: list) -> dict:
    out = {}
    for a in items or []:
        v = a.get("value") or {}
        out[a.get("key", "")] = (v.get("stringValue") or v.get("intValue")
                                 or v.get("doubleValue") or v.get("boolValue"))
    return out


# --------------------------------------------------------------------------
# 원천 2: 세션 기록
# --------------------------------------------------------------------------

class Store:
    def __init__(self) -> None:
        self.offsets: dict[str, int] = {}
        self.calls: list[dict] = []
        self.limits: list[dict] = []
        self._seen: set[str] = set()
        self._load()

    def _load(self) -> None:
        try:
            d = json.loads(CACHE.read_text(encoding="utf-8"))
            self.offsets, self.calls, self.limits = d["offsets"], d["calls"], d["limits"]
            self._seen = {c.get("rid") for c in self.calls}
        except Exception:
            pass

    def save(self) -> None:
        try:
            CACHE.write_text(json.dumps({"offsets": self.offsets, "calls": self.calls,
                                         "limits": self.limits}), encoding="utf-8")
        except Exception:
            pass

    def refresh(self) -> int:
        added = 0
        for f in ROOT.rglob("*.jsonl"):
            key, start = str(f), self.offsets.get(str(f), 0)
            try:
                size = f.stat().st_size
            except OSError:
                continue
            if size == start:
                continue
            if size < start:
                start = 0                      # 회전됐다면 처음부터
            try:
                with f.open("rb") as fh:
                    fh.seek(start)
                    blob = fh.read()
                    self.offsets[key] = fh.tell()
            except OSError:
                continue
            added += self._ingest(blob, f)
        self.calls.sort(key=lambda c: c["ts"])
        self.limits.sort(key=lambda c: c["ts"])
        return added

    def _ingest(self, blob: bytes, path: Path) -> int:
        n = 0
        for raw in blob.decode("utf-8", errors="replace").splitlines():
            if '"usage"' not in raw and "limit" not in raw:
                continue                        # json 파싱 전에 싸게 거른다
            try:
                d = json.loads(raw)
            except Exception:
                continue
            if d.get("type") != "assistant":
                continue
            msg, ts = d.get("message") or {}, d.get("timestamp") or ""
            u = msg.get("usage") or {}

            # 한도 안내도 usage 를 달고 온다 (전부 null 인 채로). 따로 봐야 한다.
            m = LIMIT_RE.search(json.dumps(msg.get("content") or "", ensure_ascii=False))
            if m:
                self.limits.append({"ts": ts, "text": m.group(0)})

            if not (u.get("output_tokens") or u.get("input_tokens")
                    or u.get("cache_read_input_tokens")):
                continue
            # 한 요청이 여러 레코드로 쪼개지고 각각 같은 usage 를 단다.
            # 이 세션만 레코드 1,535건에 requestId 852개였다 -- 그대로 더하면 1.8배가 된다.
            rid = d.get("requestId") or d.get("uuid")
            if rid in self._seen:
                continue
            self._seen.add(rid)
            cw = u.get("cache_creation") or {}
            self.calls.append({
                "rid": rid, "ts": ts, "model": msg.get("model") or "?",
                "session": d.get("sessionId") or "", "project": path.parent.name,
                "in": int(u.get("input_tokens") or 0),
                "out": int(u.get("output_tokens") or 0),
                "cr": int(u.get("cache_read_input_tokens") or 0),
                "c5": int(cw.get("ephemeral_5m_input_tokens") or 0),
                "c1": int(cw.get("ephemeral_1h_input_tokens") or 0),
                "web": int((u.get("server_tool_use") or {}).get("web_search_requests") or 0),
            })
            n += 1
        return n


def cost_of(c: dict) -> float:
    p = price_of(c["model"])
    return ((c["in"] * p[0] + c["out"] * p[1] + c["cr"] * p[2]
             + c["c5"] * p[3] + c["c1"] * p[4]) / 1_000_000
            + c["web"] * WEB_SEARCH_PER_CALL)


def tokens_of(c: dict) -> int:
    return c["in"] + c["out"] + c["cr"] + c["c5"] + c["c1"]


def _bucket(calls: list) -> dict:
    return {
        "calls": len(calls), "tokens": sum(tokens_of(c) for c in calls),
        "cost": round(sum(cost_of(c) for c in calls), 2),
        "in": sum(c["in"] for c in calls), "out": sum(c["out"] for c in calls),
        "cache_read": sum(c["cr"] for c in calls),
        "cache_write": sum(c["c5"] + c["c1"] for c in calls),
    }


def observed_limits(store: Store) -> list[dict]:
    """실제로 막힌 순간의 5시간 사용량. 한도의 **관측값** 이다.

    수치가 공개돼 있지 않으니 이것이 유일한 근거다. 값이 흔들리는 것 자체가 정보다 --
    한도가 토큰 하나로 정해지지 않는다는 뜻이니까.
    """
    out = []
    for lim in store.limits:
        t = _utc(lim["ts"])
        if not t:
            continue
        lo = t - timedelta(hours=SESSION_WINDOW_H)
        win = [c for c in store.calls if lo <= (_utc(c["ts"]) or t) <= t]
        if win:
            out.append({"ts": lim["ts"], "text": lim["text"],
                        "tokens": sum(tokens_of(c) for c in win)})
    return out[-24:]


def contributors(calls: list) -> list[dict]:
    """`/usage` 의 "무엇이 한도를 쓰고 있나". 서로 독립적인 특성이지 분해가 아니다."""
    if not calls:
        return []
    total = sum(tokens_of(c) for c in calls) or 1
    stamps = sorted((_utc(c["ts"]), c["session"], tokens_of(c))
                    for c in calls if _utc(c["ts"]))
    par = 0
    for i, (t, _s, tok) in enumerate(stamps):
        near = {sj for tj, sj, _ in stamps[max(0, i - 80):i + 80]
                if abs((tj - t).total_seconds()) <= 120}
        if len(near) >= 4:
            par += tok
    big = sum(tokens_of(c) for c in calls
              if c["cr"] + c["c5"] + c["c1"] + c["in"] > BIG_CONTEXT)
    span: dict[str, list] = {}
    for c in calls:
        t = _utc(c["ts"])
        if t:
            lo, hi = span.get(c["session"], [t, t])
            span[c["session"]] = [min(lo, t), max(hi, t)]
    long_ids = {s for s, (lo, hi) in span.items()
                if hi - lo >= timedelta(hours=LONG_SESSION_H)}
    lng = sum(tokens_of(c) for c in calls if c["session"] in long_ids)
    return [
        {"pct": round(par / total * 100), "label": "4개 이상의 세션이 동시에 돌던 중",
         "hint": "모든 세션이 한 한도를 나눠 쓴다. 동시에 필요하지 않다면 줄 세우는 편이 고르다"},
        {"pct": round(big / total * 100), "label": f"문맥 {BIG_CONTEXT // 1000}k 초과에서",
         "hint": "긴 세션은 캐시가 있어도 비싸다. 작업 중엔 /compact, 주제가 바뀌면 /clear"},
        {"pct": round(lng / total * 100), "label": f"{LONG_SESSION_H}시간 이상 살아 있던 세션에서",
         "hint": "배경·루프 세션인 경우가 많다. 의도한 것인지 확인할 값어치가 있다"},
    ]


def snapshot(store: Store, tel: Telemetry) -> dict:
    now = datetime.now(timezone.utc)

    def since(h):
        lo = now - timedelta(hours=h)
        return [c for c in store.calls if (_utc(c["ts"]) or now) >= lo]

    win = since(SESSION_WINDOW_H)
    obs = observed_limits(store)
    ref = sorted(o["tokens"] for o in obs)[len(obs) // 2] if obs else 0
    cur = sum(tokens_of(c) for c in win)
    day = since(24)

    by_model, by_day, by_proj = {}, {}, {}
    for c in store.calls:
        by_model.setdefault(c["model"], []).append(c)
        by_day.setdefault(c["ts"][:10], []).append(c)
    for c in day:
        by_proj.setdefault(c["project"] or "?", []).append(c)

    return {
        "now": now.isoformat(timespec="seconds"),
        "window_h": SESSION_WINDOW_H,
        "telemetry": {
            "all": tel.snapshot(),
            "window": tel.snapshot(SESSION_WINDOW_H),
            "day": tel.snapshot(24),
        },
        "transcripts": {
            "window": _bucket(win), "day": _bucket(day),
            "week": _bucket(since(24 * 7)), "all": _bucket(store.calls),
            "reference_limit": ref,
            "pct": round(cur / ref * 100) if ref else None,
            "by_model": {m: _bucket(v) for m, v in sorted(by_model.items())},
            "by_project": sorted(({"name": k, **_bucket(v)} for k, v in by_proj.items()),
                                 key=lambda d: -d["tokens"])[:8],
            "daily": [{"date": d, **_bucket(v)} for d, v in sorted(by_day.items())][-30:],
            "contributors": contributors(day),
            "observed_limits": obs,
            "last_limit": store.limits[-1] if store.limits else None,
            "live_sessions": len({c["session"] for c in since(1 / 6)}),
            "files": len(store.offsets),
        },
    }


# --------------------------------------------------------------------------

def make_handler(store: Store, tel: Telemetry):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, obj, code=200, ctype="application/json"):
            body = (obj if isinstance(obj, bytes)
                    else json.dumps(obj, ensure_ascii=False).encode("utf-8"))
            self.send_response(code)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                return self._send(PAGE.read_bytes(), ctype="text/html")
            if self.path.startswith("/api/usage"):
                n = store.refresh()
                if n:
                    store.save()
                snap = snapshot(store, tel)
                snap["new_calls"] = n
                return self._send(snap)
            self._send({"error": "not found"}, 404)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            # OTLP/HTTP 는 엔드포인트 뒤에 /v1/metrics 를 붙인다.
            if self.path.endswith("/v1/metrics"):
                try:
                    got = tel.ingest(json.loads(raw))
                except Exception as exc:
                    return self._send({"error": str(exc)}, 400)
                if got:
                    try:
                        with OTEL_LOG.open("a", encoding="utf-8") as fh:
                            fh.write(raw.decode("utf-8", "replace").strip() + "\n")
                    except Exception:
                        pass
                # OTLP 는 빈 JSON 객체를 성공으로 본다
                return self._send({})
            if self.path.endswith("/v1/logs") or self.path.endswith("/v1/traces"):
                return self._send({})           # 받되 쓰지 않는다
            self._send({"error": "not found"}, 404)

    return H


def main() -> int:
    ap = argparse.ArgumentParser(description="Claude Code 사용량 실시간 대시보드")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    store, tel = Store(), Telemetry()
    if ROOT.exists():
        print(f"세션 기록을 읽는 중... ({ROOT})")
        n = store.refresh()
        store.save()
        print(f"  호출 {len(store.calls):,}건 · 한도 도달 {len(store.limits)}회 (새로 {n:,})")
    else:
        print(f"세션 기록 없음: {ROOT}")

    url = f"http://127.0.0.1:{args.port}"
    srv = HTTPServer(("127.0.0.1", args.port), make_handler(store, tel))
    print(f"\n  {url}  ← 대시보드\n")
    print("실시간 지표를 받으려면 새 터미널에서 이 값들을 켜고 claude 를 쓰세요:")
    print("  CLAUDE_CODE_ENABLE_TELEMETRY=1  OTEL_METRICS_EXPORTER=otlp")
    print(f"  OTEL_EXPORTER_OTLP_PROTOCOL=http/json  OTEL_EXPORTER_OTLP_ENDPOINT={url}")
    print("  OTEL_METRIC_EXPORT_INTERVAL=10000\n")
    print("Ctrl+C 로 종료")
    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n종료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
