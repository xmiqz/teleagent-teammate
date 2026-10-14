#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""batch_ctl.py — teleagent-teammate 批次编排器的场外裁决入口。

调用方智能体（负总责者）通过它查询批次状态、对 ESCALATED 批次下达指令，
全程无需人工触碰 TeleAgent 界面。指令以追加行写入 commands.jsonl，
由常驻 batch.py 主循环在 15 秒内消费。

用法：
  python batch_ctl.py status --state-dir <dir>
  python batch_ctl.py wait   --state-dir <dir> [--timeout 420] [--interval 5]
  python batch_ctl.py retry  --state-dir <dir> [--note "..."]
  python batch_ctl.py skip   --state-dir <dir> [--note "..."]
  python batch_ctl.py abort  --state-dir <dir> [--note "..."]

wait 语义（反干等核心入口，必须在调用方当前轮次内前台阻塞执行）：
  有界轮询 state.json，单次调用至多阻塞 --timeout 秒；状态变化各输出一行
  JSON，终态输出末行后退出：
    complete/aborted -> rc0（末行 wait_terminal）
    escalated        -> rc3（末行带 escalation.reason/recommended_actions，
                            调用方在同一轮立即执行 --resolve argv 并再次 wait）
    到点仍 running   -> rc2（调用方在同一轮立即再发一次 wait，把轮次吊住，
                            绝不依赖后台任务完成通知——它不会开启新轮次）

指令语义：
  retry  当前升级阶段回到 pending（仅重试验证/等待，不重复派单）；
         若需换会话重派，调用方先自行用 teammate.py resume/abort 场外处置，
         再下达 retry。
  skip   跳过当前升级阶段（后续阶段照常执行）。
  abort  终止整个批次（状态置 aborted，batch 主循环自退，守卫随之自退）。

