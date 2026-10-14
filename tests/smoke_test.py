#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""smoke_test.py — teleagent-teammate 零依赖全链路冒烟。

用脚本内的假 TeleAgent HTTP 服务（忽略 HMAC 签名）演练：
  1. policy 三级裁决纯逻辑
  2. procutil 判活
  3. doctor / run 正常完成
  4. run 会话 abort -> fallback_needed(reason=session_aborted, rc=3)
  5. run 停滞 -> fallback_needed(reason=stalled, rc=6)
  6. run 出界授权 -> fallback_needed(reason=permission_required, rc=4)
  7. 宽申：首次自动 reject+注入纠偏，第 2 次上交 rc=4
  8. reply 404（授权已被消费）-> gone:404，任务照常完成
  9. resume --abort-old 带断点续派，新会话收到原任务书
 10. batch：dispatch_group + shell + notify 跑到 complete；guard 识别终态
 11. busy_map 残留（step-finish=stop 但 sid 仍在忙闲表）→ 双确认 done
 12. 忙闲表单轮瞬态掉表不得误判 abort，会话恢复后正常完成
 13. batch 升级即退 rc3（fallback_needed 带 --resolve argv）；
     guard 不重拉 escalated；--resolve retry/skip 断点续跑到 complete；
     --escalate-wait 旧语义升级常驻

直接运行：python smoke_test.py；任一断言失败退出码 1。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, "..", "scripts"))
PY = sys.executable
IS_WIN = sys.platform == "win32"

# ---------------------------------------------------------------------------
# policy / procutil 纯逻辑
# ---------------------------------------------------------------------------

sys.path.insert(0, SCRIPTS)
import policy as policy_mod  # noqa: E402
from procutil import pid_alive  # noqa: E402


def test_policy(wa=None):
    root = os.path.join(tempfile.gettempdir(), "tt-root")
    child = os.path.join(root, "work", "a")
    parent = os.path.dirname(root)
    pol = policy_mod.Policy([root], [], [child])
    decide = lambda pats, perm="external_directory": pol.decide(
        {"permission": perm, "patterns": pats})

    assert decide([os.path.join(root, "evidence", "x")]) == "session", \
        "工作区内非源码路径应自动放行"
    assert decide([child, os.path.join(child, "deep")]) == "session", \
        "allow 精确根及其子路径应放行"
    assert decide([parent]) == "broad", "祖先目录宽申应自动驳回纠偏"
    outside = r"C:\Windows\System32" if IS_WIN else "/etc"
    assert decide([outside]) is None, "系统/出界目录必须上交"
    assert decide([child], perm="other_permission") is None, \
        "非目录类授权必须上交"
    assert decide([r"\\tmp\*"]) == "session", "无效 UNC 视同安全"
    assert policy_mod.norm("relative/x") is None, "相对路径归一化应返回 None"
    broad_pol = policy_mod.Policy([root], [], [], strict=True)
    assert broad_pol.decide({"permission": "external_directory",
                             "patterns": [parent]}) is None, \
        "strict 模式宽申第一次即上交"
    print("[ok] policy 三级裁决")


def test_procutil(wa=None):
    assert pid_alive(os.getpid()) is True
    assert pid_alive(None) is False
    assert pid_alive("not-a-pid") is False
    assert pid_alive(0x7FFFFFF0) is False, "不存在的大 pid 应判死"
    print("[ok] procutil.pid_alive")


# ---------------------------------------------------------------------------
# 假 TeleAgent API
# ---------------------------------------------------------------------------

