# -*- coding: utf-8 -*-
"""procutil.py — 跨平台进程判活与进程树清理（零三方依赖）。

为什么不用 os.kill(pid, 0)：
  Windows 上它退化为 OpenProcess 成功性检查——进程刚退出后僵尸句柄仍可能
  短暂存活，导致"死进程被判活"（守卫因此漏拉一轮）。Windows 一律用
  OpenProcess + GetExitCodeProcess == STILL_ACTIVE(259) 双条件判定。
"""
import os
import subprocess
import sys

IS_WIN = sys.platform == "win32"
_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def pid_alive(pid):
    """真正在运行才返回 True。pid 为空/非整数/僵尸句柄一律 False。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if IS_WIN:
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == _STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但无权发信号
    except OSError:
        return False
    return True


def kill_tree(pid):
    """强杀进程树。仅在判死后调用；失败不抛异常。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return
    if IS_WIN:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True)
        return
    try:
        os.killpg(os.getpgid(pid), 9)
    except OSError:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def single_instance_mutex(name):
    """Windows 命名互斥量单实例：已存在返回 None，新持有返回句柄。

    POSIX 无等价物，调用方退回 pidfile 判活。
    """
    if not IS_WIN:
        return None
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL,
                                 wintypes.LPCWSTR]
    k32.CreateMutexW.use_last_error = True
    mutex = k32.CreateMutexW(None, False, name)
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        if mutex:
            k32.CloseHandle(mutex)
        return None
    return mutex
