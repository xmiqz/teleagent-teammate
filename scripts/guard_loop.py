#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""guard_loop.py — batch.py 常驻看门狗循环（无界面、零打扰）。

每 180 秒执行一次 guard.check_once：
  - 批次进程消失           -> 按 launch.json 立即无损重拉；
  - 心跳连续陈旧 N 轮      -> 判定僵尸/长阻塞，强杀进程树后等下一轮自动重拉；
  - 批次 complete/aborted  -> 守卫自退（不做任何多余重启）；
  - 自动重拉连续失败       -> 累计 5 轮后自退并落盘，交调用方智能体处置。

开机自启（运行时按当前机器路径生成，源码不内置任何个人路径）：
  python guard_loop.py <state-dir 参数...> --install-startup
    Windows: 在当前用户 Startup 文件夹写一个静默 VBS（零管理员）；
    POSIX  : 打印 cron @reboot / systemd user unit 安装指引。
  python guard_loop.py --state-dir <dir> --uninstall-startup

直接运行：
  python guard_loop.py --state-dir <dir> [--interval 180]
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import guard  # noqa: E402
from procutil import kill_tree, single_instance_mutex  # noqa: E402


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def startup_vbs_name(sdir):
    return "TeleagentTeammateGuard_" + os.path.basename(os.path.normpath(sdir))


def startup_vbs_path(sdir):
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return os.path.join(
        appdata, "Microsoft", "Windows", "Start Menu", "Programs",
        "Startup", startup_vbs_name(sdir) + ".vbs")


def _runner_pythonw():
    """优先同目录 pythonw.exe（无控制台窗口），不存在回退 python。"""
    d = os.path.dirname(sys.executable)
    cand = os.path.join(d, "pythonw.exe")
    return cand if os.path.isfile(cand) else sys.executable


def install_startup(sdir, namespace):
    """在用户 Startup 文件夹写静默 VBS；返回写入路径。"""
    args = ["--state-dir", os.path.abspath(sdir),
            "--interval", str(namespace.interval),
            "--heartbeat-min", str(namespace.heartbeat_min),
            "--kill-rounds", str(namespace.kill_rounds)]
    target = '"{}" "{}" '.format(_runner_pythonw(),
                                 os.path.join(HERE, "guard_loop.py"))
    target += " ".join('"{}"'.format(a) for a in args)
    vbs = ('Set ws = CreateObject("Wscript.Shell")\r\n'
           'ws.Run "{}", 0, False\r\n').format(target.replace('"', '""'))
    if sys.platform == "win32":
        path = startup_vbs_path(sdir)
        if not path:
            raise RuntimeError("APPDATA 未设置，无法定位 Startup 文件夹")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(vbs)
        return path
    print("POSIX 开机自启二选一：\n"
          "  crontab -e 追加：\n"
          "    @reboot {} {} {}\n"
          "  或建 systemd --user unit 执行同一命令。".format(
              sys.executable, os.path.join(HERE, "guard_loop.py"),
              " ".join(args)))
    return None


def uninstall_startup(sdir):
    path = startup_vbs_path(sdir)
    if path and os.path.isfile(path):
        os.remove(path)
        return path
    return None


def main():
    ap = argparse.ArgumentParser(description="batch 常驻看门狗循环")
    ap.add_argument("--state-dir")
    ap.add_argument("--interval", type=int, default=180)
    ap.add_argument("--heartbeat-min", type=int, default=60)
    ap.add_argument("--kill-rounds", type=int, default=3,
                    help="心跳连续陈旧多少轮后强杀（默认3轮≈9分钟）")
    ap.add_argument("--install-startup", action="store_true",
                    help="把本守卫写入用户开机自启后退出")
    ap.add_argument("--uninstall-startup", action="store_true",
                    help="移除本守卫对应的开机自启项后退出")
    args = ap.parse_args()

    if args.install_startup or args.uninstall_startup:
        if not args.state_dir:
            ap.error("--install-startup/--uninstall-startup 需要 --state-dir")
        if args.install_startup:
            path = install_startup(args.state_dir, args)
            print(json.dumps({"event": "startup_installed", "path": path},
                             ensure_ascii=False))
        else:
            path = uninstall_startup(args.state_dir)
            print(json.dumps({"event": "startup_removed", "path": path},
                             ensure_ascii=False))
        return 0

    if not args.state_dir:
        ap.error("--state-dir 必填")
    sdir = os.path.abspath(args.state_dir)
    os.makedirs(os.path.join(sdir, "logs"), exist_ok=True)

    # 硬单实例：同一 state-dir 只允许一个守卫（互斥量随进程死亡自动释放）
    mutex = single_instance_mutex(
        "Local\\TeleagentTeammateGuard_" + os.path.basename(sdir))
    if mutex is None and sys.platform == "win32":
        print(json.dumps({"event": "guard_duplicate_exit",
                          "state_dir": sdir}, ensure_ascii=False))
        return 0

    pid_path = os.path.join(sdir, "guard.pid")
    ghb_path = os.path.join(sdir, "guard-heartbeat.json")
    glog = os.path.join(sdir, "logs", "guard.jsonl")

    with open(pid_path, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))

    def emit(event, **kw):
        rec = {"ts": now(), "event": event, **kw}
        line = json.dumps(rec, ensure_ascii=False)
        print(line, flush=True)
        with open(glog, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    stale_rounds = 0
    restart_fail_rounds = 0
    emit("guard_start", state_dir=sdir, interval=args.interval,
         heartbeat_min=args.heartbeat_min, kill_rounds=args.kill_rounds)

    while True:
        try:
            rc, info = guard.check_once(
                sdir, heartbeat_min=args.heartbeat_min)
        except Exception as e:
            rc, info = 99, {"exception": f"{type(e).__name__}: {e}"[:300]}

        with open(ghb_path, "w", encoding="utf-8") as f:
            json.dump({"ts": now(), "pid": os.getpid(),
                       "last_rc": rc, "last_info": info}, f,
                      ensure_ascii=False)

        if rc == 0 and info.get("terminal"):
            emit("guard_exit", reason=f"batch {info['terminal']}")
            return 0
        if rc == 0:
            stale_rounds = 0
            restart_fail_rounds = 0
        elif rc == 10:
            stale_rounds = 0
            restart_fail_rounds = 0
            emit("BATCH_RESTARTED", **info)
        elif rc == 11:
            stale_rounds += 1
            emit("HEARTBEAT_STALE", rounds=stale_rounds, **info)
            if stale_rounds >= args.kill_rounds:
                emit("ZOMBIE_KILL", pid=info.get("pid"),
                     rounds=stale_rounds)
                kill_tree(info.get("pid"))
                stale_rounds = 0
        elif rc == 12:
            restart_fail_rounds += 1
            emit("RESTART_FAILED", rounds=restart_fail_rounds, **info)
            if restart_fail_rounds >= 5:
                emit("guard_exit", reason="restart failed 5 rounds")
                return 12
        else:
            emit("guard_warn", rc=rc, **info)

        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
