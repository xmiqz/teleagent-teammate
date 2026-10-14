# -*- coding: utf-8 -*-
"""watch.py — TeleAgent 多会话无人值守值班循环（零三方依赖）。

一个进程同时看守多个 TeleAgent 会话：
  1. 短窗收割（默认 45s），到期自动续窗，直到全部完成/总时长到点；
  2. 每轮轮询【仅属于本值班的会话】的授权队列，按 policy.py 三级口径
     自动放行 / 宽申驳回+纠偏 / 升级；
  3. 完成、异常中止、需升级时写 JSONL 事件。默认【零弹窗零响铃】：
     一切异常以结构化事件交给调用方智能体处置（人工不碰 TeleAgent 界面）；
     仅在显式 --popup 时才响铃+置顶弹窗。

退出码：0 全部完成或需升级（看 JSONL 末行 event）
        2 总值班到点（续值班即可，非失败）
        3 任一会话异常中止
        4 出现需上层处置的授权（调用方智能体应自行 reply/prompt，不打扰人）

直接运行：
  python watch.py <sid1> [sid2 ...] --workspace <dir> --allow <dir> [options]
"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import teammate as tac  # noqa: E402
from policy import Policy, default_toolchain_roots  # noqa: E402


class Watcher:
    def __init__(self, args, client=None, own_process_lifecycle=True):
        self.sessions = list(dict.fromkeys(args.sessions))
        self.deadline = time.time() + args.max
        self.window = args.window
        self.tag = args.tag
        self.log_path = os.path.abspath(args.log)
        os.makedirs(os.path.dirname(self.log_path) or ".", exist_ok=True)
        self.policy = Policy(
            args.workspace, args.source,
            list(dict.fromkeys(args.allow + default_toolchain_roots())),
            strict=getattr(args, "strict", False))
        self.client = client or tac.TeleAgentClient()
        self.round_no = 0
        # 默认零弹窗：异常一律结构化上交调用方智能体；--popup 才启用桌面提醒
        self.popup = bool(getattr(args, "popup", False))
        self.interval = int(getattr(args, "interval", 5))
        self.own_lifecycle = own_process_lifecycle
        # (sid, patterns指纹) -> 宽申已驳回次数；从历史 JSONL 恢复，重挂不清零
        self.broad_seen = self._load_broad_history()

    # ---------------- 事件与告警 ----------------

    def emit(self, event, **kw):
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
               "round": self.round_no, "event": event, **kw}
        line = json.dumps(rec, ensure_ascii=False)
        print(line, flush=True)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def jsonl_only(self, rec):
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
               "round": self.round_no, **rec}
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def alert(self, title, message):
        """仅在显式 --popup 时响铃+置顶弹窗；默认完全静默（事件已落 JSONL）。"""
        if not self.popup:
            return
        if sys.platform == "win32":
            try:
                import winsound
                for _ in range(3):
                    winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                    time.sleep(0.35)
            except Exception:
                pass
        else:
            sys.stdout.write("\a")
            sys.stdout.flush()
        if sys.platform != "win32":
            return
        code = (
            "import ctypes,sys;"
            "ctypes.windll.user32.MessageBoxW(0,sys.argv[2],sys.argv[1],"
            "0x1000|0x40|0x10000)")  # MB_SYSTEMMODAL|MB_ICONINFORMATION|MB_SETFOREGROUND
        try:
            subprocess.Popen([sys.executable, "-c", code, title, message],
                             creationflags=0x00000008)  # DETACHED_PROCESS
        except Exception:
            pass

    # ---------------- 宽申历史 ----------------

    @staticmethod
    def _broad_key(req):
        sid = req.get("sessionID") or ""
        pats = tuple(sorted(req.get("patterns") or req.get("always") or []))
        return sid, pats

    def _load_broad_history(self):
        seen = {}
        try:
            with open(self.log_path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("event") == "PERMISSION_BROAD_REJECT":
                        key = (rec.get("session") or "",
                               tuple(rec.get("patterns_key") or []))
                        seen[key] = seen.get(key, 0) + 1
        except FileNotFoundError:
            pass
        except Exception:
            pass
        return seen

    # ---------------- 授权队列（仅处理自己看守的会话） ----------------

    def _inject(self, sid, text):
        try:
            pid, mid, _ = tac.resolve_model(self.client, None)
            st, _ = tac.send_prompt(self.client, sid, text, pid, mid)
            return st
        except Exception as e:
            return f"err:{str(e)[:120]}"

    def _reply(self, pid, reply):
        """回复授权。返回 (ok, status)；404=该请求已不存在（被覆盖/消费）。"""
        try:
            st, _ = self.client.call(
                "POST", f"/permission/{pid}/reply",
                {"reply": reply}, timeout=15)
            return True, st
        except Exception as e:
            msg = str(e)
            if "404" in msg:
                return True, "gone:404"
            return False, f"err:{msg[:120]}"

    def poll_permissions(self):
        """处理授权队列中归属本值班会话的请求；返回仍需上层处置的请求列表。"""
        try:
            _, pending = self.client.call("GET", "/permission", timeout=20)
        except Exception as e:
            self.emit("poll_error", error=str(e)[:200])
            return []
        held = []
        for req in pending or []:
            if req.get("sessionID") not in self.sessions:
                continue  # 绝不碰其他会话/其他操作者的授权
            pid = req.get("id")
            decision = self.policy.decide(req)
            if decision == "session" and pid:
                ok, st = self._reply(pid, "session")
                if ok:
                    self.emit("PERMISSION_AUTO", permission_id=pid,
                              decision="session", status=st,
                              session=req.get("sessionID"),
                              patterns=req.get("patterns"))
                else:
                    self.emit("PERMISSION_REPLY_ERROR", permission_id=pid,
                              error=st)
            elif decision == "broad" and pid:
                sid = req.get("sessionID") or ""
                key = self._broad_key(req)
                n = self.broad_seen.get(key, 0) + 1
                self.broad_seen[key] = n
                if n >= 2:
                    held.append(dict(req, _broad_attempts=n))
                    continue
                ok, st = self._reply(pid, "reject")
                if not ok:
                    self.emit("PERMISSION_REPLY_ERROR", permission_id=pid,
                              error=st)
                inj = self._inject(sid, self.policy.correction_text(req))
                self.emit("PERMISSION_BROAD_REJECT", permission_id=pid,
                          status=st, inject_status=inj, session=sid,
                          attempt=n, patterns=req.get("patterns"),
                          patterns_key=list(key[1]))
            else:
                held.append(req)
        return held

    def _hold(self, held, why):
        # 只落事件：调用方智能体读 PERMISSION_MANUAL / fallback_needed 后自行裁决
        self.emit("PERMISSION_MANUAL", why=why, items=held)
        self.alert("TeleAgent 值班：需要授权裁决",
                   f"{len(held)} 条需上层处置（{self.tag}）：{why}。")
        return 4

    # ---------------- 收割窗 ----------------

    def _held_verdict(self, sid):
        """轮询授权；有自动可裁的直接处理，有需上交的返回 (rc, event)。"""
        held = self.poll_permissions()
        if not held:
            return None
        why = ("宽申自动纠偏无效（第2次原样申请）"
               if any(x.get("_broad_attempts") for x in held)
               else "出现出界/危险授权")
        self._hold(held, why)
        return 4, {"event": "permission_manual", "session_id": sid,
                   "why": why, "held": held}

    def tick_callback(self, sid, start=None, stall_seconds=0):
        """窗口内每轮回调：先裁授权，再判停滞。回调异常由 wait_window 兜底。"""
        state = {"last_growth": start if start is not None else time.time(),
                 "last_sig": None}

        def cb(info):
            verdict = self._held_verdict(sid)
            if verdict:
                return verdict
            sig = (info.get("state"), info.get("messages"),
                   len(info.get("tools") or []))
            now = time.time()
            if sig != state["last_sig"]:
                state["last_sig"] = sig
                state["last_growth"] = now
            stalled_for = now - state["last_growth"]
            if stall_seconds and stalled_for >= stall_seconds:
                event = {"event": "stalled", "session_id": sid,
                         "elapsed": round(now - (start or now), 1),
                         "stalled_for": int(stalled_for),
                         "hint": "会话忙但长期零进展，按停滞处理"}
                self.jsonl_only(event)
                return 6, event
            return None
        return cb

    def _window(self, sid, quiet=True, on_tick=None):
        """一个收割窗；返回 (rc, event)。"""
        return tac.wait_window(self.client, sid, self.window,
                               interval=self.interval, quiet=quiet,
                               on_tick=on_tick)

    # ---------------- 单会话一体化（teammate run 调用） ----------------

    def run_single(self, sid, timeout, output_file=None, title=None,
                   quiet=True, stall_seconds=600):
        """看守单会话到终态。返回 dict：{rc, event, held}。

        rc: 0 完成 / 2 总时长到点 / 3 会话中止 / 1 硬错误 /
            4 授权需上层裁决（held 带明细）/ 6 停滞（消息与工具长期零增长）
        """
        self.emit("run_start", sessions=[sid], timeout=timeout,
                  window=self.window, tag=self.tag,
                  ws_safe=self.policy.ws, toolchain=self.policy.ok,
                  popup=self.popup, stall_seconds=stall_seconds)
        start = time.time()
        deadline = start + timeout
        on_tick = self.tick_callback(sid, start=start,
                                     stall_seconds=stall_seconds)
        while time.time() < deadline:
            self.round_no += 1
            remain = deadline - time.time()
            old_window = self.window
            self.window = int(min(old_window, max(5, remain)))
            try:
                rc, event = self._window(sid, quiet=quiet, on_tick=on_tick)
            finally:
                self.window = old_window
            if rc == 2:
                self.emit("tick", remaining_sec=int(deadline - time.time()))
                continue
            if rc in (0, 1, 3, 6):
                self.jsonl_only(event)
                tac.finalize_event(sid, event, rc,
                                   output_file=output_file, title=title)
                return {"rc": rc, "event": event, "held": []}
            if rc == 4:
                # _hold 已落 PERMISSION_MANUAL 事件；held 明细随事件上交
                tac.finalize_event(sid, event, rc,
                                   output_file=output_file, title=title)
                return {"rc": rc, "event": event,
                        "held": event.get("held") or []}
        event = {"event": "timeout", "session_id": sid,
                 "elapsed": round(time.time() - start, 1),
                 "hint": "总时长到点：harvest --wait 可续窗"}
        self.jsonl_only(event)
        tac.finalize_event(sid, event, 2, output_file=output_file, title=title)
        return {"rc": 2, "event": event, "held": []}

    # ---------------- 多会话值班（CLI / 编排器参考） ----------------

    def run(self):
        done = set()
        self.emit("duty_start", sessions=self.sessions,
                  max_seconds=int(self.deadline - time.time()),
                  window=self.window, tag=self.tag,
                  ws_safe=self.policy.ws, toolchain=self.policy.ok)
        while time.time() < self.deadline:
            self.round_no += 1

            for sid in list(self.sessions):
                if sid in done:
                    continue
                # 授权裁决在窗口内每轮执行（不能整窗阻塞，否则中途申请无人裁）
                cb = self.tick_callback(sid)
                rc, event = self._window(sid, quiet=True, on_tick=cb)
                self.emit("harvest", session=sid, rc=rc,
                          event=event.get("event"),
                          elapsed=event.get("elapsed"))
                if rc == 0:
                    done.add(sid)
                    self.emit("SESSION_DONE", session=sid)
                elif rc in (3, 1):
                    self.emit("ABORTED", session=sid, rc=rc,
                              note="调用方智能体需立即接续/重派/自行接手")
                    self.alert("TeleAgent 值班：会话异常中止",
                               f"{sid[:16]}…（{self.tag}）收割 rc={rc}。")
                    return 3
                elif rc == 4:
                    return 4
                # rc == 2 窗口到期，继续下一个
            if len(done) == len(self.sessions):
                self.emit("ALL_DONE", count=len(done))
                self.alert("TeleAgent 值班：全部完成",
                           f"{self.tag}：{len(done)} 个会话已完成。")
                return 0
            self.emit("tick", done=len(done), total=len(self.sessions),
                      remaining_sec=int(self.deadline - time.time()))

        self.emit("DUTY_TIMEOUT", tag=self.tag, note="总值班到点，请续值班")
        self.alert("TeleAgent 值班：窗口到点",
                   f"{self.tag} 值班到点，请续值班。")
        return 2


def main():
    ap = argparse.ArgumentParser(description="TeleAgent 多会话值班循环")
    ap.add_argument("sessions", nargs="+")
    ap.add_argument("--max", type=int, default=7200, dest="max")
    ap.add_argument("--window", type=int, default=45)
    ap.add_argument("--tag", default="watch")
    ap.add_argument("--workspace", action="append", default=[])
    ap.add_argument("--source", action="append", default=[])
    ap.add_argument("--allow", action="append", default=[])
    ap.add_argument("--strict", action="store_true",
                    help="宽申第一次即升级（默认自动驳回+纠偏）")
    ap.add_argument("--log", default=None)
    ap.add_argument("--popup", action="store_true",
                    help="启用响铃+置顶弹窗（默认静默，只产出结构化事件）")
    args = ap.parse_args()
    if not args.log:
        args.log = os.path.join(os.getcwd(), f"duty_{args.tag}.jsonl")
    sys.exit(Watcher(args).run())


if __name__ == "__main__":
    main()
