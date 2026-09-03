"""자판기 조작 화면 — 실제 산출물을 그대로 구동한다.

**왜 서버인가.** 브라우저에서 돌리려면 JS로 다시 짜야 하는데, v7 구현은
guard·action 이 115개 함수 3,132줄이다. 다시 짜면 원본과 갈라지고,
그러면 화면을 조작해도 *그 제품* 을 시험한 게 아니게 된다.
그래서 `app.vending_machine.VendingMachine` 을 그대로 import 해서 구동한다.
화면은 이벤트를 보내고 관측값을 받아 그릴 뿐, 판단은 전부 제품이 한다.

    python tools/vm_gui.py [--repo <경로>] [--port 8765]
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

PAGE = Path(__file__).with_name("vm_gui.html")


class Machine:
    """제품 한 대와 그 전이 기록. 화면이 새로고침돼도 상태가 유지되도록 서버가 들고 있다."""

    def __init__(self, repo: Path, save_dir: Path):
        sys.path.insert(0, str(repo))
        from app import _fsm_spec
        from app.vending_machine import DENOMINATIONS, VendingMachine

        self.repo = repo
        self.spec = _fsm_spec
        self.cls = VendingMachine
        self.denoms = list(DENOMINATIONS)
        self.save_path = save_dir / "gui_state.json"
        self.log: list[dict] = []
        self.reset()

    # --- 초기값. 실제 자판기처럼 3종·서로 다른 가격·거스름돈 동전을 갖춰 둔다. ---
    DEFAULTS = dict(
        prices={"cola": 700, "cider": 500, "water": 400},
        stock={"cola": 3, "cider": 2, "water": 5},
        coins={1000: 2, 500: 5, 100: 10},
    )

    def reset(self, stocked: bool = True) -> None:
        """새 기계를 세운다.

        `stocked=True` 면 관리자가 채워 두고 간 자판기처럼 **유효한 저장 파일을 먼저 만든다.**
        제품은 POWER_ON 때 저장 파일에서 장부를 복원하므로(요구사항 30),
        파일이 없으면 재고 0 으로 기동하고 관리자를 부른다 -- 그게 정상 동작이다.
        `stocked=False` 는 그 '처음 설치' 경로를 시험할 때 쓴다.
        """
        self.save_path.unlink(missing_ok=True)
        defaults = {k: dict(v) for k, v in self.DEFAULTS.items()}
        if stocked:
            seed = self.cls(**defaults, state="SELLING_IDLE", save_path=str(self.save_path))
            seed.persist()
        self.vm = self.cls(**defaults, save_path=str(self.save_path))
        self.log.clear()
        self._note("(초기화)" + ("" if stocked else " · 빈 기계"), "-", None, None)

    def _note(self, label, before, tid, error, event="", effect="") -> None:
        self.log.append({
            "n": len(self.log) + 1, "label": label, "before": before,
            "after": self.vm.state, "tid": tid, "error": error, "effect": effect,
            "diag": self._diagnose(before, tid, event),
        })

    def _diagnose(self, before, tid, event) -> str:
        """전이가 안 일어났으면 **어디서는 되는지** 알려준다.

        "전이 없음"만 보여주면 막다른 길이다. 명세 표에 어느 상태가 이 이벤트를
        처리하는지 다 있으므로, 그걸 힌트로 돌려준다.
        """
        if tid or before == "-":
            return ""
        handlers = sorted({t["src"] for t in self.spec.TRANSITIONS if t["event"] == event})
        if not handlers:
            return f"'{event}' 를 처리하는 상태가 명세에 없습니다"
        if before in handlers:
            return f"'{before}' 에 이 이벤트의 전이는 있으나 guard 가 모두 거짓입니다"
        return f"이 이벤트는 {', '.join(handlers)} 에서만 처리됩니다"

    def snapshot(self) -> dict:
        vm = self.vm
        get = lambda n, d=None: getattr(vm, n, d)
        pending = get("pending")
        return {
            "state": vm.state,
            "credit": get("credit", 0),
            "display": str(get("display", "")),
            "stock": dict(get("stock", {}) or {}),
            "coins": {str(k): v for k, v in (get("coins", {}) or {}).items()},
            "prices": dict(get("_prices", {}) or {}),
            "deposit": get("deposit", 0),
            "sales": get("sales", 0),
            "serial": get("serial", 0),
            "buttons": sorted(get("enabled_buttons", set()) or []),
            "dispensed": list(get("dispensed", []) or []),
            "refunded": list(get("refunded", []) or []),
            "ejected": [str(x) for x in (get("ejected", []) or [])],
            "notifications": [str(x) for x in (get("notifications", []) or [])][-6:],
            "late_log": len(get("late_dispense_log", []) or []),
            "admin_reserved": bool(get("admin_reserved", False)),
            "fault_pending": bool(get("fault_pending", False)),
            "pending": ({"serial": pending.get("serial"), "drink": pending.get("drink")}
                        if isinstance(pending, dict) else None),
            "saved": self.save_path.exists(),
            # 지금 상태에서 전이가 정의된 이벤트. 화면이 "무엇을 누를 수 있는지" 표시하는 데 쓴다.
            "available": sorted({t["event"] for t in self.spec.TRANSITIONS
                                 if t["src"] == vm.state}),
        }

    # 관측 가능한 값들. 전이가 발동해도 이것들이 안 바뀌면 사실상 아무 일도 안 일어난 것이다.
    OBSERVED = ("credit", "stock", "coins", "dispensed", "refunded", "ejected",
                "deposit", "sales", "pending", "late_log", "admin_reserved", "fault_pending")

    def fire(self, event: str, payload: dict) -> dict:
        before_state = self.vm.state
        before = self.snapshot()
        tid = err = None
        try:
            tid = self.vm.handle(event, **payload)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
        after = self.snapshot()
        label = event + ("  " + " ".join(f"{k}={v}" for k, v in payload.items()) if payload else "")
        self._note(label, before_state, tid, err, event, self._effect(before, after))
        return {"snapshot": after, "log": self.log[-200:]}

    def _effect(self, a: dict, b: dict) -> str:
        """무엇이 바뀌었는가. 전이 번호만 보여주면 '처리됨'과 '거절됨'을 구별할 수 없다.

        T-028 처럼 거절 갈래도 전이 번호를 갖는다. 사용자에겐 둘 다 성공처럼 보인다.
        """
        parts = []
        for k in self.OBSERVED:
            x, y = a.get(k), b.get(k)
            if x == y:
                continue
            if k in ("dispensed", "refunded", "ejected"):
                added = y[len(x):] if isinstance(y, list) and isinstance(x, list) else y
                if added:
                    nm = {"dispensed": "배출", "refunded": "반환", "ejected": "되돌림"}[k]
                    parts.append(f"{nm} {', '.join(map(str, added))}")
            elif k == "stock":
                for d in y:
                    if x.get(d) != y.get(d):
                        parts.append(f"{d} 재고 {x.get(d)}→{y.get(d)}")
            elif k == "coins":
                ch = [f"{d}원 {x.get(d)}→{y.get(d)}" for d in y if x.get(d) != y.get(d)]
                if ch:
                    parts.append("동전 " + ", ".join(ch))
            elif k == "pending":
                parts.append(f"배출 개시 #{y['serial']} {y['drink']}" if y else "배출 정산")
            elif k in ("admin_reserved", "fault_pending"):
                nm = {"admin_reserved": "관리 예약", "fault_pending": "고장 미확인"}[k]
                parts.append(f"{nm} {'설정' if y else '해제'}")
            else:
                nm = {"credit": "잔액", "deposit": "보관액", "sales": "매출",
                      "late_log": "지연배출"}[k]
                parts.append(f"{nm} {x}→{y}")
        return " · ".join(parts)

    def meta(self) -> dict:
        return {
            "events": self.spec.EVENTS,
            "states": sorted({t["src"] for t in self.spec.TRANSITIONS}
                             | {t["dst"] for t in self.spec.TRANSITIONS}),
            "transitions": len(self.spec.TRANSITIONS),
            "denoms": self.denoms,
            "project": getattr(self.spec, "PROJECT", "vending_machine"),
        }


def make_handler(machine: Machine):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):        # 콘솔을 조용히
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
                self._send(PAGE.read_bytes(), ctype="text/html")
            elif self.path == "/api/meta":
                self._send({**machine.meta(), "snapshot": machine.snapshot(),
                            "log": machine.log})
            else:
                self._send({"error": "not found"}, 404)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                req = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._send({"error": "bad json"}, 400)
            try:
                if self.path == "/api/reset":
                    machine.reset(stocked=bool(req.get("stocked", True)))
                    return self._send({"snapshot": machine.snapshot(), "log": machine.log})
                if self.path == "/api/event":
                    return self._send(machine.fire(req.get("event", ""),
                                                   req.get("payload") or {}))
                self._send({"error": "not found"}, 404)
            except Exception:
                self._send({"error": traceback.format_exc()[-1500:]}, 500)

    return H


def main() -> int:
    ap = argparse.ArgumentParser(description="v7 자판기 조작 화면")
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]
                                          / "baselines" / "2026-09-02_vending_machine_v7" / "repo"))
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    if not (repo / "app" / "vending_machine.py").exists():
        print(f"제품을 찾을 수 없습니다: {repo}/app/vending_machine.py", file=sys.stderr)
        return 1

    save_dir = Path(__file__).with_name(".vm_gui")
    save_dir.mkdir(exist_ok=True)
    machine = Machine(repo, save_dir)

    srv = HTTPServer(("127.0.0.1", args.port), make_handler(machine))
    print(f"제품: {repo}")
    print(f"전이 {machine.meta()['transitions']}개 · 이벤트 {len(machine.meta()['events'])}종")
    print(f"\n  http://127.0.0.1:{args.port}  ← 브라우저에서 열기\n")
    print("Ctrl+C 로 종료")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n종료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
