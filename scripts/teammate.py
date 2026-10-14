#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""teammate.py — TeleAgent 本地分包客户端（零三方依赖，Windows 为主，POSIX 尽力兼容）。

给"发分包智能体"使用的命令行工具：向本机 TeleAgent 桌面应用分发任务、
无人值守监控、自动处理授权、收割结果、中止/接续。所有输出为单行 JSON 事件，
便于上层智能体或脚本解析；任何密钥只在内存中存在，不打印、不日志、不落盘。

命令一览：
  doctor                         健康检查（进程/密钥/API/模型自动选择）
  models                         列出可用模型与自动选择结果
  config                         打印解析后的配置路径（不含敏感值）
  dispatch                       分发任务（--wait 可同步等完）
  run                            分发 + 一体化无人值守监控（推荐入口）
  monitor                        监控已有会话
  harvest                        收割结果（跨进程/跨会话恢复入口）
  status                         忙闲查询
  messages                       会话消息明细
  prompt                         向会话追加指令（自动纠偏/接续）
  permission list                查看待裁决授权队列
  permission reply               回复授权（once/session/always/reject）
  abort                          中止会话当前任务
  list / delete                  列出/删除会话

退出码（监控类命令统一语义）：
  0 完成   2 超时（窗口到期打卡，非失败）   3 会话异常中止   4 授权需上层处置
