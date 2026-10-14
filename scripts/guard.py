#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""guard.py — batch.py 常驻编排器的单轮巡检（看门狗的一次心跳）。

判定：
  1. state.json 为 complete/aborted      -> rc 0（终态，守卫应自退）
  2. state.json 为 escalated             -> rc 0 awaiting_caller（调用方负总责：
                                           进程升级即退是正常唤醒机制，绝不重拉，
                                           调用方 --resolve 重拉后自动恢复监控）
  3. 批次 pid 不存活且非终态/升级态       -> 读 launch.json 无损重拉，rc 10
  4. pid 存活但心跳陈旧（>heartbeat-min）-> rc 11（疑似僵尸/长阻塞，交 guard_loop 计数）
  5. 一切正常                             -> rc 0

重拉依据 launch.json（由 batch.py 启动时写入）：python 可执行文件可能随
TeleAgent 运行时轮换而变化，因此绝不硬编码 python 路径。
"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from procutil import pid_alive  # noqa: E402


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def spawn_batch(sdir, log):
    """按 launch.json 重新拉起 batch.py；失败抛异常。"""
    launch = read_json(os.path.join(sdir, "launch.json"))
    if not launch or not launch.get("python") or not launch.get("argv"):
        raise RuntimeError(f"launch.json 缺失或不完整：{sdir}")
    argv = [launch["python"], launch.get("script") or
            os.path.join(HERE, "batch.py")] + list(launch["argv"])
    kwargs = {"cwd": HERE, "stdout": log, "stderr": subprocess.STDOUT,
              "close_fds": True, "stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008)  # DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    p = subprocess.Popen(argv, **kwargs)
    return p.pid, argv


def heartbeat_age(hb):
    ts = (hb or {}).get("ts")
    if not ts:
        return None
    try:
        return time.time() - time.mktime(
            time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
    except (TypeError, ValueError):
        return None


def check_once(sdir, heartbeat_min=60, log_path=None):
    """执行一轮巡检，返回 (rc, info)。"""
    state = read_json(os.path.join(sdir, "state.json"))
    if state is None:
        return 3, {"detail": f"state.json 不可读：{sdir}"}
    status = state.get("status")
    if status in ("complete", "aborted"):
        return 0, {"terminal": status}
    if status == "escalated":
        # 默认 escalate-exit：批次进程此刻大概率已退出，这是等待调用方裁决
        # 的正常驻停态。重拉会与调用方的 --resolve 重拉撞互斥量/形成空转，
        # 因此只报告、不动进程；调用方续跑后心跳恢复，监控自动接续。
        esc = state.get("escalation") or {}
        return 0, {"awaiting_caller": True,
                   "stage": esc.get("stage"), "reason": esc.get("reason")}

    hb = read_json(os.path.join(sdir, "heartbeat.json")) or {}
    pid = hb.get("pid")
    if not pid:
        try:
            with open(os.path.join(sdir, "batch.pid")) as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            pid = None

    if not pid_alive(pid):
        log_path = log_path or os.path.join(sdir, "logs", "guard-restart.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as logf:
            logf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} "
                       f"restart: dead pid={pid} status={status} =====\n")
            try:
                new_pid, argv = spawn_batch(sdir, logf)
            except Exception as e:
                return 12, {"restart_failed": str(e)[:300], "old_pid": pid}
        return 10, {"restarted": True, "old_pid": pid, "new_pid": new_pid,
                    "argv": argv}

    age = heartbeat_age(hb)
    if age is not None and age > heartbeat_min * 60:
        return 11, {"stale": True, "pid": pid,
                    "heartbeat_age_sec": int(age),
                    "still_alive": pid_alive(pid)}
    return 0, {"alive": True, "pid": pid,
               "heartbeat_age_sec": None if age is None else int(age)}


def main():
    ap = argparse.ArgumentParser(description="batch 编排器单轮巡检")
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--heartbeat-min", type=int, default=60)
    args = ap.parse_args()
    rc, info = check_once(os.path.abspath(args.state_dir),
                          heartbeat_min=args.heartbeat_min)
    print(json.dumps({"event": "guard_check", "rc": rc, **info},
                     ensure_ascii=False))
    sys.exit(rc)


if __name__ == "__main__":
    main()