class FakeState:
    def __init__(self):
        self.lock = threading.Lock()
        self.mode = "done_after_2"
        self.sid_seq = 0
        self.busy = {}                 # sid -> poll 计数
        self.prompts = {}              # sid -> [text]
        self.permissions = []          # 待裁决授权
        self.reply_404_ids = set()
        self.replies = []
        self.aborted = set()
        self.reenqueue_broad = None
        self.perm_seq = 1

    def new_sid(self):
        self.sid_seq += 1
        sid = "sid-%d" % self.sid_seq
        self.busy[sid] = 0
        self.prompts[sid] = []
        return sid

    def tick(self, sid):
        n = self.busy.get(sid, 0)
        self.busy[sid] = n + 1
        return n

    def is_done_poll(self, sid):
        n = self.busy.get(sid, 0)
        if self.mode == "done_after_1":
            return n >= 1
        if self.mode in ("always_running", "stale_busy_done"):
            return False if self.mode == "always_running" else n >= 2
        if self.mode == "blip_then_done":
            return n >= 4
        return n >= 2

    def in_busy_status(self, sid):
        """状态忙闲表是否含 sid（在 GET /message tick 之前调用）。"""
        if sid in self.aborted:
            return False
        n = self.busy.get(sid, 0)
        if self.mode == "stale_busy_done":
            # 任务已 stop 但忙闲表永不清理：模拟 TeleAgent busy_map 残留
            return True
        if self.mode == "blip_then_done":
            # 第 2 轮瞬态掉表（消息还只是 assistant 文本），第 3 轮恢复忙，
            # 第 4 轮真正完成——双确认必须能挡住这次假 abort
            return n not in (2,) and n < 4
        return not self.is_done_poll(sid)

    def messages(self, sid):
        if sid in self.aborted:
            return [{"info": {"role": "assistant"}, "parts": [
                {"type": "text", "text": "connection lost mid task"},
                {"type": "step-finish", "reason": "aborted"}]}]
        if self.mode == "aborted" and sid == self._first_sid():
            return [{"info": {"role": "assistant"}, "parts": [
                {"type": "text", "text": "dial tcp :11434 connect refused"},
                {"type": "step-finish", "reason": "aborted"}]}]
        if self.mode == "model_error" and self.is_done_poll(sid):
            # 真实形态：parts 为空，错误只挂在 info.finish/info.error
            return [{"info": {
                        "role": "assistant", "finish": "error",
                        "modelID": "ghost", "providerID": "GhostCorp",
                        "error": {"name": "UnknownError", "data": {
                            "message": 'create chat model: provider "GhostCorp" not found'}}},
                     "parts": []}]
        if self.mode == "blip_then_done":
            n = self.busy.get(sid, 0)  # 本轮 GET /message 已 tick
            if n == 3:
                return [{"info": {"role": "assistant"}, "parts": [
                    {"type": "text", "text": "halfway text, no finish yet"}]}]
            if n >= 5:
                return [{"info": {"role": "assistant"}, "parts": [
                    {"type": "text", "text": "TASK_ANSWER_MARKER 全部完成"},
                    {"type": "step-finish", "reason": "stop"}]}]
            return [{"info": {"role": "assistant"}, "parts": [
                {"type": "tool", "state": {"status": "running"}},
                {"type": "text", "text": "working..."}]}]
        if self.is_done_poll(sid):
            return [{"info": {"role": "assistant"}, "parts": [
                {"type": "text", "text": "TASK_ANSWER_MARKER 全部完成"},
                {"type": "step-finish", "reason": "stop"}]}]
        return [{"info": {"role": "assistant"}, "parts": [
            {"type": "tool", "state": {"status": "running"}},
            {"type": "text", "text": "working..."}]}]

    def _first_sid(self):
        return "sid-1"