"""
import argparse
import ast
import base64
import ctypes
import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# 路径与配置（全部经环境变量/配置文件解析，不内置任何个人路径）
# ---------------------------------------------------------------------------

HOME = os.path.expanduser("~")
IS_WIN = sys.platform == "win32"

DEFAULT_BASE_URL = os.environ.get("TELEAGENT_BASE_URL", "http://127.0.0.1:4397")
SIGN_VERSION = "local-v1"

DATA_DIR = os.environ.get(
    "TELEAGENT_DATA_DIR",
    os.path.join(HOME, ".local", "share", "TeleAgent"),
)
CONFIG_DIR = os.environ.get(
    "TELEAGENT_TEAMMATE_CONFIG_DIR",
    os.path.join(HOME, ".config", "teleagent-teammate"),
)
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
REGISTRY_FILE = os.path.join(DATA_DIR, "teleagent-teammate-registry.json")
# 旧版技能（teleagent-dispatch）注册表，仅做只读迁移
LEGACY_REGISTRY_FILE = os.path.join(DATA_DIR, "trae-dispatch-registry.json")
STATE_FILE = os.path.join(DATA_DIR, "im-bridge-opencode-state.json")
REGISTRY_KEEP = 200

ERROR_PATTERNS = [
    "dial tcp", "connection refused", "econnrefused", "actively refused",
    "unauthorized", "forbidden", "rate limit", "quota exceeded",
    "context deadline exceeded", "internal server error", "internal error",
    "overloaded", "insufficient", "invalid api key", "authentication",
]

if IS_WIN:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def load_user_config():
    """~/.config/teleagent-teammate/config.json，当前仅识别 model/popup 等可公开项。"""
    try:
        with open(CONFIG_FILE, encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Windows：从 TeleAgent 进程环境块（PEB）读取会话密钥
# POSIX：仅支持环境变量/配置文件注入（桌面应用形态以 Windows 为主）
# ---------------------------------------------------------------------------

if IS_WIN:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")

    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_READ = 0x0010
    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_uint32),
            ("cntUsage", ctypes.c_uint32),
            ("th32ProcessID", ctypes.c_uint32),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", ctypes.c_uint32),
            ("cntThreads", ctypes.c_uint32),
            ("th32ParentProcessID", ctypes.c_uint32),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.c_uint32),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    class PROCESS_BASIC_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("Reserved1", ctypes.c_void_p),
            ("PebBaseAddress", ctypes.c_void_p),
            ("Reserved2", ctypes.c_void_p * 2),
            ("UniqueProcessId", ctypes.c_void_p),
            ("Reserved3", ctypes.c_void_p),
        ]

    AGENT_PROCESS_NAMES = ("teleagent.exe", "node.exe")

    def list_processes():
        kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        kernel32.Process32FirstW.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32NextW.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == ctypes.c_void_p(-1).value:
            return []
        result = []
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        try:
            if kernel32.Process32FirstW(snap, ctypes.byref(entry)):
                while True:
                    result.append((entry.th32ProcessID, entry.szExeFile))
                    if not kernel32.Process32NextW(snap, ctypes.byref(entry)):
                        break
        finally:
            kernel32.CloseHandle(snap)
        return result

    def _read_mem(handle, addr, size):
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t(0)
        if not kernel32.ReadProcessMemory(
                handle, ctypes.c_void_p(addr), buf, size, ctypes.byref(got)):
            return None
        return buf.raw[:got.value]

    def _read_ptr(handle, addr):
        data = _read_mem(handle, addr, 8)
        if data is None or len(data) < 8:
            return None
        return int.from_bytes(data, "little")

    def read_process_env(pid):
        """读取目标进程环境块，返回 dict；失败返回 None。"""
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not handle:
            return None
        try:
            pbi = PROCESS_BASIC_INFORMATION()
            retlen = ctypes.c_ulong(0)
            status = ntdll.NtQueryInformationProcess(
                handle, 0, ctypes.byref(pbi), ctypes.sizeof(pbi),
                ctypes.byref(retlen))
            if status != 0 or not pbi.PebBaseAddress:
                return None
            params = _read_ptr(handle, pbi.PebBaseAddress + 0x20)
            if not params:
                return None
            env_ptr = _read_ptr(handle, params + 0x80)
            env_size = _read_ptr(handle, params + 0x3F0)
            if not env_ptr:
                return None
            if not env_size or env_size > 4 * 1024 * 1024:
                env_size = 65536
            data = _read_mem(handle, env_ptr, env_size)
            if data is None:
                return None
            text = data.decode("utf-16-le", errors="replace")
            if "\u0000\u0000" not in text and env_size == 65536:
                data = _read_mem(handle, env_ptr, 512 * 1024)
                if data is not None:
                    text = data.decode("utf-16-le", errors="replace")
            end = text.find("\u0000\u0000")
            if end > 0:
                text = text[:end]
            return dict(
                item.split("=", 1) for item in text.split("\u0000") if "=" in item
            )
        finally:
            kernel32.CloseHandle(handle)

else:  # POSIX 占位：进程发现不支持，凭据走环境变量/配置
    AGENT_PROCESS_NAMES = ()

    def list_processes():
        return []

    def read_process_env(pid):
        return None


class TeleAgentError(Exception):
    pass


# ---------------------------------------------------------------------------
# 应用自动拉起（TeleAgent 没运行时，调用方无需人工去点桌面图标）
# ---------------------------------------------------------------------------

def locate_agent_exe():
    """按环境变量 > 常见安装位置定位 TeleAgent 可执行文件；找不到返回 None。"""
    env_exe = os.environ.get("TELEAGENT_EXE")
    if env_exe and os.path.isfile(env_exe):
        return env_exe
    candidates = []
    if IS_WIN:
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pfx86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", os.path.join(HOME, "AppData", "Local"))
        candidates += [
            os.path.join(local, "Programs", "TeleAgent", "TeleAgent.exe"),
            os.path.join(pf, "TeleAgent", "TeleAgent.exe"),
            os.path.join(pfx86, "TeleAgent", "TeleAgent.exe"),
            os.path.join(local, "TeleAgent", "TeleAgent.exe"),
        ]
    else:
        candidates += [
            "/usr/bin/teleagent",
            "/opt/TeleAgent/teleagent",
            os.path.join(HOME, ".local", "bin", "teleagent"),
        ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def agent_process_present():
    return any((n or "").lower() in AGENT_PROCESS_NAMES
               for _, n in list_processes())


def agent_named_present():
    """仅认 teleagent.exe；任意 node.exe 不算（开发机常有无关 node 进程）。"""
    return any((n or "").lower() == "teleagent.exe"
               for _, n in list_processes())


def launch_agent(exe_path):
    """脱离当前进程启动 TeleAgent 桌面应用（不继承控制台）。"""
    if IS_WIN:
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess_kwargs = {"creationflags": 0x00000008 | 0x00000200}
    else:
        subprocess_kwargs = {"start_new_session": True}
    subprocess.Popen([exe_path], close_fds=True,
                     stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL,
                     **subprocess_kwargs)


def ensure_agent_running(timeout=120, autostart=True):
    """确保 TeleAgent 进程存在且本地 API 可用。

    返回 None；失败抛 TeleAgentError（调用方据此走环境兜底）。
    """
    if find_credentials():
        return
    if not agent_named_present():
        exe = locate_agent_exe() if autostart else None
        if exe:
            emit({"event": "agent_autostart", "path": exe})
            try:
                launch_agent(exe)
            except Exception as e:
                raise TeleAgentError(f"尝试拉起 TeleAgent 失败：{e}")
        elif not autostart:
            raise TeleAgentError("TeleAgent 未运行（已禁用自动拉起）")
        else:
            raise TeleAgentError(
                "TeleAgent 未运行，且未在常见位置找到可执行文件；"
                "请启动 TeleAgent，或设置 TELEAGENT_EXE 环境变量指向其路径")
    # teleagent.exe 已在但密钥尚不可读：启动/自动更新窗口期，退避等待
    deadline = time.time() + timeout
    delay = 2.0
    while time.time() < deadline:
        if find_credentials():
            try:
                c = TeleAgentClient()
                st, _ = c.call("GET", "/session", timeout=10)
                if st == 200:
                    emit({"event": "agent_ready"})
                    return
            except TeleAgentError:
                pass
        time.sleep(delay)
        delay = min(delay * 1.5, 8.0)
    raise TeleAgentError(
        f"TeleAgent 已启动但 {timeout}s 内本地 API 未就绪（自动更新窗口期可重试）")


def find_credentials():
    """发现本地会话密钥。优先级：环境变量 > TeleAgent 进程 PEB > 状态文件 URL。

    成功返回 {"session_key", "base_url", "source_pid", "source_process"}。
    """
    env_key = os.environ.get("SUPER_AGENT_LOCAL_SESSION_KEY")
    if env_key:
        return {
            "session_key": env_key,
            "base_url": os.environ.get("OPENCODE_BASE_URL")
            or os.environ.get("TELEAGENT_BASE_URL") or DEFAULT_BASE_URL,
            "source_pid": os.getpid(),
            "source_process": "env",
        }

    state_url = None
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state_url = json.load(f).get("url")
    except Exception:
        pass

    candidates = [
        (pid, name) for pid, name in list_processes()
        if (name or "").lower() in AGENT_PROCESS_NAMES
    ]
    candidates.sort(key=lambda x: 0 if x[1].lower() == "node.exe" else 1)
    for pid, name in candidates:
        env = read_process_env(pid)
        if env and env.get("SUPER_AGENT_LOCAL_SESSION_KEY"):
            return {
                "session_key": env["SUPER_AGENT_LOCAL_SESSION_KEY"],
                "base_url": env.get("OPENCODE_BASE_URL") or state_url
                or DEFAULT_BASE_URL,
                "source_pid": pid,
                "source_process": name,
            }
    return None


# ---------------------------------------------------------------------------
# 签名与 HTTP
# ---------------------------------------------------------------------------

def sign_headers(method, path_with_query, session_key):
    ts = str(int(time.time() * 1000))
    import secrets
    nonce = secrets.token_hex(12)
    payload = "\n".join(
        [SIGN_VERSION, method.upper(), path_with_query, ts, nonce])
    sig = base64.urlsafe_b64encode(
        hmac.new(session_key.encode(), payload.encode(),
                 hashlib.sha256).digest()
    ).decode().rstrip("=")
    return {
        "X-SA-Sign-Version": SIGN_VERSION,
        "X-SA-Timestamp": ts,
        "X-SA-Nonce": nonce,
        "X-SA-Signature": sig,
    }


class TeleAgentClient:
    def __init__(self):
        self.creds = None
        self._lock = threading.Lock()  # call 可能被监控/授权线程并发使用

    def ensure_creds(self, force=False):
        if self.creds and not force:
            return self.creds
        creds = find_credentials()
        if not creds:
            running = bool(AGENT_PROCESS_NAMES) and any(
                (n or "").lower() in AGENT_PROCESS_NAMES
                for _, n in list_processes())
            if running:
                raise TeleAgentError(
                    "TeleAgent 正在运行但无法读取本地会话密钥。"
                    "请尝试重启 TeleAgent，或改用 64 位 Python 运行本脚本。")
            raise TeleAgentError(
                "TeleAgent 未运行。请先启动 TeleAgent 桌面应用后重试；"
                "若刚自动更新，等待 1-2 分钟。")
        self.creds = creds
        return creds

    def call(self, method, path, body=None, timeout=30):
        with self._lock:
            creds = self.ensure_creds()
            last_err = None
            for attempt in (1, 2):
                headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/json"}
                headers.update(
                    sign_headers(method, path, creds["session_key"]))
                data = json.dumps(body).encode("utf-8") if body is not None else None
                req = urllib.request.Request(
                    creds["base_url"].rstrip("/") + path, data=data,
                    headers=headers, method=method)
                try:
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        raw = resp.read().decode("utf-8", "replace")
                        return resp.status, (
                            json.loads(raw) if raw.strip() else None)
                except urllib.error.HTTPError as e:
                    detail = e.read().decode("utf-8", "replace")[:400]
                    if e.code == 401 and attempt == 1:
                        try:
                            creds = self.ensure_creds(force=True)
                            continue
                        except TeleAgentError:
                            pass
                    raise TeleAgentError(
                        f"HTTP {e.code} {method} {path}: {detail}")
                except urllib.error.URLError as e:
                    raise TeleAgentError(
                        f"无法连接 TeleAgent API（{creds['base_url']}）：{e.reason}")
            raise last_err or TeleAgentError("请求失败")


def call_with_retry(client, method, path, body=None, connect_timeout=120,
                    autostart=True):
    """连接类错误：指数退避 + 主动拉起（应对 TeleAgent 未运行/重启/自动更新）。

    主动拉起不以"当前已连接"为门槛——未运行也必须尝试拉起，调用方无需
    人工去启动桌面应用。整窗口内最多自动拉起一次。
    """
    start = time.time()
    delay = 2.0
    autostart_tried = False
    while True:
        try:
            return client.call(method, path, body)
        except TeleAgentError as e:
            msg = str(e)
            reconnectable = any(
                s in msg for s in ("无法连接", "未运行", "无法读取"))
            if not reconnectable or time.time() - start > connect_timeout:
                raise
            if autostart and not autostart_tried:
                autostart_tried = True
                try:
                    ensure_agent_running(
                        timeout=min(90, max(10, connect_timeout)))
                except TeleAgentError:
                    pass  # 拉起失败也继续退避重试，最终交调用方兜底
            time.sleep(delay)
            delay = min(delay * 1.5, 10.0)


# ---------------------------------------------------------------------------
# 模型自动选择
# ---------------------------------------------------------------------------

# 编码分包任务需要工具调用能力；优先编码专线供应商，避开 lite/nano/flash 小模型
def _model_score(provider_id, model_id):
    pid, mid = provider_id.lower(), model_id.lower()
    score = 0
    if "for-coding" in pid or "coding" in pid:
        score += 3
    if any(bad in mid for bad in ("lite", "nano", "flash", "mini")):
        score -= 2
    if any(good in mid for good in ("pro", "flagship", "max")):
        score += 1
    return score


def agent_env_names():
    """收集本机环境与 TeleAgent 进程环境中的变量名集合（只取名字，绝不取值）。

    用于判断 custom 供应商要求的凭据是否已配置，不落盘、不打印。
    """
    names = set(os.environ.keys())
    for pid, name in list_processes():
        if (name or "").lower() in AGENT_PROCESS_NAMES:
            env = read_process_env(pid)
            if env:
                names.update(env.keys())
    return names


def _provider_ready(entry, env_names):
    """供应商是否真正可创建模型：config 已带 options 凭据，或其要求的 env 已配置。

    市场目录里 enabled_providers 只表示"在目录中启用"，不代表凭据齐全；
    对未就绪供应商派单会立刻收到 create chat model: provider not found。
    """
    opts = entry.get("options")
    if isinstance(opts, dict) and (opts.get("apiKey") or opts.get("baseURL")):
        return True
    return any(k in env_names for k in (entry.get("env") or []))


def candidate_models(client):
    """返回 [(score, provider, model, toolcall)]，按选择优先级排序。

    仅纳入"凭据就绪"的供应商（options 已配置或所需 env 变量存在），
    市场目录中的未配置条目一律不选——否则 TeleAgent 会在 5 秒内以
    create chat model: provider not found 中止会话。

    走 call_with_retry：TeleAgent 未运行/重启时指数退避并自动拉起，
    避免 run/dispatch 在应用关闭时直接拿到误导性的"无可用模型"错误。
    """
    _, cfg = call_with_retry(client, "GET", "/config")
    _, cat = call_with_retry(client, "GET", "/config/providers")
    cfg = cfg or {}
    cat = cat or {}
    defaults = cat.get("default") or {}
    env_names = agent_env_names()
    out = []
    for p in cat.get("providers") or []:
        pid = p.get("id")
        mid = defaults.get(pid)
        if not pid or not mid or not _provider_ready(p, env_names):
            continue
        m = (p.get("models") or {}).get(mid) or {}
        caps = m.get("capabilities") or {}
        toolcall = bool(caps.get("toolcall") or caps.get("tool_call"))
        out.append((_model_score(pid, mid), pid, mid, toolcall))
    out.sort(key=lambda x: (-x[0], x[1]))
    return out, cfg.get("small_model")


def ordered_models(client, model_str=None):
    """返回本次任务可用的模型序列 [(provider, model, source)]，用于故障转移。

    第一位：显式/环境/配置指定（若有）；其后：AUTO 选出的 toolcall 候选。
    """
    explicit = (
        model_str
        or os.environ.get("TELEAGENT_TEAMMATE_MODEL")
        or (load_user_config().get("model"))
    )
    cands, _ = candidate_models(client)
    chain = []
    if explicit and explicit.lower() != "auto":
        if "/" in explicit:
            p, m = explicit.split("/", 1)
            chain.append((p.strip(), m.strip(), "explicit"))
        else:
            chain.append(("", explicit.strip(), "explicit"))
    for _, p, m, tc in cands:
        if tc and (p, m) not in [(a, b) for a, b, _ in chain]:
            chain.append((p, m, "auto"))
    if not chain and cands:
        _, p, m, _ = cands[0]
        chain.append((p, m, "auto-no-toolcall"))
    if not chain:
        raise TeleAgentError(
            "没有凭据就绪的模型供应商（/config/providers 中无带 options 的条目，"
            "且 custom 供应商要求的 *_API_KEY 环境变量均未配置）。"
            "请先在 TeleAgent 中连接供应商，或用 --model Provider/Model 显式指定。")
    return chain


def resolve_model(client, model_str):
    """解析单个 (provider, model, source)。优先级：显式 > 环境变量 > 配置 > AUTO。"""
    p, m, source = ordered_models(client, model_str)[0]
    return p, m, source


# 模型/供应商侧故障特征（命中即应换模型重派，而不是傻等或升级人工）
MODEL_FAULT_PATTERNS = [
    "dial tcp", "11434", "rate limit", "quota", "insufficient",
    "invalid api key", "unauthorized", "forbidden", "overloaded",
    "model", "provider", "upstream", "bad gateway", "gateway timeout",
    "502", "503", "504", "context deadline",
]


def looks_like_model_fault(event):
    """根据终态事件/线索判断是否供应商侧故障（可换模型重派）。"""
    blob = json.dumps(
        {"h": event.get("error_hints"), "t": event.get("last_text"),
         "d": event.get("detail")}, ensure_ascii=False).lower()
    return any(pat in blob for pat in MODEL_FAULT_PATTERNS)


# ---------------------------------------------------------------------------
# 会话状态分析
# ---------------------------------------------------------------------------

def parse_tokens(raw):
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return ast.literal_eval(raw)
        except Exception:
            return None
    return None


def analyze_session(client, sid):
    """返回会话状态摘要 dict。"""
    _, status_data = client.call("GET", "/session/status")
    busy_map = status_data if isinstance(status_data, dict) else {}
    busy = sid in busy_map

    _, msgs = client.call("GET", f"/session/{sid}/message")
    msgs = msgs if isinstance(msgs, list) else []

    tools = []
    for m in msgs:
        for p in m.get("parts") or []:
            if p.get("type") == "tool":
                state = p.get("state") or {}
                tools.append(
                    {"tool": p.get("tool"), "status": state.get("status")})

    finish_reason = None
    last_texts = []
    last_role = None
    last_reasoning = None
    tokens = None
    model_error = None
    if msgs:
        last = msgs[-1]
        last_info = last.get("info") or {}
        last_role = last_info.get("role")
        # 消息级终态：供应商创建模型失败等错误不产生 parts，
        # 只在 info.finish=error / info.error 里给出原因
        if last_info.get("finish") in ("error", "aborted"):
            finish_reason = last_info.get("finish")
            err = last_info.get("error")
            if isinstance(err, dict):
                data = err.get("data") or {}
                msg = data.get("message") if isinstance(data, dict) else None
                model_error = {"name": err.get("name"), "message": msg}
        for p in last.get("parts") or []:
            t = p.get("type")
            if t == "step-finish":
                finish_reason = p.get("reason")
                tokens = parse_tokens(p.get("tokens"))
            elif t == "text":
                last_texts.append(p.get("text") or "")
            elif t == "reasoning":
                last_reasoning = p.get("text") or None

    if busy:
        state = "running"
    else:
        if finish_reason == "stop":
            state = "done"
        elif finish_reason in ("error", "aborted"):
            state = finish_reason
        elif last_role == "assistant":
            state = "aborted"
        else:
            state = "pending"

    answer = "\n".join(t for t in last_texts if t.strip()).strip() \
        if state == "done" else None
    error_texts = [
        t for t in last_texts
        if any(pat in t.lower() for pat in ERROR_PATTERNS)]
    errored_tools = [t for t in tools if t.get("status") in ("error", "failed")]
    if model_error and model_error.get("message"):
        error_texts = error_texts + [model_error["message"]]

    return {
        "session_id": sid,
        "state": state,
        "busy": busy,
        "messages": len(msgs),
        "tools": tools,
        "finish_reason": finish_reason,
        "model_error": model_error,
        "last_role": last_role,
        "last_reasoning": (last_reasoning or "").strip()[:200] or None,
        "last_text": ("\n".join(last_texts).strip()[:300] or None),
        "answer": answer,
        "tokens": tokens,
        "error_hints": (
            error_texts[:3] + [t["tool"] for t in errored_tools]) or None,
    }


def breakpoint_brief(client, sid, last_n=8):
    """会话断点摘要：供自动续派任务书与 FALLBACK_NEEDED 事件使用。"""
    try:
        _, msgs = client.call("GET", f"/session/{sid}/message")
    except TeleAgentError as e:
        return {"error": str(e)[:200]}
    msgs = msgs if isinstance(msgs, list) else []
    tail = []
    for m in msgs[-last_n:]:
        minfo = m.get("info") or {}
        role = minfo.get("role")
        if minfo.get("finish") == "error":
            err = minfo.get("error") or {}
            data = err.get("data") or {}
            tail.append({"role": role, "type": "model_error",
                         "message": data.get("message") if isinstance(data, dict) else None})
        for p in m.get("parts") or []:
            t = p.get("type")
            if t == "text":
                txt = (p.get("text") or "").strip()
                if txt:
                    tail.append({"role": role, "type": "text",
                                 "text": txt[:500]})
            elif t == "tool":
                st = p.get("state") or {}
                tail.append({"role": role, "type": "tool",
                             "tool": p.get("tool"),
                             "status": st.get("status")})
            elif t == "step-finish":
                tail.append({"role": role, "type": "finish",
                             "reason": p.get("reason")})
    return {"messages": len(msgs), "tail": tail}


def continuation_prompt(original_prompt, brief, reason):
    """根据原任务书+断点生成接续任务书（严禁重做已完成部分）。"""
    lines = [
        "【自动接续任务】上一个执行会话异常中断（原因：%s）。" % reason,
        "你是接续工程师，下面给你原任务书与上一会话的断点轨迹。",
        "",
        "铁律：",
        "1. 严禁重做已完成部分。先用只读方式（git status/diff、列目录、",
        "读产物文件）核对现场，确认哪些已完成、哪些未完成；",
        "2. 只做剩余收口动作；现场损坏无法判断时，保留证据并在结果中 STOP 上报；",
        "3. 完成后写结果文件并立即结束，不要扩展范围。",
        "",
        "## 断点轨迹（最近消息，倒序来源中的末尾片段）",
        "```",
        json.dumps(brief.get("tail", []), ensure_ascii=False, indent=1)[:3000],
        "```",
        "",
        "## 原任务书",
        original_prompt,
    ]
    return "\n".join(lines)


def terminal_event(sid, info, elapsed):
    """由 analyze_session 摘要构造终态事件；非终态返回 None。"""
    st = info["state"]
    if st == "done":
        return {
            "event": "done", "session_id": sid,
            "elapsed": round(elapsed, 1), "answer": info["answer"],
            "tokens": info["tokens"],
            "tool_calls": len(info["tools"]),
            "error_hints": info["error_hints"],
        }, 0
    if st in ("error", "aborted"):
        return {
            "event": st, "session_id": sid,
            "elapsed": round(elapsed, 1),
            "finish_reason": info["finish_reason"],
            "last_text": info["last_text"],
            "error_hints": info["error_hints"],
            "detail": info.get("model_error"),
            "hint": "任务异常终止：先 messages 看断点，可修正后重新分发或派接续会话",
        }, 3
    return None


def wait_window(client, sid, window, interval=5, start=None,
                quiet=True, progress_sink=None, on_tick=None):
    """在 window 秒窗口内监控一个会话。

    返回 (rc, event)：0 完成 / 2 窗口到期（仍在跑）/ 3 异常 / 1 连接类硬错误。
    progress_sink 可选回调，接收 progress 事件。
    on_tick 可选回调，每轮成功 analyze 后以 info 调用；返回 (rc, event)
    可立即终态短路（用于窗口内的授权裁决与停滞检测，避免整窗阻塞）。
    """
    start = start if start is not None else time.time()
    delay = 2.0
    autostart_tried = False
    reported_tools = 0
    last_state = None
    last_emit = 0.0
    last_info = None
    while True:
        elapsed = time.time() - start
        if elapsed > window:
            return 2, {
                "event": "timeout", "session_id": sid,
                "elapsed": round(elapsed, 1),
                "state": (last_info or {}).get("state"),
                "messages": (last_info or {}).get("messages"),
                "tool_calls": len((last_info or {}).get("tools") or []),
                "hint": "窗口到期，续窗即可（非失败）"}
        try:
            info = analyze_session(client, sid)
            last_info = info
        except TeleAgentError as e:
            msg = str(e)
            if any(s in msg for s in ("无法连接", "未运行", "无法读取")):
                if progress_sink:
                    progress_sink({
                        "event": "reconnect", "elapsed": round(elapsed, 1),
                        "detail": msg})
                # 任务执行中 TeleAgent 被杀/崩溃/升级：窗口内主动拉起一次，
                # 调用方智能体无需感知，密钥轮换由 client 401 重试自行处理
                if not autostart_tried:
                    autostart_tried = True
                    try:
                        ensure_agent_running(timeout=90, autostart=True)
                    except TeleAgentError:
                        pass
                time.sleep(delay)
                delay = min(delay * 1.5, 15.0)
                continue
            return 1, {"event": "error", "session_id": sid, "detail": msg}
        delay = 2.0
        new_tools = len(info["tools"])
        should_emit = (
            info["state"] != last_state
            or new_tools > reported_tools
            or elapsed - last_emit >= 60)
        if not quiet and should_emit:
            prog = {
                "event": "progress", "elapsed": round(elapsed, 1),
                "state": info["state"], "messages": info["messages"],
                "tool_calls": new_tools,
                "last_reasoning": info["last_reasoning"],
                "last_text": info["last_text"]}
            if new_tools > reported_tools:
                prog["recent_tools"] = info["tools"][reported_tools:]
                reported_tools = new_tools
            if progress_sink:
                progress_sink(prog)
            else:
                emit(prog)
            last_emit = elapsed
        last_state = info["state"]
        if on_tick:
            try:
                verdict = on_tick(info)
            except Exception as e:
                emit({"event": "on_tick_error", "detail": str(e)[:200]})
                verdict = None
            if verdict:
                return verdict
        term = terminal_event(sid, info, elapsed)
        if term:
            return term[1], term[0]
        time.sleep(interval)


# ---------------------------------------------------------------------------
# 结果文件与注册表
# ---------------------------------------------------------------------------

def write_result_file(path, event, title=None):
    """把终态事件写成人类可读 markdown，供跨 turn 收割。"""
    sid = event.get("session_id") or "未知"
    lines = [
        f"# TeleAgent 任务结果：{title or sid}", "",
        f"- 会话 ID：`{sid}`",
        f"- 状态：`{event.get('event')}`"]
    if event.get("elapsed") is not None:
        lines.append(f"- 耗时：{event['elapsed']} 秒")
    if event.get("tool_calls") is not None:
        lines.append(f"- 工具调用：{event['tool_calls']} 次")
    if event.get("tokens"):
        lines.append(f"- Token：{json.dumps(event['tokens'], ensure_ascii=False)}")
    lines.append("")
    if event.get("event") == "done":
        lines += ["---", "", event.get("answer") or "（无输出）"]
    else:
        detail = event.get("last_text") or event.get("detail") or ""
        if detail:
            lines += ["**最后输出**：", "", detail, ""]
        if event.get("error_hints"):
            lines += [f"> 错误线索：{event['error_hints']}", ""]
        if event.get("hint"):
            lines.append(f"> 提示：{event['hint']}")
    content = "\n".join(lines).rstrip() + "\n"
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def load_registry():
    """加载新注册表；新文件不存在时只读迁移旧版注册表。"""
    for path, migrated in ((REGISTRY_FILE, False),
                           (LEGACY_REGISTRY_FILE, True)):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            sessions = data.get("sessions") if isinstance(data, dict) else data
            if isinstance(sessions, list):
                rows = [s for s in sessions
                        if isinstance(s, dict) and s.get("session_id")]
                for r in rows:
                    r["migrated"] = migrated
                return rows
        except Exception:
            continue
    return []


def save_registry(sessions):
    os.makedirs(os.path.dirname(REGISTRY_FILE), exist_ok=True)
    tmp = REGISTRY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"sessions": sessions[-REGISTRY_KEEP:]}, f,
                  ensure_ascii=False, indent=2)
    os.replace(tmp, REGISTRY_FILE)


TASK_DIR = os.path.join(DATA_DIR, "teleagent-teammate-tasks")


def save_task_prompt(sid, prompt):
    """归档原始任务书：会话死掉后 resume 无需调用方再提供一遍任务上下文。"""
    try:
        os.makedirs(TASK_DIR, exist_ok=True)
        path = os.path.join(TASK_DIR, f"{sid}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(prompt)
        return path
    except OSError:
        return None


def load_task_prompt(sid):
    path = os.path.join(TASK_DIR, f"{sid}.md")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def record_dispatch(sid, title, model, output_file, directory):
    sessions = load_registry()
    # 迁移旧表后第一次写入即完成整体切换：只保留有效行，去掉迁移标记
    for s in sessions:
        s.pop("migrated", None)
    sessions.append({
        "session_id": sid,
        "title": title,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": model,
        "output_file": output_file,
        "directory": directory,
        "state": "dispatched",
    })
    save_registry(sessions)


def update_registry_state(sid, state):
    sessions = load_registry()
    for s in sessions:
        if s.get("session_id") == sid and s.get("state") != state:
            s["state"] = state
            s["state_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
            save_registry(sessions)
            return


def finalize_event(sid, event, code, output_file=None, title=None):
    """统一收尾：发事件、更注册表、写结果文件。"""
    emit(event)
    try:
        update_registry_state(sid, event.get("event"))
    except OSError:
        pass
    if output_file:
        try:
            write_result_file(output_file, event, title=title)
            emit({"event": "result_saved", "path": output_file})
        except OSError as e:
            emit({"event": "result_save_failed", "path": output_file,
                  "detail": str(e)})
    return code


# ---------------------------------------------------------------------------
# FALLBACK_NEEDED：一切非 done 终态结构化上交给调用方智能体（其负总责）
# ---------------------------------------------------------------------------

SCRIPT_PATH = os.path.abspath(__file__)
LAST_RESORT = (
    "若下列动作均不可行，调用方智能体必须自行完成该任务（或改派其他执行通道），"
    "不得让项目因 TeleAgent 停工，也不得要求用户去操作 TeleAgent 界面")

# fallback reason -> 进程退出码（调用方也可只解析末行 JSON 的 reason 字段）
FALLBACK_EXIT = {
    "env_unavailable": 1,
    "timeout": 2,
    "session_aborted": 3,
    "model_fault": 3,
    "permission_required": 4,
    "stalled": 6,
}


def cli_argv(*parts):
    """生成可直接执行的本工具 argv（绝对 python + 绝对脚本路径）。"""
    return [sys.executable, SCRIPT_PATH] + [str(p) for p in parts]


def _held_brief(held):
    """只保留裁决必需字段，避免把授权请求里的杂项内容抛给调用方。"""
    out = []
    for r in held or []:
        out.append({
            "id": r.get("id"),
            "sessionID": r.get("sessionID"),
            "permission": r.get("permission"),
            "patterns": r.get("patterns") or r.get("always"),
            "broad_attempts": r.get("_broad_attempts"),
        })
    return out


def root_cli_parts(args):
    """从 args 提取授权根参数，供 attach/resume 续接时保持同一裁决口径。"""
    parts = []
    for w in getattr(args, "workspace", []) or []:
        parts += ["--workspace", w]
    for s in getattr(args, "source", []) or []:
        parts += ["--source", s]
    for a in getattr(args, "allow", []) or []:
        parts += ["--allow", a]
    if getattr(args, "strict", False):
        parts.append("--strict")
    if getattr(args, "popup", False):
        parts.append("--popup")
    return parts


def make_watcher(sid, args, client=None, log_path=None, tag=None):
    """按 run/attach/resume 公共参数构造值班器（进程内，无子进程壳）。"""
    import types
    here = os.path.dirname(SCRIPT_PATH)
    if here not in sys.path:
        sys.path.insert(0, here)
    import watch as watch_mod
    if not log_path:
        log_path = os.path.join(DATA_DIR, "watch-logs", f"{sid}.jsonl")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    wargs = types.SimpleNamespace(
        sessions=[sid], max=args.timeout, window=args.window,
        interval=getattr(args, "interval", 5),
        tag=tag or sid[:16], log=log_path, popup=bool(args.popup),
        workspace=args.workspace, source=args.source, allow=args.allow,
        strict=args.strict)
    return watch_mod.Watcher(wargs, client=client)


def fallback_event(reason, sid, title, terminal=None, held=None, brief=None,
                   actions=None, blocking=True, note=""):
    """构造 fallback_needed 事件：调用方智能体凭此即可闭环处置。"""
    ev = {
        "event": "fallback_needed",
        "reason": reason,
        "blocking": blocking,
        "session_id": sid,
        "title": title,
        "context": {
            "terminal": terminal,
            "held": _held_brief(held),
            "breakpoint": brief,
        },
        "recommended_actions": actions or [],
        "last_resort": LAST_RESORT,
    }
    if note:
        ev["note"] = note
    return ev


def attach_argv(sid, args, extra=None):
    parts = ["attach", "--session", sid,
             "--timeout", args.timeout, "--window", args.window,
             "--stall-seconds", args.stall_seconds] + root_cli_parts(args)
    if getattr(args, "output_file", None):
        parts += ["--output-file", args.output_file]
    parts += extra or []
    return cli_argv(*parts)


def build_fallback(res, sid, title, args, client):
    """把看守结果映射为 fallback_needed（rc=0 完成返回 None）。"""
    rc, event, held = res["rc"], res["event"], res["held"]
    if rc == 0:
        return None

    if rc == 4:
        actions = []
        for h in held:
            hid = h.get("id")
            actions.append({
                "label": f"确认 patterns 在本任务范围内后放行授权 {hid}",
                "argv": cli_argv("permission", "reply", "--id", hid,
                                 "--reply", "session"),
                "effect": "本会话内放行；随后执行续看动作",
            })
            actions.append({
                "label": f"驳回授权 {hid} 并向会话注入纠偏（路径越界时）",
                "argv": cli_argv("prompt", "--session", sid,
                                 "--text", "你的授权申请越界：只申请本次操作"
                                           "最精确的叶子目录，shell 用安全 cwd "
                                           "+绝对路径，立即继续任务。"),
                "effect": "驳回后由你判断是否续看或重派",
            })
        actions.append({
            "label": "处置授权后续看会话直到终态",
            "argv": attach_argv(sid, args),
            "effect": "attach 复用同一授权根口径继续看守",
        })
        actions.append({
            "label": "中止该会话并带断点自动续派新会话",
            "argv": cli_argv("resume", "--session", sid, "--abort-old",
                             "--timeout", args.timeout,
                             "--window", args.window,
                             "--stall-seconds", args.stall_seconds,
                             *root_cli_parts(args)),
            "effect": "新会话只做断点后的剩余收口，不重做已完成部分",
        })
        return fallback_event("permission_required", sid, title, event,
                              held=held, actions=actions,
                              note="调用方智能体按任务真实边界自行裁决，"
                                   "禁止把用户拉去点 TeleAgent 授权弹窗")

    if rc == 3:
        brief = None
        try:
            brief = breakpoint_brief(client, sid)
        except Exception:
            pass
        model_fault = looks_like_model_fault(event)
        reason = "model_fault" if model_fault else "session_aborted"
        actions = [{
            "label": ("带断点自动续派（换下一可用模型）"
                      if model_fault else "带断点自动续派（同模型）"),
            "argv": cli_argv("resume", "--session", sid,
                             *(("--next-model",) if model_fault else ()),
                             "--timeout", args.timeout, "--window", args.window,
                             "--stall-seconds", args.stall_seconds,
                             *root_cli_parts(args)),
            "effect": "读取归档原任务书+断点轨迹生成接续任务书，新建会话执行",
        }, {
            "label": "查看完整断点消息后人工式（你自己）改写接续任务书再派",
            "argv": cli_argv("messages", "--session", sid, "--last", "8"),
            "effect": "只读排查，不改变会话",
        }]
        return fallback_event(reason, sid, title, event, brief=brief,
                              actions=actions)

    if rc == 6:
        actions = [{
            "label": "向停滞会话注入推进指令后续看",
            "argv": cli_argv("prompt", "--session", sid,
                             "--text", "值班检测到你长时间零进展。若有工具"
                                       "失败请立即换方法继续；若已完成请写"
                                       "结果文件并结束。不要等待。"),
            "effect": "注入后必须紧接着执行 attach 续看",
        }, {
            "label": "续看会话（给停滞注入后的恢复窗）",
            "argv": attach_argv(sid, args),
            "effect": "同一授权口径继续看守",
        }, {
            "label": "仍停滞：中止并带断点续派新会话",
            "argv": cli_argv("resume", "--session", sid, "--abort-old",
                             "--timeout", args.timeout, "--window", args.window,
                             "--stall-seconds", args.stall_seconds,
                             *root_cli_parts(args)),
            "effect": "旧会话终止，新会话断点续做",
        }]
        return fallback_event("stalled", sid, title, event, actions=actions)

    if rc == 2:
        return fallback_event(
            "timeout", sid, title, event, blocking=False,
            actions=[{
                "label": "会话仍在正常执行：续窗看守",
                "argv": attach_argv(sid, args),
                "effect": "再给一个 timeout 窗口；可重复",
            }, {
                "label": "不愿再等：中止并带断点续派",
                "argv": cli_argv("resume", "--session", sid, "--abort-old",
                                 "--timeout", args.timeout,
                                 "--window", args.window,
                                 "--stall-seconds", args.stall_seconds,
                                 *root_cli_parts(args)),
                "effect": "另起会话收口",
            }],
            note="窗口到点不是失败；只要任务还在推进就持续续窗")

    # rc == 1 及其他：环境/连接类硬错误
    actions = [{
        "label": "体检并自动拉起 TeleAgent，随后看能否续看",
        "argv": cli_argv("doctor"),
        "effect": "doctor 会自动探测/拉起桌面应用并验证 API",
    }, {
        "label": "环境恢复后续看原会话",
        "argv": attach_argv(sid, args),
        "effect": "会话可能仍存活",
    }, {
        "label": "会话已死：带断点续派",
        "argv": cli_argv("resume", "--session", sid,
                         "--timeout", args.timeout, "--window", args.window,
                         "--stall-seconds", args.stall_seconds,
                         *root_cli_parts(args)),
        "effect": "新建会话收口",
    }]
    return fallback_event("env_unavailable", sid, title, event,
                          actions=actions)


# ---------------------------------------------------------------------------
# 分发
# ---------------------------------------------------------------------------

def read_prompt(args):
    if args.prompt_file:
        with open(args.prompt_file, encoding="utf-8-sig") as f:
            return f.read()
    if not args.prompt:
        raise SystemExit("错误：必须提供 --prompt 或 --prompt-file")
    return args.prompt


def preflight(args):
    """分发前静态检查（事故前置拦截）。"""
    if args.directory and not os.path.isdir(args.directory):
        raise TeleAgentError(
            f"--directory 不存在：{args.directory}（先创建/核实真实目录名再分发；"
            "错误的启动目录会导致会话所有 shell fork 失败并空转）")
    if args.prompt_file and args.output_file and os.path.abspath(
            args.prompt_file) == os.path.abspath(args.output_file):
        raise TeleAgentError(
            "--output-file 不能与 --prompt-file 同名：收割摘要会静默覆盖任务书")


def create_session(client, title, directory=None):
    body = {"title": title}
    if directory:
        body["directory"] = directory
    _, sess = call_with_retry(client, "POST", "/session", body)
    sid = (sess or {}).get("id")
    if not sid:
        raise TeleAgentError(f"创建会话失败：{sess}")
    return sid


def send_prompt(client, sid, prompt, provider_id, model_id,
                directory=None, agent=None):
    payload = {
        "parts": [{"type": "text", "text": prompt}],
        "model": {"providerID": provider_id, "modelID": model_id},
    }
    if directory:
        payload["directory"] = directory
    if agent:
        payload["agent"] = agent
    return call_with_retry(
        client, "POST", f"/session/{sid}/prompt_async", payload)


def cmd_dispatch(args):
    preflight(args)
    client = TeleAgentClient()
    provider_id, model_id, source = resolve_model(client, args.model)
    prompt = read_prompt(args)
    title = args.title or time.strftime("teammate-%Y%m%d-%H%M%S")

    sid = create_session(client, title, args.directory)
    status, _ = send_prompt(client, sid, prompt, provider_id, model_id,
                            directory=args.directory, agent=args.agent)
    emit({
        "event": "dispatched", "session_id": sid, "title": title,
        "model": f"{provider_id}/{model_id}", "model_source": source,
        "directory": args.directory, "http_status": status})
    try:
        record_dispatch(sid, title, f"{provider_id}/{model_id}",
                        args.output_file, args.directory)
    except OSError as e:
        emit({"event": "registry_write_failed", "detail": str(e)})
    save_task_prompt(sid, prompt)

    if args.confirm_busy:
        deadline = time.time() + args.confirm_busy
        while time.time() < deadline:
            time.sleep(3)
            _, busy = client.call("GET", "/session/status")
            if sid in (busy or {}):
                emit({"event": "busy_confirmed", "session_id": sid})
                break
            info = analyze_session(client, sid)
            if info["state"] in ("done", "error", "aborted"):
                ev, code = terminal_event(sid, info, 0.0)
                return finalize_event(sid, ev, code or 3,
                                      args.output_file, title)
        else:
            emit({"event": "busy_unconfirmed", "session_id": sid,
                  "hint": "分发后未观测到 busy，建议立刻 status/messages 核查"})

    if args.wait:
        return _monitor_to_end(client, sid, args, title=title)
    return 0


def _monitor_to_end(client, sid, args, title=None, output_file=None,
                    held_callback=None):
    """全程监控直到终态/超时。held_callback 见 run 命令（授权处置）。"""
    output_file = output_file or args.output_file
    start = time.time()
    deadline = start + args.timeout
    while True:
        window = min(args.window, max(1, deadline - time.time()))
        rc, event = wait_window(
            client, sid, window, interval=args.interval, quiet=args.quiet)
        # 单窗 elapsed 每窗归零展示；总时长由外层 deadline 控制
        if rc == 2:
            if time.time() >= deadline:
                return finalize_event(sid, {
                    "event": "timeout", "session_id": sid,
                    "elapsed": round(time.time() - start, 1),
                    "hint": "任务仍未完成，可用 harvest --wait 续窗"}, 2,
                    output_file, title)
            continue
        if rc == 0 or rc == 3:
            event["elapsed"] = round(time.time() - start, 1)
            return finalize_event(sid, event, rc, output_file, title)
        return finalize_event(sid, event, 1, output_file, title)


def cmd_monitor(args):
    return _monitor_to_end(TeleAgentClient(), args.session, args)


def resolve_harvest_target(args):
    """按 --session / --title / 最近一次分发解析目标。"""
    sessions = load_registry()
    if args.session:
        for s in sessions:
            if s.get("session_id") == args.session:
                return args.session, s
        return args.session, None
    if args.title:
        matched = [s for s in sessions
                   if args.title in (s.get("title") or "")]
        if matched:
            return matched[-1]["session_id"], matched[-1]
        return None, None
    if sessions:
        return sessions[-1]["session_id"], sessions[-1]
    return None, None


def cmd_harvest(args):
    client = TeleAgentClient()
    sid, entry = resolve_harvest_target(args)
    if not sid:
        raise TeleAgentError(
            f"注册表（{REGISTRY_FILE}）中没有可收割的会话；"
            "用 --session <id> 指定，或先 dispatch 任务")
    title = (entry or {}).get("title")
    output_file = args.output_file or (entry or {}).get("output_file")
    if args.wait:
        start = time.time()
        deadline = start + args.timeout
        while True:
            window = min(args.window, max(1, deadline - time.time()))
            rc, event = wait_window(
                client, sid, window, interval=args.interval, quiet=args.quiet)
            if rc == 2:
                if time.time() >= deadline:
                    return finalize_event(sid, event, 2, output_file, title)
                continue
            if rc in (0, 3, 1):
                return finalize_event(sid, event, rc, output_file, title)
    info = analyze_session(client, sid)
    if info["state"] in ("running", "pending"):
        emit({"event": "running", "session_id": sid, "title": title,
              "messages": info["messages"],
              "tool_calls": len(info["tools"]),
              "hint": "任务仍在执行：harvest --wait 续窗"})
        return 0
    term = terminal_event(sid, info, 0.0)
    if term:
        event, code = term
        return finalize_event(sid, event, code, output_file, title)
    return 0


def cmd_status(args):
    client = TeleAgentClient()
    _, data = call_with_retry(client, "GET", "/session/status")
    if args.session:
        emit({"session_id": args.session,
              "busy": args.session in (data or {}),
              "detail": (data or {}).get(args.session)})
    else:
        emit({"busy_sessions": data or {}})
    return 0


def cmd_messages(args):
    client = TeleAgentClient()
    _, msgs = call_with_retry(
        client, "GET", f"/session/{args.session}/message")
    msgs = msgs if isinstance(msgs, list) else []
    if args.last and len(msgs) > args.last:
        msgs = msgs[-args.last:]
    out = []
    for m in msgs:
        info = m.get("info") or {}
        entry = {"role": info.get("role"), "parts": []}
        for p in m.get("parts") or []:
            t = p.get("type")
            if t == "text":
                entry["parts"].append(
                    {"type": "text", "text": p.get("text")})
            elif t == "tool":
                state = p.get("state") or {}
                entry["parts"].append({
                    "type": "tool", "tool": p.get("tool"),
                    "status": state.get("status"),
                    "input": state.get("input")})
            elif t == "step-finish":
                entry["parts"].append({
                    "type": "step-finish", "reason": p.get("reason"),
                    "tokens": parse_tokens(p.get("tokens"))})
        out.append(entry)
    emit({"session_id": args.session, "messages": out})
    return 0


def cmd_prompt(args):
    """向运行中/已停止的会话追加一条指令（接续、纠偏）。"""
    client = TeleAgentClient()
    text = args.text
    if args.file:
        with open(args.file, encoding="utf-8-sig") as f:
            text = f.read()
    if not text:
        raise SystemExit("错误：必须提供 --text 或 --file")
    pid, mid, _ = resolve_model(client, args.model)
    st, _ = call_with_retry(
        client, "POST", f"/session/{args.session}/prompt_async",
        {"parts": [{"type": "text", "text": text}],
         "model": {"providerID": pid, "modelID": mid}})
    emit({"event": "prompt_sent", "session_id": args.session,
          "http_status": st, "model": f"{pid}/{mid}"})
    return 0


def cmd_permission(args):
    client = TeleAgentClient()
    if args.perm_action == "list":
        _, pending = call_with_retry(client, "GET", "/permission")
        emit({"pending": pending or []})
        return 0
    body = {"reply": args.reply}
    st, data = call_with_retry(
        client, "POST", f"/permission/{args.perm_id}/reply", body)
    emit({"event": "permission_replied", "id": args.perm_id,
          "reply": args.reply, "http_status": st, "detail": data})
    return 0


def cmd_abort(args):
    client = TeleAgentClient()
    status, data = call_with_retry(
        client, "POST", f"/session/{args.session}/abort", {})
    emit({"event": "abort_requested", "session_id": args.session,
          "http_status": status, "detail": data})
    return 0


def cmd_list(args):
    client = TeleAgentClient()
    _, data = call_with_retry(client, "GET", "/session")
    sessions = data if isinstance(data, list) \
        else (data or {}).get("sessions") or []
    out = [{
        "id": s.get("id"),
        "title": s.get("title"),
        "directory": s.get("directory"),
        "time_updated": (s.get("time") or {}).get("updated"),
    } for s in sessions[:args.limit]]
    emit({"count": len(out), "sessions": out})
    return 0


def cmd_delete(args):
    client = TeleAgentClient()
    status, _ = call_with_retry(
        client, "DELETE", f"/session/{args.session}")
    emit({"event": "deleted", "session_id": args.session,
          "http_status": status})
    return 0


def cmd_models(args):
    client = TeleAgentClient()
    cands, small = candidate_models(client)
    emit({
        "small_model": small,
        "candidates": [
            {"provider": p, "model": m, "toolcall": tc, "score": sc}
            for sc, p, m, tc in cands],
        "auto_select": (
            {"provider": next((p for _, p, _, tc in cands if tc), None),
             "model": next((m for _, _, m, tc in cands if tc), None)}
            if cands else None),
    })
    return 0


def cmd_config(args):
    emit({
        "data_dir": DATA_DIR,
        "config_dir": CONFIG_DIR,
        "registry": REGISTRY_FILE,
        "legacy_registry": (LEGACY_REGISTRY_FILE
                            if os.path.exists(LEGACY_REGISTRY_FILE) else None),
        "base_url_env": os.environ.get("TELEAGENT_BASE_URL"),
        "model_env": os.environ.get("TELEAGENT_TEAMMATE_MODEL"),
        "config_file": CONFIG_FILE if os.path.exists(CONFIG_FILE) else None,
        "platform": sys.platform,
        "python": sys.executable,
    })
    return 0


def cmd_doctor(args):
    report = {"ok": False, "platform": sys.platform}
    if IS_WIN:
        procs = [
            {"pid": pid, "name": name}
            for pid, name in list_processes()
            if (name or "").lower() in AGENT_PROCESS_NAMES]
        report["teleagent_processes"] = procs
        if not procs:
            if getattr(args, "autostart", True):
                exe = locate_agent_exe()
                report["agent_exe"] = exe
                try:
                    ensure_agent_running(timeout=60, autostart=True)
                    report["autostarted"] = True
                    procs = [
                        {"pid": pid, "name": name}
                        for pid, name in list_processes()
                        if (name or "").lower() in AGENT_PROCESS_NAMES]
                    report["teleagent_processes"] = procs
                except TeleAgentError as e:
                    report["problem"] = f"自动拉起失败：{e}"
                    report["hint"] = ("人工启动一次 TeleAgent，或设置 "
                                      "TELEAGENT_EXE 指向其可执行文件")
                    emit(report)
                    return 1
            else:
                report["problem"] = "未发现 TeleAgent 进程，应用未运行"
                report["hint"] = "启动 TeleAgent 后重试（不带 --no-autostart 可自动拉起）"
                emit(report)
                return 1
    creds = find_credentials()
    if not creds:
        report["problem"] = "未能获取本地会话密钥（进程 PEB/环境变量均无）"
        report["hint"] = ("Windows：重启 TeleAgent 并用 64 位 Python 运行；"
                          "其他平台：设置 SUPER_AGENT_LOCAL_SESSION_KEY 环境变量")
        emit(report)
        return 1
    report["credentials"] = {
        "found": True, "source_pid": creds["source_pid"],
        "source_process": creds["source_process"],
        "base_url": creds["base_url"]}
    client = TeleAgentClient()
    try:
        status, _ = call_with_retry(
            client, "GET", "/session", connect_timeout=15)
        report["api_auth_ok"] = status == 200
    except TeleAgentError as e:
        report["problem"] = f"API 调用失败：{e}"
        emit(report)
        return 1
    if not report["api_auth_ok"]:
        report["problem"] = "签名认证未通过（401）"
        emit(report)
        return 1
    try:
        p, m, source = resolve_model(client, None)
        report["model"] = f"{p}/{m}"
        report["model_source"] = source
    except TeleAgentError as e:
        report["problem"] = f"模型选择失败：{e}"
        emit(report)
        return 1
    report["ok"] = True
    emit(report)
    return 0


# ---------------------------------------------------------------------------
# run：分发 + 一体化无人值守（带授权自动裁决，详见 watch.Policy）
# ---------------------------------------------------------------------------

def _watch_and_fallback(client, sid, title, args, log_path=None, tag=None):
    """统一看守入口：done 返回 0；非 done 发 fallback_needed 并返回退出码。"""
    watcher = make_watcher(sid, args, client=client, log_path=log_path,
                           tag=tag or (title or sid)[:24])
    res = watcher.run_single(
        sid, timeout=args.timeout, output_file=args.output_file,
        title=title, quiet=args.quiet, stall_seconds=args.stall_seconds)
    if res["rc"] == 0:
        return 0
    fb = build_fallback(res, sid, title, args, client)
    emit(fb)
    return FALLBACK_EXIT.get(fb["reason"], 5)


def cmd_run(args):
    preflight(args)
    client = TeleAgentClient()
    provider_id, model_id, source = resolve_model(client, args.model)
    prompt = read_prompt(args)
    title = args.title or time.strftime("teammate-%Y%m%d-%H%M%S")
    sid = create_session(client, title, args.directory)
    st, _ = send_prompt(client, sid, prompt, provider_id, model_id,
                        directory=args.directory, agent=args.agent)
    emit({"event": "dispatched", "session_id": sid, "title": title,
          "model": f"{provider_id}/{model_id}", "model_source": source,
          "directory": args.directory, "http_status": st, "mode": "run"})
    try:
        record_dispatch(sid, title, f"{provider_id}/{model_id}",
                        args.output_file, args.directory)
    except OSError as e:
        emit({"event": "registry_write_failed", "detail": str(e)})
    save_task_prompt(sid, prompt)
    return _watch_and_fallback(client, sid, title, args,
                               log_path=args.jsonl)


def cmd_attach(args):
    """对已存在的会话执行一体化看守（reply 授权/注入推进后续看）。"""
    client = TeleAgentClient()
    _, entry = (args.session, None)
    for s in load_registry():
        if s.get("session_id") == args.session:
            entry = s
            break
    title = (entry or {}).get("title") or args.session
    if not args.output_file and entry and entry.get("output_file"):
        args.output_file = entry["output_file"]
    return _watch_and_fallback(client, args.session, title, args,
                               log_path=args.jsonl)


def cmd_resume(args):
    """带断点自动续派：读归档原任务书+死会话轨迹，生成接续任务书，新建会话。"""
    client = TeleAgentClient()
    old_sid = args.session
    entry = None
    for s in load_registry():
        if s.get("session_id") == old_sid:
            entry = s
            break
    old_title = (entry or {}).get("title") or old_sid[:16]

    original = None
    if args.prompt_file:
        with open(args.prompt_file, encoding="utf-8-sig") as f:
            original = f.read()
    else:
        original = load_task_prompt(old_sid)
    if not original:
        raise TeleAgentError(
            f"找不到会话 {old_sid} 的归档任务书，请用 --prompt-file 显式提供")

    if args.abort_old:
        try:
            client.call("POST", f"/session/{old_sid}/abort", {}, timeout=15)
            emit({"event": "old_aborted", "session_id": old_sid})
        except TeleAgentError as e:
            emit({"event": "old_abort_skip", "session_id": old_sid,
                  "detail": str(e)[:200]})

    brief = breakpoint_brief(client, old_sid)
    reason = args.reason or "上一会话异常中止或停滞"

    # 模型选择：显式 > --next-model（排除旧模型的下一位）> 自动
    if args.model:
        pid, mid, source = resolve_model(client, args.model)
    elif args.next_model:
        chain = ordered_models(client, None)
        old_model = (entry or {}).get("model")
        alt = [m for m in chain if f"{m[0]}/{m[1]}" != old_model]
        pid, mid, source = (alt or chain)[0]
        source = "failover"
    else:
        pid, mid, source = resolve_model(client, None)

    prompt2 = continuation_prompt(original, brief, reason)
    directory = args.directory or (entry or {}).get("directory")
    if directory and not os.path.isdir(directory):
        emit({"event": "resume_directory_missing", "path": directory,
              "hint": "旧工作目录已不存在，改为不带目录派发"})
        directory = None
    title = args.title or f"{old_title[:30]}-resume"
    new_sid = create_session(client, title, directory)
    st, _ = send_prompt(client, new_sid, prompt2, pid, mid,
                        directory=directory, agent=args.agent)
    save_task_prompt(new_sid, prompt2)

    output_file = args.output_file
    if not output_file and (entry or {}).get("output_file"):
        old_out = entry["output_file"]
        root, ext = os.path.splitext(old_out)
        output_file = root + ".resume" + (ext or ".md")
        args.output_file = output_file
    try:
        record_dispatch(new_sid, title, f"{pid}/{mid}", output_file, directory)
    except OSError:
        pass
    emit({"event": "resumed", "old_session_id": old_sid,
          "session_id": new_sid, "title": title,
          "model": f"{pid}/{mid}", "model_source": source,
          "directory": directory, "breakpoint_messages": brief.get("messages"),
          "http_status": st})
    return _watch_and_fallback(client, new_sid, title, args,
                               log_path=args.jsonl)


def build_parser():
    parser = argparse.ArgumentParser(
        description="TeleAgent 本地分包客户端 teleagent-teammate")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="健康检查（默认自动拉起 TeleAgent）")
    p.add_argument("--no-autostart", dest="autostart",
                   action="store_false", help="只体检，不自动拉起")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("models", help="可用模型与自动选择")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("config", help="打印解析后的配置路径")
    p.set_defaults(func=cmd_config)

    def add_wait_args(p):
        p.add_argument("--timeout", type=int, default=3600,
                       help="总超时秒数，默认 3600")
        p.add_argument("--window", type=int, default=45,
                       help="单次收割窗秒数，默认 45")
        p.add_argument("--interval", type=int, default=5,
                       help="轮询间隔秒数，默认 5")
        p.add_argument("--output-file", help="终态结果写入该 markdown 文件")
        p.add_argument("--quiet", action="store_true",
                       help="抑制 progress 事件")

    def add_watch_args(p):
        """run/attach/resume 共用：看守窗口 + 授权根 + 兜底参数。"""
        add_wait_args(p)
        p.add_argument("--stall-seconds", type=int, default=600,
                       help="忙但消息/工具零增长多少秒判定停滞，默认 600，0=关")
        p.add_argument("--tag")
        p.add_argument("--jsonl", help="值班/授权事件 JSONL 日志路径")
        p.add_argument("--popup", action="store_true",
                       help="启用响铃+置顶弹窗；默认零打扰，异常只走结构化事件")
        p.add_argument("--strict", action="store_true",
                       help="宽申第一次即上交（默认自动驳回+纠偏，两次才上交）")
        p.add_argument("--workspace", action="append", default=[],
                       help="工作区根，可重复；区内非源码路径自动放行")
        p.add_argument("--source", action="append", default=[],
                       help="源码区根，可重复；区内路径不自动放行")
        p.add_argument("--allow", action="append", default=[],
                       help="额外精确放行根（本任务工作目录等），可重复")

    p = sub.add_parser("dispatch", help="分发任务")
    p.add_argument("--prompt")
    p.add_argument("--prompt-file")
    p.add_argument("--title")
    p.add_argument("--directory",
                   help="任务工作目录；涉及文件操作必填，目录必须已存在")
    p.add_argument("--model",
                   help="Provider/Model；缺省自动选择（可用 models 查看）")
    p.add_argument("--agent")
    p.add_argument("--wait", action="store_true")
    p.add_argument("--confirm-busy", type=int, metavar="SECONDS",
                   help="分发后观测忙闲 N 秒，秒死立即报错（建议 12）")
    add_wait_args(p)
    p.add_argument("--connect-timeout", type=int, default=120)
    p.set_defaults(func=cmd_dispatch)

    p = sub.add_parser("run", help="分发 + 一体化无人值守（推荐；异常出 fallback_needed）")
    p.add_argument("--prompt")
    p.add_argument("--prompt-file")
    p.add_argument("--title")
    p.add_argument("--directory")
    p.add_argument("--model")
    p.add_argument("--agent")
    add_watch_args(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("attach", help="对已有会话一体化看守（授权裁决后续看）")
    p.add_argument("--session", required=True)
    add_watch_args(p)
    p.set_defaults(func=cmd_attach)

    p = sub.add_parser("resume", help="中止/死掉会话带断点自动续派新会话")
    p.add_argument("--session", required=True, help="旧会话 ID")
    p.add_argument("--prompt-file", help="原任务书；缺省读归档任务书")
    p.add_argument("--directory", help="覆盖工作目录；缺省沿用旧会话")
    p.add_argument("--model", help="显式 Provider/Model")
    p.add_argument("--next-model", action="store_true",
                   help="自动选择与旧会话不同的下一可用模型（供应商故障转移）")
    p.add_argument("--abort-old", action="store_true",
                   help="续派前先中止旧会话（停滞/卡死时用）")
    p.add_argument("--title")
    p.add_argument("--reason", help="断点原因说明，写入接续任务书")
    p.add_argument("--agent")
    add_watch_args(p)
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("monitor", help="监控已有会话（纯轮询，不带授权裁决）")
    p.add_argument("--session", required=True)
    add_wait_args(p)
    p.set_defaults(func=cmd_monitor)

    p = sub.add_parser("harvest", help="收割结果（恢复入口）")
    p.add_argument("--latest", action="store_true")
    p.add_argument("--session")
    p.add_argument("--title")
    p.add_argument("--wait", action="store_true")
    add_wait_args(p)
    p.set_defaults(func=cmd_harvest)

    p = sub.add_parser("status", help="忙闲查询")
    p.add_argument("--session")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("messages", help="会话消息明细")
    p.add_argument("--session", required=True)
    p.add_argument("--last", type=int)
    p.set_defaults(func=cmd_messages)

    p = sub.add_parser("prompt", help="向会话追加指令")
    p.add_argument("--session", required=True)
    p.add_argument("--text")
    p.add_argument("--file")
    p.add_argument("--model", help="Provider/Model，缺省自动选择")
    p.set_defaults(func=cmd_prompt)

    p = sub.add_parser("permission", help="授权队列处置")
    psub = p.add_subparsers(dest="perm_action", required=True)
    pl = psub.add_parser("list")
    pl.set_defaults(func=cmd_permission)
    pr = psub.add_parser("reply")
    pr.add_argument("--id", dest="perm_id", required=True)
    pr.add_argument("--reply", required=True,
                    choices=["once", "session", "always", "reject"])
    pr.set_defaults(func=cmd_permission)

    p = sub.add_parser("abort", help="中止会话当前任务")
    p.add_argument("--session", required=True)
    p.set_defaults(func=cmd_abort)

    p = sub.add_parser("list", help="列出最近会话")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("delete", help="删除会话")
    p.add_argument("--session", required=True)
    p.set_defaults(func=cmd_delete)

    return parser


def main():
    args = build_parser().parse_args()
    try:
        sys.exit(args.func(args) or 0)
    except TeleAgentError as e:
        emit({"event": "error", "detail": str(e)})
        sys.exit(1)


if __name__ == "__main__":
    main()
