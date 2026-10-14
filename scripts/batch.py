# -*- coding: utf-8 -*-
r"""batch.py — TeleAgent 分包批次无人值守编排器（playbook 状态机，v1）。

watch.py 解决"看守会话+自动授权"，但会话完成后如果上层智能体没有新的
对话轮次，收割/合线/回归/构建仍会断在原地。batch.py 把"下一步"固化成
playbook，独立于任何智能体进程持续运行（pythonw + 开机自启守卫）：

  dispatch_group  并行派 N 个 TeleAgent 会话，等完，按产物验证结论
  shell           顺序执行 argv 步骤，支持退出码/输出断言/超时/重试
  notify          批次通知（弹窗 + 可选通知命令），状态机收尾

每步状态落 state.json，崩溃/被杀从断点恢复（sid 持久化不重复派单；
shell 步骤必须幂等）。以下情况进入 ESCALATED（弹窗每 15 分钟重弹，
进程不退，继续处理授权，等 batch_ctl 下指令）：
  - 危险授权 / 宽申纠偏第 2 次
  - TeleAgent 会话两次 abort / 阶段超时
  - 收割结论缺产物 / shell 失败或验证不过 / merge 冲突

playbook 示例见 assets/playbook.example.json。红线：push/tag 不入 playbook。

用法：
  python batch.py --playbook pb.json --state-dir <dir> [--popup]
"""
import argparse
import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import teammate as tac  # noqa: E402
import watch as watch_mod  # noqa: E402
from procutil import pid_alive  # noqa: E402


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Batch:
    def __init__(self, args):
        with open(args.playbook, "r", encoding="utf-8-sig") as f:
            self.pb = json.load(f)
        self.playbook_path = os.path.abspath(args.playbook)
        self.sdir = os.path.abspath(args.state_dir)
        os.makedirs(os.path.join(self.sdir, "logs"), exist_ok=True)
        self.state_path = os.path.join(self.sdir, "state.json")
        self.cmd_path = os.path.join(self.sdir, "commands.jsonl")
        self.pid_path = os.path.join(self.sdir, "batch.pid")
        self.launch_path = os.path.join(self.sdir, "launch.json")
        self.hb_path = os.path.join(self.sdir, "heartbeat.json")
        self.log_path = os.path.join(self.sdir, "logs", "batch.jsonl")
        self.popup = bool(getattr(args, "popup", False))
        # 默认 escalate-exit：升级的同一次迭代内输出末行 fallback_needed
        # 后即退 rc3（零额外 sleep），供调用方同轮 batch_ctl wait 循环捕获；
        # 调用方处置后用 --resolve 重拉。
        # --escalate-wait 保留旧语义（常驻等 batch_ctl，配合 --popup/守卫）。
        self.escalate_wait = bool(getattr(args, "escalate_wait", False))
        self.boot_resolve_action = getattr(args, "resolve", None)
        self.python = sys.executable
        self.window = int(self.pb.get("window", 45))

        roots = self.pb.get("roots", {})
        wargs = types.SimpleNamespace(
            sessions=[], max=999999, window=self.window, interval=5,
            tag=self.pb.get("batch", "batch"),
            log=self.log_path, popup=self.popup,
            workspace=roots.get("workspace", []),
            source=roots.get("source", []),
            allow=roots.get("allow", []),
            strict=bool(roots.get("strict", False)))
        self.watch = watch_mod.Watcher(wargs, own_process_lifecycle=False)
        self.client = self.watch.client
        self.lock = threading.RLock()
        self._single_instance()
        self._write_launch(args)
        self.state = self._load_state()

    def _write_launch(self, args):
        """持久化启动参数：守卫/开机自启据此无损重拉（python 路径可能轮换）。"""
        launch = {
            "python": sys.executable,
            "script": os.path.join(HERE, "batch.py"),
            # --escalate-wait 必须随守卫重拉保留；--resolve 是一次性场外裁决，
            # 绝不能进 launch（否则每次重拉都把旧裁决再应用一遍）
            "argv": ["--playbook", os.path.abspath(args.playbook),
                     "--state-dir", os.path.abspath(args.state_dir)]
                    + (["--escalate-wait"] if self.escalate_wait else [])
                    + (["--popup"] if self.popup else []),
            "ts": now(),
        }
        with open(self.launch_path, "w", encoding="utf-8") as f:
            json.dump(launch, f, ensure_ascii=False, indent=2)

    # ---------------- 基础设施 ----------------

    def _single_instance(self):
        """Windows 命名互斥量硬单实例；其他平台退回 pidfile。"""
        if sys.platform == "win32":
            from ctypes import wintypes
            k32 = ctypes.windll.kernel32
            k32.CreateMutexW.restype = wintypes.HANDLE
            k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL,
                                         wintypes.LPCWSTR]
            k32.CreateMutexW.use_last_error = True
            name = f"Local\\TeleagentTeammateBatch_{os.path.basename(self.sdir)}"
            self._mutex = k32.CreateMutexW(None, False, name)
            if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
                print(f"another batch holds mutex {name}, exit")
                sys.exit(0)
        if os.path.exists(self.pid_path):
            try:
                old = int(open(self.pid_path).read().strip())
                if old != os.getpid() and not pid_alive(old):
                    print(f"stale pidfile pid={old}（互斥量空闲，接管）")
            except (OSError, ValueError):
                pass
        with open(self.pid_path, "w") as f:
            f.write(str(os.getpid()))

    def _load_state(self):
        valid = {s["id"] for s in self.pb["stages"]}
        if os.path.exists(self.state_path):
            with open(self.state_path, "r", encoding="utf-8") as f:
                st = json.load(f)
            st["stages"] = {
                k: v for k, v in st["stages"].items() if k in valid}
            return st
        st = {
            "batch": self.pb.get("batch"),
            "status": "running",
            "started": now(),
            "stages": {s["id"]: {"state": "pending"}
                       for s in self.pb["stages"]},
            "escalation": None,
        }
        self._save_state(st)
        return st

    def _save_state(self, st=None):
        st = st or self.state
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.state_path)

    def emit(self, name, **kw):
        # 形参不得叫 event：harvest/escalate 调用会传 event=... 关键字
        rec = {"ts": now(), "event": name, **kw}
        line = json.dumps(rec, ensure_ascii=False)
        print(line, flush=True)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def heartbeat(self, stage_id, phase):
        hb = {"ts": now(), "pid": os.getpid(),
              "batch": self.state["batch"], "stage": stage_id,
              "phase": phase, "status": self.state["status"]}
        with open(self.hb_path, "w", encoding="utf-8") as f:
            json.dump(hb, f, ensure_ascii=False)

    def alert(self, title, msg):
        self.watch.popup = self.popup
        self.watch.alert(title, msg)

    def _ctl_argv(self, action, note=""):
        argv = [sys.executable,
                os.path.join(HERE, "batch_ctl.py"),
                action, "--state-dir", os.path.abspath(self.sdir)]
        if note:
            argv += ["--note", note]
        return argv

    def _resume_argv(self, resolve):
        """场外处置后一条命令完成裁决+断点续跑（进程已退场景）。"""
        argv = [sys.executable, os.path.join(HERE, "batch.py"),
                "--playbook", self.playbook_path,
                "--state-dir", os.path.abspath(self.sdir)]
        if self.escalate_wait:
            argv.append("--escalate-wait")
        argv += ["--resolve", resolve]
        return argv

    def escalation_actions(self, key=None):
        if self.escalate_wait:
            # 旧语义：进程常驻，调用方经 commands.jsonl 通道裁决
            return [
                {"label": "重试该阶段（仅重试验证/等待，不重复派单）",
                 "argv": self._ctl_argv("retry")},
                {"label": "跳过该阶段", "argv": self._ctl_argv("skip")},
                {"label": "终止整个批次", "argv": self._ctl_argv("abort")},
                {"label": "自行场外处置（abort 会话+新会话）后再 retry",
                 "argv": self._ctl_argv("retry", "场外处理完成")},
            ]
        # 默认语义：本进程升级即退 rc3 唤醒调用方；下列 argv 完成裁决并续跑
        actions = [
            {"label": "场外处置后续跑（先自行修正，再执行本命令，"
                      "只重试验证/等待，不重复派单）",
             "argv": self._resume_argv("retry")},
            {"label": "跳过当前阶段，继续后续阶段",
             "argv": self._resume_argv("skip")},
            {"label": "终止整个批次",
             "argv": self._resume_argv("abort")},
        ]
        if key:
            actions.insert(0, {
                "label": "先 teammate.py abort 旧会话（sid 见 state.json），"
                         "再执行本命令：作废 sid 重新派单（保留故障模型规避）后续跑",
                "argv": self._resume_argv("redispatch:%s" % key)})
        return actions

    def escalate(self, stage_id, reason, detail="", key=None):
        with self.lock:
            if self.state.get("escalation"):
                return
            # 调用方智能体负总责：默认零弹窗，只留结构化事件与状态文件
            actions = self.escalation_actions(key)
            self.state["escalation"] = {
                "stage": stage_id, "reason": reason,
                "detail": str(detail)[:800], "ts": now(),
                "alert_last": now(), "recommended_actions": actions}
            self.state["status"] = "escalated"
            self._save_state()
            self.emit("ESCALATED", event="fallback_needed",
                      reason=reason, stage=stage_id,
                      detail=str(detail)[:400],
                      blocking=True, recommended_actions=actions,
                      state_dir=os.path.abspath(self.sdir))
            if self.popup:
                self.alert(f"批次待裁决：{reason}",
                           f"批次 {self.state['batch']} 阶段 {stage_id}\n"
                           f"{str(detail)[:500]}\n"
                           "用 batch_ctl.py retry/skip/abort 处置。")

    def apply_resolution(self, action):
        """统一裁决状态机：活进程（batch_ctl 指令）与进程已退（--resolve 重拉）
        共用，两条路径语义必须完全一致。"""
        key = None
        if action.startswith("redispatch:"):
            key = action.split(":", 1)[1]
            action = "redispatch"
        if action == "abort":
            self.state["status"] = "aborted"
            self.state["escalation"] = None
            self._save_state()
            return
        sid = (self.state.get("escalation") or {}).get("stage")
        valid = {s["id"] for s in self.pb["stages"]}
        if action == "redispatch":
            stage = next((s for s in self.pb["stages"]
                          if s["id"] == sid), None)
            keys = {a["key"] for a in (stage or {}).get("agents", [])}
            if sid not in valid or key not in keys:
                raise ValueError(
                    f"redispatch 目标非法：stage={sid} key={key}")
            stg = self.state["stages"].setdefault(sid, {"state": "pending"})
            # 作废旧 sid/模型记录，强制下一轮重新派单；avoid_ 保留以继续
            # 规避故障模型；redispatch 计数清零给新会话完整重试额度
            for p in (f"sid_{key}", f"model_{key}", f"sig_{key}",
                      f"sig_since_{key}", f"redispatch_{key}"):
                stg.pop(p, None)
            finished = stg.get("finished_agents")
            if isinstance(finished, list) and key in finished:
                finished.remove(key)
            stg["state"] = "pending"
            stg.pop("started", None)
            stg.pop("fail", None)
        elif sid in valid and action in ("retry", "skip"):
            stg = self.state["stages"].get(sid, {"state": "pending"})
            if action == "retry":
                stg["state"] = "pending"
                stg.pop("started", None)
                # 已派会话的 sid 保留：retry 只重试验证/等待，不重复派单；
                # 需要重派用 redispatch:<key>
                stg.pop("fail", None)
            else:
                stg["state"] = "skipped"
                stg["finished"] = now()
            self.state["stages"][sid] = stg
        self.state["escalation"] = None
        self.state["status"] = "running"
        self._save_state()

    def maybe_realert(self):
        """升级态每 15 分钟重弹，漏接一次弹窗不等于永久停工。"""
        esc = self.state.get("escalation")
        if not esc:
            return
        last = esc.get("alert_last", esc.get("ts"))
        try:
            gap = time.time() - time.mktime(
                time.strptime(last, "%Y-%m-%d %H:%M:%S"))
        except (TypeError, ValueError):
            return
        if gap >= 900 and self.popup:
            esc["alert_last"] = now()
            self._save_state()
            self.alert(f"批次仍待裁决（{int(gap // 60)}min）：{esc['reason']}",
                       f"批次 {self.state['batch']} 阶段 {esc['stage']}\n"
                       f"{esc.get('detail', '')[:500]}\n"
                       "用 batch_ctl.py retry/skip/abort 处置。")

    # ---------------- 指令通道 ----------------

    def drain_commands(self):
        if not os.path.exists(self.cmd_path):
            return
        with open(self.cmd_path, "r", encoding="utf-8") as f:
            raw = [ln.strip() for ln in f if ln.strip()]
        cmds, changed = [], False
        for line in raw:
            try:
                cmd = json.loads(line)
            except Exception:
                continue
            if cmd.get("consumed"):
                cmds.append(cmd)
                continue
            self._apply_command(cmd)
            cmd["consumed"] = now()
            cmds.append(cmd)
            changed = True
        if changed:
            tmp = self.cmd_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                for cmd in cmds:
                    f.write(json.dumps(cmd, ensure_ascii=False) + "\n")
            os.replace(tmp, self.cmd_path)

    def _apply_command(self, cmd):
        act = cmd.get("action")
        self.emit("COMMAND", action=act, note=cmd.get("note", ""))
        if act not in ("abort", "retry", "skip"):
            return
        if act != "abort" and not self.state.get("escalation"):
            return
        self.apply_resolution(act)

    # ---------------- 授权 ----------------

    def poll_perm(self):
        held = self.watch.poll_permissions()
        if held:
            self.escalate("__permission__", "出界授权/宽申纠偏无效",
                          json.dumps(held, ensure_ascii=False)[:600])

    # ---------------- TeleAgent 派发/收割（进程内） ----------------

    def dispatch_agent(self, agent, avoid_model=None):
        if avoid_model:
            # 供应商/模型故障转移：取自动链中不同于旧模型的下一位
            chain = tac.ordered_models(self.client, agent.get("model"))
            alt = [m for m in chain if f"{m[0]}/{m[1]}" != avoid_model]
            chosen = (alt or chain or
                      [tac.resolve_model(self.client, agent.get("model"))])[0]
            provider_id, model_id, _src = chosen
            source = ("failover" if not agent.get("model")
                      else "explicit_failover")
        else:
            provider_id, model_id, source = tac.resolve_model(
                self.client, agent.get("model"))
        with open(agent["prompt_file"], encoding="utf-8-sig") as f:
            prompt = f.read()
        directory = agent.get("directory")
        if directory and not os.path.isdir(directory):
            raise RuntimeError(f"agent 工作目录不存在：{directory}")
        title = agent["title"]
        sid = tac.create_session(self.client, title, directory)
        tac.send_prompt(self.client, sid, prompt, provider_id, model_id,
                        directory=directory, agent=agent.get("agent"))
        tac.record_dispatch(sid, title, f"{provider_id}/{model_id}",
                            agent.get("output_file"), directory)
        tac.save_task_prompt(sid, prompt)
        self.watch.sessions = list(dict.fromkeys(self.watch.sessions + [sid]))
        self.emit("DISPATCHED", key=agent["key"], sid=sid, title=title,
                  model=f"{provider_id}/{model_id}", model_source=source)
        return sid, f"{provider_id}/{model_id}"

    def harvest_window(self, sid, out_file, st=None, key=None,
                       stall_seconds=1800):
        """一个收割窗；返回 rc（0 完成 2 未完成 3 abort 1 硬错误 6 停滞）。"""
        old = self.watch.window
        self.watch.window = self.window
        try:
            rc, event = self.watch._window(sid, quiet=True)
        finally:
            self.watch.window = old
        self.emit("HARVEST", sid=sid, event=event.get("event"), rc=rc,
                  hints=event.get("error_hints"))
        # 停滞检测：忙但 (state,messages,tool_calls) 签名零增长超时
        if rc == 2 and st is not None and key is not None and stall_seconds:
            sig = (event.get("state"), event.get("messages"),
                   event.get("tool_calls"))
            now_ts = time.time()
            if st.get(f"sig_{key}") != list(sig):
                st[f"sig_{key}"] = list(sig)
                st[f"sig_since_{key}"] = now_ts
            elif now_ts - float(st.get(f"sig_since_{key}", now_ts)) >= stall_seconds:
                self.emit("STALLED", sid=sid, key=key,
                          stall_seconds=stall_seconds,
                          hint="调用方可用 teammate.py resume --abort-old 续派后 ctl retry")
                rc = 6
        if rc in (0, 3, 1) and out_file:
            try:
                tac.write_result_file(out_file, event)
                tac.update_registry_state(sid, event.get("event"))
            except OSError as e:
                self.emit("RESULT_WRITE_FAIL", sid=sid, error=str(e)[:200])
        return rc

    def run_dispatch_group(self, stage):
        st = self.state["stages"][stage["id"]]
        agents = stage["agents"]
        finished = st.setdefault("finished_agents", [])
        deadline = time.time() + int(stage.get("timeout", 5400))

        for a in agents:
            key = a["key"]
            if not st.get(f"sid_{key}"):
                avoid = st.pop(f"avoid_{key}", None)
                sid, model = self.dispatch_agent(a, avoid_model=avoid)
                st[f"sid_{key}"] = sid
                st[f"model_{key}"] = model
                st["state"] = "running"
                st["started"] = st.get("started", now())
                self._save_state()

        for a in agents:
            if a["key"] in finished:
                continue
            sid = st[f"sid_{a['key']}"]
            out = os.path.join(self.sdir, "logs",
                               f"watch_{stage['id']}_{a['key']}.md")
            stall = int(stage.get("stall_seconds",
                                  self.pb.get("stall_seconds", 1800)))
            rc = self.harvest_window(sid, out, st=st, key=a["key"],
                                     stall_seconds=stall)
            self._save_state()
            if rc == 0:
                finished.append(a["key"])
                self._save_state()
            elif rc == 6:
                self.escalate(stage["id"],
                              f"会话停滞：{a['key']}（{stall}s 零增长）", sid,
                              key=a["key"])
                return False
            elif rc in (3, 1):
                # abort 自动重派一次（连接抖动/秒死占多数），第二次才升级
                n = int(st.get(f"redispatch_{a['key']}", 0))
                if n < 1:
                    st[f"redispatch_{a['key']}"] = n + 1
                    st.pop(f"sid_{a['key']}", None)
                    # rc3 多为供应商/模型故障：换链上另一模型重派一次
                    st[f"avoid_{a['key']}"] = st.get(f"model_{a['key']}")
                    self._save_state()
                    self.emit("AUTO_REDISPATCH", key=a["key"], attempt=n + 1,
                              reason=f"{a['key']} 会话中止(rc={rc})，自动重派",
                              avoid_model=st[f"avoid_{a['key']}"])
                    return None
                self.escalate(stage["id"],
                              f"TeleAgent 会话两次中止：{a['key']}", sid,
                              key=a["key"])
                return False
        if time.time() > deadline and len(finished) < len(agents):
            self.escalate(stage["id"], "阶段超时",
                          f"finished {len(finished)}/{len(agents)}")
            return False
        if len(finished) < len(agents):
            return None  # 还在跑，下一轮继续

        ok, info = self.verify_agents(stage, agents, st)
        if not ok:
            self.escalate(stage["id"], "收割验证未过/缺产物", info)
            return False
        st["state"] = "done"
        st["finished"] = now()
        st["info"] = info
        self._save_state()
        self.emit("STAGE_DONE", stage=stage["id"], info=info)
        return True

    def verify_agents(self, stage, agents, st):
        msgs = []
        for a in agents:
            v = a.get("verify", {})
            for f in v.get("files_exist", []):
                if not os.path.isfile(f):
                    return False, f"{a['key']} 缺产物 {f}"
            for f, needle in (v.get("files_contain") or {}).items():
                if not os.path.isfile(f):
                    return False, f"{a['key']} 缺产物 {f}"
                txt = open(f, "r", encoding="utf-8", errors="replace").read()
                if needle not in txt:
                    tail = txt[-400:].replace("\n", " ")
                    return False, (
                        f"{a['key']} {os.path.basename(f)} 未找到「{needle}」"
                        f"；尾：{tail}")
            # files_match：正则容错（结论措辞/全半角冒号/加粗差异不应误杀）
            for f, pat in (v.get("files_match") or {}).items():
                if not os.path.isfile(f):
                    return False, f"{a['key']} 缺产物 {f}"
                txt = open(f, "r", encoding="utf-8", errors="replace").read()
                if not re.search(pat, txt):
                    tail = txt[-400:].replace("\n", " ")
                    return False, (
                        f"{a['key']} {os.path.basename(f)} 正则未中「{pat}」"
                        f"；尾：{tail}")
            msgs.append(f"{a['key']}:{st['sid_' + a['key']][:14]}")
        return True, ";".join(msgs)

    # ---------------- shell 阶段 ----------------

    def run_shell(self, stage):
        st = self.state["stages"][stage["id"]]
        st["state"] = "running"
        st["started"] = st.get("started", now())
        self._save_state()
        logf = os.path.join(self.sdir, "logs", f"shell_{stage['id']}.log")
        for i, step in enumerate(stage.get("steps", [])):
            argv = step["argv"]
            cwd = step.get("cwd")
            if cwd and not os.path.isdir(cwd):
                self.escalate(stage["id"], f"shell step{i} cwd 不存在", cwd)
                return False
            to = int(step.get("timeout", 1800))
            # retries 仅给环境 flaky 步骤；git merge/rebase 冲突必须人工，
            # playbook 评审时需把关（这里做关键字防御）
            attempts = int(step.get("retries", 0)) + 1
            joined = " ".join(str(x) for x in argv).lower()
            if "rebase" in joined or "merge" in joined:
                attempts = 1
            ok_round = False
            last_blob = ""
            for attempt in range(1, attempts + 1):
                self.emit("SHELL_START", stage=stage["id"], i=i,
                          attempt=attempt, argv=argv, cwd=cwd, timeout=to)
                try:
                    p = subprocess.run(
                        argv, cwd=cwd, timeout=to, capture_output=True,
                        text=True, encoding="utf-8", errors="replace")
                except subprocess.TimeoutExpired:
                    self.escalate(stage["id"],
                                  f"shell 超时 step{i} attempt{attempt}",
                                  str(argv))
                    return False
                blob = (p.stdout or "") + (p.stderr or "")
                last_blob = blob
                with open(logf, "a", encoding="utf-8") as f:
                    f.write(f"\n===== {now()} step{i} attempt{attempt} "
                            f"rc={p.returncode} =====\n")
                    f.write((p.stdout or "")[-8000:] + "\n"
                            + (p.stderr or "")[-4000:])
                if p.returncode != 0:
                    if attempt < attempts:
                        self.emit("SHELL_RETRY", stage=stage["id"], i=i,
                                  attempt=attempt, rc=p.returncode,
                                  tail=blob[-200:])
                        time.sleep(5)
                        continue
                    self.escalate(
                        stage["id"],
                        f"shell 失败 step{i} rc={p.returncode}"
                        f"（{attempts} 轮全挂）",
                        (p.stdout or "")[-300:] + (p.stderr or "")[-300:])
                    return False
                if step.get("stdout_empty") and blob.strip():
                    if attempt < attempts:
                        self.emit("SHELL_RETRY", stage=stage["id"], i=i,
                                  attempt=attempt, reason="stdout_not_empty",
                                  tail=blob[-200:])
                        time.sleep(5)
                        continue
                    self.escalate(stage["id"],
                                  f"shell step{i} 期望无输出（工作树不干净？）",
                                  blob[-400:])
                    return False
                miss = [n for n in step.get("stdout_contains", [])
                        if n not in blob]
                if miss:
                    if attempt < attempts:
                        self.emit("SHELL_RETRY", stage=stage["id"], i=i,
                                  attempt=attempt, reason=f"缺{miss}",
                                  tail=blob[-200:])
                        time.sleep(5)
                        continue
                    self.escalate(stage["id"],
                                  f"shell step{i} 输出缺「{miss}」",
                                  blob[-400:])
                    return False
                self.emit("SHELL_STEP_OK", stage=stage["id"], i=i,
                          attempt=attempt)
                ok_round = True
                break
            if not ok_round:
                self.escalate(stage["id"], f"shell step{i} 重试耗尽",
                              last_blob[-400:])
                return False
        verify = stage.get("verify", {})
        for f in verify.get("files_exist", []):
            if not os.path.isfile(f):
                self.escalate(stage["id"], f"验证缺产物 {f}", "")
                return False
        st["state"] = "done"
        st["finished"] = now()
        self._save_state()
        self.emit("STAGE_DONE", stage=stage["id"])
        return True

    def run_notify(self, stage):
        self.alert(stage.get("title", "批次阶段通知"),
                   stage.get("message", "") + f"\n批次 {self.state['batch']}")
        argv = stage.get("argv")
        if argv:
            try:
                subprocess.run(argv, cwd=stage.get("cwd"), timeout=120)
            except Exception as e:
                self.emit("NOTIFY_CMD_FAIL", error=str(e)[:300])
        self.emit("NOTIFY", title=stage.get("title"),
                  message=stage.get("message"))
        st = self.state["stages"][stage["id"]]
        st["state"] = "done"
        st["finished"] = now()
        self._save_state()
        self.emit("STAGE_DONE", stage=stage["id"])
        return True

    # ---------------- 主循环 ----------------

    def current_stage(self):
        for s in self.pb["stages"]:
            if self.state["stages"][s["id"]]["state"] in ("pending", "running"):
                return s
        return None

    def permission_thread(self):
        """授权 5s 常驻轮询：收割窗/build 阻塞期间也不能漏授权。"""
        while self.state["status"] not in ("aborted", "complete"):
            try:
                self.poll_perm()
            except Exception as e:
                self.emit("PERM_LOOP_ERROR", error=str(e)[:300])
            time.sleep(5)

    def boot_resolve(self):
        """进程已退场景的场外裁决：--resolve retry|skip|abort|redispatch:<key>
        在主循环开始前一次性应用，随后从断点继续。"""
        action = self.boot_resolve_action
        if not action:
            return
        if self.state.get("status") != "escalated":
            self.emit("RESOLVE_IGNORED", action=action,
                      current_status=self.state.get("status"),
                      note="只有 escalated 状态接受裁决")
            return
        try:
            self.apply_resolution(action)
        except Exception as e:
            self.emit("RESOLVE_BAD", action=action,
                      error=f"{type(e).__name__}: {e}"[:300])
            return
        self.emit("RESOLVED", action=action)

    def run(self):
        self.emit("BATCH_START", batch=self.state["batch"],
                  stages=[s["id"] for s in self.pb["stages"]],
                  python=self.python, playbook=self.playbook_path,
                  escalate_mode=("wait" if self.escalate_wait else "exit"))
        self.boot_resolve()
        pt = threading.Thread(target=self.permission_thread, daemon=True)
        pt.start()
        while True:
            self.drain_commands()
            if self.state["status"] == "aborted":
                self.emit("BATCH_ABORTED")
                return 4
            if self.state["status"] == "complete":
                return 0

            stage = self.current_stage()
            if stage is None:
                self.state["status"] = "complete"
                self.state["finished"] = now()
                self._save_state()
                self.emit("BATCH_COMPLETE")
                return 0

            self.heartbeat(stage["id"], self.state["status"])

            if self.state["status"] == "escalated":
                if self.escalate_wait:
                    self.maybe_realert()
                    time.sleep(15)
                    continue
                return self.exit_for_escalation()

            try:
                typ = stage["type"]
                if typ == "dispatch_group":
                    r = self.run_dispatch_group(stage)
                    if self._escalated_exit():
                        continue
                    if r is None:
                        time.sleep(10)
                    elif r is False:
                        time.sleep(15)
                elif typ == "shell":
                    if self.run_shell(stage) is False:
                        if self._escalated_exit():
                            continue
                        time.sleep(15)
                elif typ == "notify":
                    self.run_notify(stage)
                else:
                    self.escalate(stage["id"], f"未知阶段类型 {typ}", "")
                    if self._escalated_exit():
                        continue
                    time.sleep(15)
            except Exception as e:
                # TeleAgent 服务抖动/瞬时错误不得杀死进程触发重启循环：
                # 记日志、退避 60s，状态机原样下一轮重试（sid 已持久化）。
                # 例外：抖动同时已升级（如授权线程上交）-> 立即退出唤醒。
                self.emit("BATCH_ERROR", stage=stage["id"],
                          error=f"{type(e).__name__}: {e}"[:500])
                if self._escalated_exit():
                    continue
                time.sleep(60)
            time.sleep(3)

    def _escalated_exit(self):
        """exit 模式下升级已发生：本轮不得再睡，立即到循环顶部退出。"""
        return (not self.escalate_wait
                and self.state["status"] == "escalated")

    def exit_for_escalation(self):
        """默认 escalate-exit：末行 fallback_needed 后立即退出 rc3。进程退出
        是阻塞等待中调用方的唯一可靠唤醒信号（无弹窗、无用户）；调用方执行
        末行 argv 即完成裁决并从断点续跑。"""
        esc = self.state.get("escalation") or {}
        self.emit("BATCH_ESCALATED_EXIT", event="fallback_needed",
                  escalated_exit=True,
                  reason=esc.get("reason"), stage=esc.get("stage"),
                  detail=esc.get("detail"), blocking=True,
                  recommended_actions=esc.get(
                      "recommended_actions") or self.escalation_actions(),
                  state_dir=os.path.abspath(self.sdir),
                  hint="处置后执行 recommended_actions 的 argv 即续跑；"
                       "guard 不会重拉 escalated 状态")
        return 3


def main():
    ap = argparse.ArgumentParser(description="TeleAgent 分包批次编排器")
    ap.add_argument("--playbook", required=True)
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--popup", action="store_true",
                    help="启用响铃+弹窗；默认零打扰，异常只写事件与状态文件")
    ap.add_argument("--escalate-wait", action="store_true",
                    help="升级后进程常驻等 batch_ctl 裁决（旧语义，配合 "
                         "--popup/守卫）；默认升级即退 rc3 唤醒调用方")
    ap.add_argument("--resolve", default=None, metavar="ACTION",
                    help="一次性场外裁决（进程已退时）：retry|skip|abort|"
                         "redispatch:<agent_key>；应用后从断点续跑")
    args = ap.parse_args()
    sys.exit(Batch(args).run())


if __name__ == "__main__":
    main()