STATE = FakeState()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        p = self.path
        with STATE.lock:
            if p == "/config":
                return self._send(200, {"enabled_providers": ["FakeCorp", "GhostCorp"],
                                        "small_model": "FakeCorp/lite"})
            if p == "/config/providers":
                return self._send(200, {
                    "default": {"FakeCorp": "coder", "GhostCorp": "ghost"},
                    "providers": [
                        {"id": "FakeCorp", "source": "config",
                         "options": {"apiKey": "test-key"},
                         "models": {"coder": {"capabilities": {"toolcall": True}}}},
                        {"id": "GhostCorp", "source": "custom",
                         "env": ["GHOST_API_KEY"],
                         "models": {"ghost": {"capabilities": {"toolcall": True}}}}]})
            if p == "/session/status":
                # 忙闲表是否残留由 FakeState.in_busy_status 决定
                # （覆盖正常完成、busy_map 残留、瞬态掉表等场景）
                return self._send(200, {
                    s: {} for s in STATE.busy if STATE.in_busy_status(s)})
            if p == "/permission":
                return self._send(200, list(STATE.permissions))
            if p == "/session":
                return self._send(200, [{"id": s, "title": t[0][:40]}
                                        for s, t in list(STATE.prompts.items())])
            if p.startswith("/session/") and p.endswith("/message"):
                sid = p.split("/")[2]
                STATE.tick(sid)
                return self._send(200, STATE.messages(sid))
        self._send(404, {"error": "not found"})

    def do_POST(self):
        p = self.path
        body = self._read_body()
        with STATE.lock:
            if p == "/session":
                sid = STATE.new_sid()
                return self._send(200, {"id": sid})
            if p.startswith("/session/") and p.endswith("/prompt_async"):
                sid = p.split("/")[2]
                text = ""
                for part in body.get("parts") or []:
                    text += part.get("text") or ""
                STATE.prompts.setdefault(sid, []).append(text)
                return self._send(200, {})
            if p.startswith("/session/") and p.endswith("/abort"):
                sid = p.split("/")[2]
                STATE.aborted.add(sid)
                STATE.busy.pop(sid, None)
                return self._send(200, {})
            if p.startswith("/permission/") and p.endswith("/reply"):
                pid = p.split("/")[2]
                STATE.replies.append((pid, body.get("reply")))
                if pid in STATE.reply_404_ids:
                    STATE.permissions = [r for r in STATE.permissions
                                         if r.get("id") != pid]
                    return self._send(404, {"error": "no pending permission"})
                STATE.permissions = [r for r in STATE.permissions
                                     if r.get("id") != pid]
                # 模拟真实智能体被 reject 后按同样 patterns 重新申请（新 id）
                if body.get("reply") == "reject" and STATE.reenqueue_broad:
                    STATE.perm_seq += 1
                    STATE.permissions.append({
                        "id": "perm-%d" % STATE.perm_seq,
                        "sessionID": "sid-1",
                        "permission": "external_directory",
                        "patterns": list(STATE.reenqueue_broad)})
                return self._send(200, {})
        self._send(404, {"error": "not found"})


def serve():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


# ---------------------------------------------------------------------------
# CLI 子进程
# ---------------------------------------------------------------------------

class WorkArea:
    def __init__(self, port):
        # 工作区刻意放在用户主目录下的唯一临时目录：系统临时目录属于工具链
        # 白名单根，放那里会让"宽申/出界授权"场景失真
        self.root = os.path.join(os.path.expanduser("~"),
                                 ".teleagent-teammate-smoke-%d-%d" % (
                                     os.getpid(), int(time.time())))
        self.data = os.path.join(tempfile.gettempdir(),
                                 "tt-smoke-data-%d" % os.getpid())
        self.ws = os.path.join(self.root, "project")
        os.makedirs(self.ws)
        self.env = dict(os.environ)
        self.env.update({
            "SUPER_AGENT_LOCAL_SESSION_KEY": "smoke-test-key",
            "TELEAGENT_BASE_URL": "http://127.0.0.1:%d" % port,
            "TELEAGENT_DATA_DIR": self.data,
            "TELEAGENT_TEAMMATE_CONFIG_DIR": os.path.join(self.root, "cfg"),
        })

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.data, ignore_errors=True)


def run_cli(wa, *args, timeout=90):
    p = subprocess.run([PY, os.path.join(SCRIPTS, "teammate.py"), *args],
                       env=wa.env, capture_output=True, text=True,
                       timeout=timeout, encoding="utf-8", errors="replace")
    lines = [json.loads(x) for x in p.stdout.splitlines()
             if x.strip().startswith("{")]
    return p.returncode, lines, p.stdout, p.stderr


def last_event(lines, name=None):
    for ev in reversed(lines):
        if name is None or ev.get("event") == name:
            return ev
    return None


def reset_state(mode):
    with STATE.lock:
        STATE.__init__()
        STATE.mode = mode


# ---------------------------------------------------------------------------
# 场景
# ---------------------------------------------------------------------------

def test_doctor(wa):
    rc, lines, out, err = run_cli(wa, "doctor", "--no-autostart")
    assert rc == 0, "doctor 应通过：\n%s\n%s" % (out, err)
    assert last_event(lines).get("ok") is True
    rc, lines, out, err = run_cli(wa, "models")
    assert rc == 0, out + err
    pids = {c["provider"] for c in last_event(lines).get("candidates", [])}
    assert pids == {"FakeCorp"}, "未配置凭据的 GhostCorp 不得入选：%s" % pids
    print("[ok] doctor 假服务通过；无凭据供应商已过滤")


