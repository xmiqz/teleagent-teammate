# -*- coding: utf-8 -*-
"""TeleAgent 目录授权自动裁决策略（零三方依赖，跨平台路径归一化）。

三级裁决，永不使用 always：
  session —— 安全路径，自动回复 session（本会话内有效），任务零等待；
  broad   —— 祖先目录/盘符级宽申，自动 reject 并向上层返回纠偏文案，
             同会话同样 patterns 第 N 次（默认 2）才升级人工；
  manual  —— 真出界/危险路径/非目录类授权，交上层智能体或人工裁决。

路径根由上层通过参数注入（workspace/source/allow），本模块不内置任何
个人路径；default_toolchain_roots() 只按环境变量与通用安装位置探测。
"""
import os
import re
import sys

IS_WIN = sys.platform == "win32"


def norm(p):
    """归一化为绝对路径；非绝对/无法解析返回 None。Windows 容错 / 与 \\。"""
    if not p or not isinstance(p, str):
        return None
    s = p.strip()
    if IS_WIN:
        s = s.replace("/", "\\")
        if s.endswith("*"):
            s = s[:-1]
        # 盘符根 C:\ 的尾反斜杠是根的组成部分，不能剥
        if not re.match(r"^[A-Za-z]:\\?$", s):
            s = s.rstrip("\\")
    else:
        if s.endswith("*"):
            s = s[:-1]
        s = s.rstrip("/")
    if not os.path.isabs(s):
        return None
    try:
        return os.path.normcase(os.path.abspath(s))
    except Exception:
        return None


def under(p, root):
    """p 是否位于 root 内（相等也算）。"""
    if not p or not root:
        return False
    try:
        return os.path.commonpath([p, root]) == root
    except ValueError:
        return False


def benign_unc(pattern):
    """主机名不存在的裸通配 UNC（\\*、\\tmp\\*），实际打不开，视同安全。"""
    if not IS_WIN:
        return False
    s = pattern.replace("/", "\\").strip()
    if s in ("\\*", "\\\\*", "*"):
        return True
    m = re.match(r"^\\+([^\\]+)\\", s)
    return bool(m and m.group(1).lower() in ("tmp", "*"))


def default_toolchain_roots():
    """通用工具链/代理自身目录探测：命中才返回，不猜个人目录。"""
    home = os.path.expanduser("~")
    roots = []
    if IS_WIN:
        roots += [
            os.path.join(home, ".local", "share", "TeleAgent"),
            os.path.join(home, "AppData", "Roaming", "TeleAgent"),
            os.path.join(home, ".trae-cn"),
            os.path.join(home, ".trae"),
            os.path.join(home, "AppData", "Local", "Temp"),
            os.path.join(home, "AppData", "Local", "Programs"),
            os.path.join(home, "tools"),
            r"C:\Program Files (x86)\Microsoft\Edge\Application",
            r"C:\Program Files\Microsoft\Edge\Application",
        ]
        for k in ("TEMP", "TMP"):
            v = os.environ.get(k)
            if v:
                roots.append(v)
    else:
        roots += [
            os.path.join(home, ".local", "share", "TeleAgent"),
            os.path.join(home, ".config", "TeleAgent"),
            "/tmp",
        ]
        for k in ("TMPDIR",):
            v = os.environ.get(k)
            if v:
                roots.append(v)
    # 去重保序 + 存在性校验
    out, seen = [], set()
    for r in roots:
        rn = norm(r)
        if rn and rn not in seen and os.path.isdir(rn):
            seen.add(rn)
            out.append(rn)
    return out


class Policy:
    """三级授权裁决器。

    workspaces: 工作区根（区内非源码路径放行）
    sources:    源码区根（与区内任何覆盖关系都不放行）
    allows:     精确放行根（优先级最高，即使物理位于 source 内）
    strict:     True 时宽申第一次即升级（默认自动驳回+纠偏）
    """

    def __init__(self, workspaces, sources, allows, strict=False):
        self.ws = [norm(x) for x in workspaces if norm(x)]
        self.src = [norm(x) for x in sources if norm(x)]
        self.ok = [norm(x) for x in allows if norm(x)]
        self.strict = strict

    def _safe(self, pattern):
        if benign_unc(pattern):
            return True
        p = norm(pattern)
        if p is None:
            return False
        # allow 精确根最高优先（本任务 worktree 即使物理在 source 区内也放行）
        if any(under(p, r) for r in self.ok):
            return True
        # 与源码区有任何覆盖关系（p 在 src 下，或 p 包住 src）都不算安全
        if any(under(p, s) or under(s, p) for s in self.src):
            return False
        return any(under(p, w) for w in self.ws)

    def safe_roots_text(self):
        return "\n".join("  - " + r for r in sorted(set(self.ws + self.ok)))

    def decide(self, req):
        """返回 'session' / 'broad' / None（None=必须升级）。"""
        if req.get("permission") != "external_directory":
            return None
        pats = req.get("patterns") or req.get("always") or []
        if not pats:
            return None
        if all(self._safe(x) for x in pats):
            return "session"
        if self.strict:
            return None
        roots = self.ws + self.ok
        for pat in pats:
            if benign_unc(pat):
                continue
            p = norm(pat)
            if p is None:
                continue
            # 申请根是任一已知安全根的严格祖先（盘符根、主目录、工作区父目录等）
            if any(under(r, p) and r != p for r in roots):
                return "broad"
        return None

    def correction_text(self, req):
        """宽申驳回后注入会话的纠偏指令（要求对方按最精确叶子目录重申请）。"""
        pats = req.get("patterns") or req.get("always") or []
        return (
            "【值班自动纠偏，无需回复，用行动体现】你的授权申请已被自动驳回：\n"
            f"  {pats}\n"
            "原因：祖先目录/盘符级宽授权永不批准。正确做法：\n"
            "1) 只申请本次工具调用真正需要的【最精确叶子目录】，patterns "
            "只放这一条，不要夹带任何父目录或其他目录；\n"
            "2) 以下根已在值班白名单，对其【精确路径】的申请会被立即自动"
            "放行（白名单根的父目录仍会被驳回）：\n"
            + self.safe_roots_text() + "\n"
            "3) shell 类工具优先把 cwd 设为 TeleAgent 自身数据目录或系统"
            "临时目录，命令内全部使用绝对路径（如 git -C、可执行文件绝对"
            "路径），这种方式通常不需要任何目录授权；\n"
            "4) 主检出、其他任务的工作目录、系统目录、真实 UNC 共享永不放行。\n"
            "请立即按精确路径重新申请并继续原任务。"
        )
