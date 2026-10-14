#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""batch_ctl.py — teleagent-teammate 批次编排器的场外裁决入口。

调用方智能体（负总责者）通过它查询批次状态、对 ESCALATED 批次下达指令，
全程无需人工触碰 TeleAgent 界面。指令以追加行写入 commands.jsonl，
由常驻 batch.py 主循环在 15 秒内消费。

用法：
  python batch_ctl.py status --state-dir <dir>
  python batch_ctl.py retry  --state-dir <dir> [--note "..."]
  python batch_ctl.py skip   --state-dir <dir> [--note "..."]
  python batch_ctl.py abort  --state-dir <dir> [--note "..."]

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
             "指令无法被消费；请先重启 batch.py（守卫会按 launch.json 自动重拉）"},
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
    args = ap.parse_args()
    sdir = os.path.abspath(args.state_dir)
    if args.action == "status":
        sys.exit(cmd_status(sdir))
    sys.exit(send_command(sdir, args.action, args.note))


if __name__ == "__main__":
    main()