def test_run_done(wa):
    reset_state("done_after_2")
    pf = wa.path("task1.md")
    outf = wa.path("result1.md")
    with open(pf, "w", encoding="utf-8") as f:
        f.write("# 任务一\n完成 TASK_ANSWER_MARKER。")
    rc, lines, out, err = run_cli(
        wa, "run", "--title", "smoke-done", "--prompt-file", pf,
        "--directory", wa.ws, "--output-file", outf,
        "--timeout", "60", "--window", "6", "--interval", "1",
        "--workspace", wa.ws, "--allow", wa.ws)
    assert rc == 0, "正常任务应 rc=0：\n%s\n%s" % (out, err)
    assert last_event(lines, "done")
    assert "TASK_ANSWER_MARKER" in open(outf, encoding="utf-8").read()
    assert not any(ev.get("event") == "fallback_needed" for ev in lines)
    print("[ok] run 正常完成（无 fallback）")


def test_run_aborted(wa):
    reset_state("aborted")
    pf = wa.path("task2.md")
    with open(pf, "w", encoding="utf-8") as f:
        f.write("ORIGINAL_TASK_BODY 二号任务")
    rc, lines, out, err = run_cli(
        wa, "run", "--title", "smoke-abort", "--prompt-file", pf,
        "--directory", wa.ws, "--timeout", "60", "--window", "4",
        "--interval", "1", "--workspace", wa.ws)
    assert rc == 3, "abort 应 rc=3：\n%s\n%s" % (out, err)
    fb = last_event(lines, "fallback_needed")
    assert fb and fb["reason"] in ("session_aborted", "model_fault"), fb
    assert fb["recommended_actions"], "必须带等价处置 argv"
    assert any("resume" in " ".join(a["argv"]) for a in fb["recommended_actions"])
    assert fb["context"]["breakpoint"] is not None
    assert "不得要求用户" in fb["last_resort"]
    print("[ok] run abort -> fallback_needed(session_aborted) 带处置动作")


def test_run_model_fault(wa):
    reset_state("model_error")
    pf = wa.path("task2b.md")
    with open(pf, "w", encoding="utf-8") as f:
        f.write("二号乙任务")
    rc, lines, out, err = run_cli(
        wa, "run", "--title", "smoke-model-fault", "--prompt-file", pf,
        "--directory", wa.ws, "--timeout", "60", "--window", "6",
        "--interval", "1", "--workspace", wa.ws)
    assert rc == 3, "模型故障应 rc=3：\n%s\n%s" % (out, err)
    fb = last_event(lines, "fallback_needed")
    assert fb and fb["reason"] == "model_fault", fb
    argv0 = " ".join(fb["recommended_actions"][0]["argv"])
    assert "--next-model" in argv0, argv0
    print("[ok] run info.finish=error(provider not found) -> fallback_needed(model_fault)")


def test_run_stalled(wa):
    reset_state("always_running")
    pf = wa.path("task3.md")
    with open(pf, "w", encoding="utf-8") as f:
        f.write("三号任务")
    rc, lines, out, err = run_cli(
        wa, "run", "--title", "smoke-stall", "--prompt-file", pf,
        "--directory", wa.ws, "--timeout", "60", "--window", "3",
        "--interval", "1", "--stall-seconds", "4",
        "--workspace", wa.ws)
    assert rc == 6, "停滞应 rc=6：\n%s\n%s" % (out, err)
    fb = last_event(lines, "fallback_needed")
    assert fb and fb["reason"] == "stalled"
    assert any(a["argv"][1:3] == ["prompt"] or "prompt" in a["argv"]
               for a in fb["recommended_actions"])
    print("[ok] run 停滞 -> fallback_needed(stalled)")


def _set_permission(patterns):
    STATE.permissions = [{
        "id": "perm-1", "sessionID": "sid-1",
        "permission": "external_directory", "patterns": patterns}]