退出码：0 已投递/状态正常；2 批次进程不存活；3 状态文件缺失/损坏。
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from procutil import pid_alive  # noqa: E402


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def read_json(path):
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def cmd_status(sdir):
    state = read_json(os.path.join(sdir, "state.json"))
    if state is None:
        print(json.dumps({"event": "error",
                          "detail": f"state.json 不存在：{sdir}"},
                         ensure_ascii=False))
        return 3
    hb = read_json(os.path.join(sdir, "heartbeat.json")) or {}
    pid = (read_json(os.path.join(sdir, "batch.pid"))
           if os.path.exists(os.path.join(sdir, "batch.pid")) else None)
    if pid is None:
        try:
            with open(os.path.join(sdir, "batch.pid")) as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            pid = hb.get("pid")
    alive = pid_alive(pid)
    stages = [
        {"id": k, "state": v.get("state"),
         "started": v.get("started"), "finished": v.get("finished"),
         "fail": v.get("fail")}
        for k, v in (state.get("stages") or {}).items()]
    out = {
        "event": "batch_status",
        "batch": state.get("batch"),
        "status": state.get("status"),
        "pid": pid, "alive": alive,
        "heartbeat": hb,
        "stale": _stale_desc(hb),
        "stages": stages,
        "escalation": state.get("escalation"),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if alive else 2


def _stale_desc(hb):
    ts = hb.get("ts")
    if not ts:
        return None
    try:
        t = time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
        return int(time.time() - t)
    except (TypeError, ValueError):
        return None


def cmd_wait(sdir, timeout, interval):
    """有界前台阻塞等待批次终态。

    设计依据（真机实测）：Shell 后台任务完成通知不会开启调用方智能体的
    新一轮对话，进程退出后智能体不会凭空醒来。唯一可靠的收口方式是
    "当前轮次内的前台阻塞调用"：本函数保证至多阻塞 timeout 秒就把控制权
    交还给同一轮，rc2 时调用方立即再发一次 wait，形成同轮续窗循环。
    """
    deadline = time.time() + timeout
    last_status = None
    last_alive = None
    while True:
        state = read_json(os.path.join(sdir, "state.json"))
        if state is None:
            print(json.dumps({"event": "error",
                              "detail": f"state.json 不存在：{sdir}"},
                             ensure_ascii=False))
            return 3
        status = state.get("status")
        hb = read_json(os.path.join(sdir, "heartbeat.json")) or {}
        pid = hb.get("pid")
        alive = pid_alive(pid) if pid else False
        if status != last_status or alive != last_alive:
            print(json.dumps(
                {"event": "wait_status", "status": status, "pid": pid,
                 "alive": alive, "heartbeat_age": _stale_desc(hb)},
                ensure_ascii=False), flush=True)
            last_status, last_alive = status, alive

        if status in ("complete", "aborted"):
            print(json.dumps(
                {"event": "wait_terminal", "status": status, "rc": 0,
                 "state_dir": sdir}, ensure_ascii=False), flush=True)
            return 0
        if status == "escalated":
            esc = state.get("escalation") or {}
            print(json.dumps(
                {"event": "wait_terminal", "status": "escalated", "rc": 3,
                 "reason": esc.get("reason"), "stage": esc.get("stage"),
                 "detail": esc.get("detail"),
                 "recommended_actions": esc.get("recommended_actions"),
                 "state_dir": sdir,
                 "hint": "同一轮内执行 recommended_actions 的 --resolve "
                         "argv 后，立即再发一次 wait"},
                ensure_ascii=False), flush=True)
            return 3
        if time.time() >= deadline:
            print(json.dumps(
                {"event": "wait_timeout", "status": status, "rc": 2,
                 "state_dir": sdir,
                 "hint": "批次仍在跑：同一轮内立即再发一次 wait，"
                         "不要结束本轮、不要转后台等待唤醒"},
                ensure_ascii=False), flush=True)
            return 2
        time.sleep(interval)


def send_command(sdir, action, note):
    state = read_json(os.path.join(sdir, "state.json"))
    if state is None:
        print(json.dumps({"event": "error",
                          "detail": f"state.json 不存在：{sdir}"},
                         ensure_ascii=False))
        return 3
    pid = None
    ppath = os.path.join(sdir, "batch.pid")
    if os.path.exists(ppath):
        try:
            with open(ppath) as f:
                pid = int(f.read().strip())
        except ValueError:
            pass
    if not pid_alive(pid):
        print(json.dumps(
            {"event": "error", "detail": f"批次进程不存活 pid={pid}，"
             "指令无法被消费。escalated 状态请直接重拉并带裁决："
             "python batch.py --playbook <pb> --state-dir <dir> "
             "--resolve retry|skip|abort|redispatch:<key>（守卫不重拉升级态）"},
            ensure_ascii=False))
        return 2
    if action in ("retry", "skip") and state.get("status") != "escalated":
        print(json.dumps(
            {"event": "note", "detail":
             f"当前状态为 {state.get('status')}，{action} 仅在 escalated 时生效"},
            ensure_ascii=False))
    rec = {"ts": now(), "action": action, "note": note or ""}
    os.makedirs(sdir, exist_ok=True)
    with open(os.path.join(sdir, "commands.jsonl"), "a",
              encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(json.dumps({"event": "command_queued", "action": action,
                      "note": note or "", "pid": pid},
                     ensure_ascii=False))
    return 0


def main():
    ap = argparse.ArgumentParser(description="批次编排器裁决入口")
    sub = ap.add_subparsers(dest="action", required=True)
    for name in ("status", "retry", "skip", "abort"):
        p = sub.add_parser(name)
        p.add_argument("--state-dir", required=True)
        if name != "status":
            p.add_argument("--note", default="")
    pw = sub.add_parser("wait", help="有界前台阻塞等待批次终态（反干等入口）")
    pw.add_argument("--state-dir", required=True)
    pw.add_argument("--timeout", type=int, default=420)
    pw.add_argument("--interval", type=int, default=5)
    args = ap.parse_args()
    sdir = os.path.abspath(args.state_dir)
    if args.action == "status":
        sys.exit(cmd_status(sdir))
    if args.action == "wait":
        sys.exit(cmd_wait(sdir, args.timeout, max(1, args.interval)))
    sys.exit(send_command(sdir, args.action, args.note))


if __name__ == "__main__":
    main()