def test_run_permission_manual(wa):
    reset_state("always_running")
    outside = r"C:\Windows\System32\drivers" if IS_WIN else "/etc"
    _set_permission([outside])
    pf = wa.path("task4.md")
    with open(pf, "w", encoding="utf-8") as f:
        f.write("四号任务")
    rc, lines, out, err = run_cli(
        wa, "run", "--title", "smoke-perm", "--prompt-file", pf,
        "--directory", wa.ws, "--timeout", "30", "--window", "3",
        "--interval", "1", "--workspace", wa.ws)
    assert rc == 4, "出界授权应 rc=4：\n%s\n%s" % (out, err)
    fb = last_event(lines, "fallback_needed")
    assert fb and fb["reason"] == "permission_required"
    assert any("permission" in " ".join(a["argv"]) and "reply" in " ".join(a["argv"])
               for a in fb["recommended_actions"])
    assert STATE.replies == [], "出界授权绝不能被自动回复"
    # 调用方智能体自行裁决后用 CLI 放行（覆盖 --id dest 回归）
    rc2, lines2, out2, err2 = run_cli(
        wa, "permission", "reply", "--id", "perm-1", "--reply", "session")
    assert rc2 == 0, out2 + err2
    assert last_event(lines2).get("event") == "permission_replied"
    assert ("perm-1", "session") in STATE.replies
    print("[ok] run 出界授权 -> fallback_needed(permission_required)，未越权回复；CLI reply 可用")


def test_run_permission_broad(wa):
    reset_state("always_running")
    broad = os.path.dirname(wa.ws.rstrip("\\/"))
    _set_permission([broad])
    STATE.reenqueue_broad = [broad]
    pf = wa.path("task5.md")
    with open(pf, "w", encoding="utf-8") as f:
        f.write("五号任务")
    rc, lines, out, err = run_cli(
        wa, "run", "--title", "smoke-broad", "--prompt-file", pf,
        "--directory", wa.ws, "--timeout", "30", "--window", "3",
        "--interval", "1", "--workspace", wa.ws)
    assert rc == 4, "宽申第 2 次应 rc=4：\n%s\n%s" % (out, err)
    rejects = [r for r in STATE.replies if r[1] == "reject"]
    assert len(rejects) == 1, "首次宽申应恰好自动驳回 1 次：%s" % STATE.replies
    assert any("自动纠偏" in p for texts in STATE.prompts.values()
               for p in texts), "应注入纠偏指令"
    assert last_event(lines, "PERMISSION_MANUAL")
    print("[ok] 宽申首次驳回+纠偏，第 2 次上交")


def test_run_permission_404(wa):
    reset_state("done_after_1")
    _set_permission([os.path.join(wa.ws, "inside")])
    STATE.reply_404_ids.add("perm-1")
    pf = wa.path("task6.md")
    outf = wa.path("result6.md")
    with open(pf, "w", encoding="utf-8") as f:
        f.write("六号任务")
    rc, lines, out, err = run_cli(
        wa, "run", "--title", "smoke-404", "--prompt-file", pf,
        "--directory", wa.ws, "--output-file", outf,
        "--timeout", "60", "--window", "4", "--interval", "1",
        "--workspace", wa.ws)
    assert rc == 0, "404 已消费授权不应阻塞任务：\n%s\n%s" % (out, err)
    auto = last_event(lines, "PERMISSION_AUTO")
    assert auto and auto["status"] == "gone:404"
    print("[ok] reply 404 视为已消费，任务正常完成")


def test_resume(wa):
    reset_state("aborted")
    pf = wa.path("task7.md")
    with open(pf, "w", encoding="utf-8") as f:
        f.write("RESUME_BODY_MARKER 七号原任务书")
    rc, lines, out, err = run_cli(
        wa, "dispatch", "--title", "smoke-old", "--prompt-file", pf,
        "--directory", wa.ws)
    assert rc == 0, (out, err)
    # 此后新会话改为快速完成
    with STATE.lock:
        STATE.mode = "done_after_1"
    rc, lines, out, err = run_cli(
        wa, "resume", "--session", "sid-1", "--abort-old",
        "--timeout", "60", "--window", "4", "--interval", "1",
        "--workspace", wa.ws)
    assert rc == 0, "续派新会话应完成 rc=0：\n%s\n%s" % (out, err)
    ev = last_event(lines, "resumed")
    assert ev and ev["session_id"] == "sid-2"
    assert "old_aborted" in [e.get("event") for e in lines]
    new_prompt = "".join(STATE.prompts.get("sid-2", []))
    assert "RESUME_BODY_MARKER" in new_prompt, "接续任务书必须含原任务书"
    assert "严禁重做已完成部分" in new_prompt
    print("[ok] resume 带断点+原任务书续派成功")


def test_batch_and_guard(wa):
    reset_state("done_after_1")
    pf = wa.path("task8.md")
    outf = wa.path("result8.md")
    with open(pf, "w", encoding="utf-8") as f:
        f.write("八号批次任务，完成后写结论 TASK_ANSWER_MARKER")
    # 假服务的完成答案不含文件产物，故 dispatch_group 不做 files_exist
    pb = {
        "batch": "smoke-batch", "window": 4,
        "roots": {"workspace": [wa.ws], "allow": [wa.ws]},
        "stages": [
            {"id": "g", "type": "dispatch_group", "timeout": 120, "agents": [
                {"key": "a", "title": "batch-agent", "directory": wa.ws,
                 "prompt_file": pf, "output_file": outf}]},
            {"id": "sh", "type": "shell", "steps": [
                {"argv": [PY, "-c", "print('BATCH_SHELL_MARKER')"],
                 "stdout_contains": ["BATCH_SHELL_MARKER"]}]},
            {"id": "note", "type": "notify",
             "title": "smoke", "message": "batch done"}],
    }
    pb_path = wa.path("playbook.json")
    sdir = wa.path("orch")
    with open(pb_path, "w", encoding="utf-8") as f:
        json.dump(pb, f, ensure_ascii=False)
    p = subprocess.run([PY, os.path.join(SCRIPTS, "batch.py"),
                        "--playbook", pb_path, "--state-dir", sdir],
                       env=wa.env, capture_output=True, text=True,
                       timeout=120, encoding="utf-8", errors="replace")
    assert p.returncode == 0, "batch 应跑到 complete：\n%s\n%s" % (
        p.stdout, p.stderr)
    state = json.load(open(os.path.join(sdir, "state.json"), encoding="utf-8"))
    assert state["status"] == "complete", state
    assert all(v["state"] == "done" for v in state["stages"].values())
    assert os.path.isfile(os.path.join(sdir, "launch.json")), \
        "launch.json 必须落盘供守卫无损重拉"

    # guard 对终态批次应 rc=0 terminal
    g = subprocess.run([PY, os.path.join(SCRIPTS, "guard.py"),
                        "--state-dir", sdir], env=wa.env,
                       capture_output=True, text=True)
    assert g.returncode == 0, g.stdout
    assert json.loads(g.stdout).get("terminal") == "complete"

    # batch_ctl status：进程已退出，rc=2 但 JSON 可读
    c = subprocess.run([PY, os.path.join(SCRIPTS, "batch_ctl.py"),
                        "status", "--state-dir", sdir], env=wa.env,
                       capture_output=True, text=True)
    st = json.loads(c.stdout)
    assert st["status"] == "complete" and st["alive"] is False
    print("[ok] batch dispatch_group+shell+notify complete，guard/ctl 正常")


def _batch_playbook(wa, marker, outf, tag):
    pf = wa.path("task-%s.md" % tag)
    with open(pf, "w", encoding="utf-8") as f:
        f.write("批次任务，完成后写结论 TASK_ANSWER_MARKER")
    return {
        "batch": "smoke-batch-%s" % tag, "window": 4,
        "roots": {"workspace": [wa.ws], "allow": [wa.ws]},
        "stages": [
            {"id": "g", "type": "dispatch_group", "timeout": 120, "agents": [
                {"key": "a", "title": "batch-agent-%s" % tag,
                 "directory": wa.ws, "prompt_file": pf,
                 "output_file": outf,
                 "verify": {"files_exist": [marker]}}]},
            {"id": "sh", "type": "shell", "steps": [
                {"argv": [PY, "-c", "print('BATCH_SHELL_MARKER')"],
                 "stdout_contains": ["BATCH_SHELL_MARKER"]}]},
            {"id": "note", "type": "notify",
             "title": "smoke", "message": "batch done"}],
    }


def run_batch(wa, pb_path, sdir, *extra, timeout=90):
    p = subprocess.run([PY, os.path.join(SCRIPTS, "batch.py"),
                        "--playbook", pb_path, "--state-dir", sdir, *extra],
                       env=wa.env, capture_output=True, text=True,
                       timeout=timeout, encoding="utf-8", errors="replace")
    lines = [json.loads(x) for x in p.stdout.splitlines()
             if x.strip().startswith("{")]
    return p.returncode, lines, p.stdout, p.stderr


def _guard_once(wa, sdir):
    g = subprocess.run([PY, os.path.join(SCRIPTS, "guard.py"),
                        "--state-dir", sdir], env=wa.env,
                       capture_output=True, text=True)
    return g.returncode, json.loads(g.stdout)


def _wait_state(sdir, status, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        try:
            st = json.load(open(os.path.join(sdir, "state.json"),
                                encoding="utf-8"))
            if st.get("status") == status:
                return st
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise AssertionError("state-dir 未在 %ds 内进入 %s" % (timeout, status))


def test_run_stale_busy_done(wa):
    reset_state("stale_busy_done")
    pf = wa.path("task-stale.md")
    with open(pf, "w", encoding="utf-8") as f:
        f.write("忙闲表残留任务")
    rc, lines, out, err = run_cli(
        wa, "run", "--title", "smoke-stale-busy", "--prompt-file", pf,
        "--directory", wa.ws, "--timeout", "60", "--window", "8",
        "--interval", "1", "--workspace", wa.ws, "--allow", wa.ws)
    assert rc == 0, "busy_map 残留但已 stop 必须按 done 收口：\n%s\n%s" % (
        out, err)
    ev = last_event(lines, "done")
    assert ev and ev.get("busy_map_lag") is True, ev
    print("[ok] busy_map 残留（stop 仍 busy）双确认后 rc0，不再干等")


def test_run_abort_blip(wa):
    reset_state("blip_then_done")
    pf = wa.path("task-blip.md")
    with open(pf, "w", encoding="utf-8") as f:
        f.write("瞬态掉表任务")
    rc, lines, out, err = run_cli(
        wa, "run", "--title", "smoke-blip", "--prompt-file", pf,
        "--directory", wa.ws, "--timeout", "60", "--window", "8",
        "--interval", "1", "--workspace", wa.ws, "--allow", wa.ws)
    assert rc == 0, "单轮瞬态掉表不得误判 abort：\n%s\n%s" % (out, err)
    assert last_event(lines, "done"), "会话恢复后应正常完成"
    assert not any(ev.get("event") in ("aborted", "error") for ev in lines)
    print("[ok] 忙闲表单轮瞬态掉表被双确认挡住，恢复后正常完成")


def test_batch_escalate_exit_resolve(wa):
    reset_state("done_after_1")
    marker = wa.path("marker-exit.txt")
    outf = wa.path("result-exit.md")
    pb = _batch_playbook(wa, marker, outf, "exit")
    pb_path = wa.path("playbook-exit.json")
    sdir = wa.path("orch-exit")
    with open(pb_path, "w", encoding="utf-8") as f:
        json.dump(pb, f, ensure_ascii=False)

    # 缺产物 -> 验证不过 -> 默认 escalate-exit：进程必须退出 rc3 唤醒调用方。
    # 时效量的是 ESCALATED -> BATCH_ESCALATED_EXIT 的间隔（进程总时长含
    # 收割窗节奏，不能用作判据；旧代码这里会白睡 15+3s）。
    rc, lines, out, err = run_batch(wa, pb_path, sdir)
    assert rc == 3, "默认升级即退 rc3：\n%s\n%s" % (out, err)
    esc = next((e for e in lines if e.get("event") == "fallback_needed"
                and not e.get("escalated_exit")), None)
    ex = next((e for e in lines if e.get("escalated_exit")), None)
    assert esc and ex, "缺升级/退出事件：%s" % [e.get("event") for e in lines]

    def _ts(e):
        return time.mktime(time.strptime(e["ts"], "%Y-%m-%d %H:%M:%S"))

    gap = _ts(ex) - _ts(esc)
    assert gap <= 1, "升级后必须同秒退出，不得再睡 15+3s，实际 %ss" % gap
    fb = lines[-1]
    assert fb.get("event") == "fallback_needed" and fb.get("escalated_exit")
    assert "收割验证未过" in fb.get("reason", "")
    assert all("--resolve" in " ".join(a["argv"])
               for a in fb["recommended_actions"]), fb
    state = json.load(open(os.path.join(sdir, "state.json"), encoding="utf-8"))
    assert state["status"] == "escalated"

    # 守卫对升级态（进程已死）只报告、绝不重拉
    grc, ginfo = _guard_once(wa, sdir)
    assert grc == 0 and ginfo.get("awaiting_caller") is True, ginfo
    assert "restarted" not in ginfo, "升级态禁止重拉：%s" % ginfo

    # 调用方场外补齐产物，执行 retry argv 续跑 -> complete
    with open(marker, "w", encoding="utf-8") as f:
        f.write("fixed")
    rc, lines, out, err = run_batch(wa, pb_path, sdir, "--resolve", "retry")
    assert rc == 0, "--resolve retry 应续跑到 complete：\n%s\n%s" % (
        out, err)
    state = json.load(open(os.path.join(sdir, "state.json"), encoding="utf-8"))
    assert state["status"] == "complete", state
    launch = json.load(open(os.path.join(sdir, "launch.json"),
                            encoding="utf-8"))
    assert "--resolve" not in launch["argv"], "一次性裁决不得进 launch.json"
    assert "--escalate-wait" not in launch["argv"]
    assert any(ev.get("event") == "RESOLVED" for ev in lines)
    print("[ok] batch 验证失败升级即退 rc3；guard 不重拉；--resolve retry 续跑 complete")


def test_batch_escalate_wait_and_skip(wa):
    reset_state("done_after_1")
    marker = wa.path("marker-wait.txt")
    outf = wa.path("result-wait.md")
    pb = _batch_playbook(wa, marker, outf, "wait")
    pb_path = wa.path("playbook-wait.json")
    sdir = wa.path("orch-wait")
    with open(pb_path, "w", encoding="utf-8") as f:
        json.dump(pb, f, ensure_ascii=False)

    # --escalate-wait：升级后进程必须常驻（旧语义，配合弹窗/守卫）
    proc = subprocess.Popen(
        [PY, os.path.join(SCRIPTS, "batch.py"), "--playbook", pb_path,
         "--state-dir", sdir, "--escalate-wait"],
        env=wa.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace")
    try:
        _wait_state(sdir, "escalated")
        time.sleep(2)
        assert proc.poll() is None, "escalate-wait 模式升级后不得退出"
        launch = json.load(open(os.path.join(sdir, "launch.json"),
                                encoding="utf-8"))
        assert "--escalate-wait" in launch["argv"], launch
    finally:
        proc.kill()
        proc.wait(timeout=20)

    # 即使进程已死，guard 对 escalated 仍然只报告不重拉
    grc, ginfo = _guard_once(wa, sdir)
    assert grc == 0 and ginfo.get("awaiting_caller") is True, ginfo
    assert "restarted" not in ginfo

    # 调用方决定跳过该阶段：--resolve skip 续跑 -> complete
    rc, lines, out, err = run_batch(wa, pb_path, sdir, "--resolve", "skip")
    assert rc == 0, "--resolve skip 应续跑到 complete：\n%s\n%s" % (
        out, err)
    state = json.load(open(os.path.join(sdir, "state.json"), encoding="utf-8"))
    assert state["status"] == "complete"
    assert state["stages"]["g"]["state"] == "skipped"
    print("[ok] --escalate-wait 升级常驻；死进程不被守卫重拉；--resolve skip 续跑 complete")


def main():
    httpd = serve()
    port = httpd.server_address[1]
    wa = WorkArea(port)
    cases = [
        test_policy, test_procutil, test_doctor, test_run_done,
        test_run_aborted, test_run_model_fault, test_run_stalled, test_run_permission_manual,
        test_run_permission_broad, test_run_permission_404,
        test_resume, test_batch_and_guard,
        test_run_stale_busy_done, test_run_abort_blip,
        test_batch_escalate_exit_resolve, test_batch_escalate_wait_and_skip,
    ]
    failed = 0
    for fn in cases:
        try:
            fn(wa)
        except Exception as e:
            failed += 1
            print("[FAIL] %s: %r" % (fn.__name__, e))
    httpd.shutdown()
    wa.cleanup()
    if failed:
        print("%d 个场景失败" % failed)
        sys.exit(1)
    print("全部 %d 个冒烟场景通过" % len(cases))


PORT = 0
if __name__ == "__main__":
    main()
