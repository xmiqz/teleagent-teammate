# 架构说明

## 组件

```
调用方智能体（负总责）
        │  子进程调用，单行 JSON 事件流
        ▼
teammate.py   CLI 核心：派单/看守/续派/授权/收割/注册表/自动拉起
        │
        ├─ watch.py      多会话值班循环（窗口内授权轮询 + 停滞检测）
        ├─ policy.py     三级授权裁决（路径归一，无个人路径内置）
        ├─ procutil.py   pid 判活/进程树杀/命名互斥量（跨平台）
        ├─ batch.py      playbook 状态机（并行派单 → shell → notify）
        ├─ batch_ctl.py  升级裁决通道（commands.jsonl）
        ├─ guard.py      单轮巡检（死/僵尸判定，先杀后拉）
        └─ guard_loop.py 循环巡检 + 开机自启管理（VBS/cron/systemd 指引）
                │  HMAC-SHA256 签名的本地 HTTP（默认 127.0.0.1:4397）
                ▼
        TeleAgent 桌面应用 + 远端模型供应商
```

## 关键机制

### 凭据与密钥

- Windows 下会话密钥 `SUPER_AGENT_LOCAL_SESSION_KEY` 从 TeleAgent 进程
  环境块（PEB）读取；POSIX 走环境变量。密钥只在内存，不打印、不日志、不落盘。
- 密钥轮换/401：客户端自动重试一次；连接拒绝指数退避；应用未运行时
  doctor/run 按常见安装位置自动拉起并等待 API 就绪。
- 模型就绪判定：`/config/providers` 的市场目录不等于可用。只有
  provider entry 带 `options`（config 内置凭据）或其声明的 env 变量名
  存在于本机/TeleAgent 进程环境中，才算凭据就绪；env 只采集变量名，
  绝不采集值。

### 看守循环（watch.py）

- 收割窗：`wait_window` 每 interval 秒 analyze（忙闲 + 消息 + 工具状态）。
- **授权裁决与停滞检测通过 on_tick 回调在窗口内每轮执行**。早期实现
  只在两窗之间轮询一次授权，长窗口（如 300s）中途出现的授权申请会把
  工程师一直挂住——这是必须保持的设计约束，回退会复发该事故。
- 宽申计数按 (sid, patterns) 记忆，并从 JSONL 历史恢复，重挂不清零。
- 停滞签名 = (state, messages 数, tools 数)；签名零增长超过
  stall-seconds 即 rc6。

### 断点续派（resume）

- 每次 dispatch 的原始任务书归档到数据目录 tasks/<sid>.md。
- resume 读旧会话消息生成断点摘要（含模型错误信息），拼出"严禁重做
  已完成部分"的接续任务书，新会话执行；支持 `--abort-old` 与
  `--next-model`（从候选链排除旧模型）。

### 批次（batch.py）

- 状态机：state.json 原子写（tmp+replace），sid/model 持久化。
- dispatch_group 并行派单、按窗收割；agent abort 自动重派 1 次
  （自动模型故障转移）；shell retries 用于环境抖动，merge/rebase
  强制 1 次（冲突不可靠重试解决）；API 抖动 catch-all 退避 60s。
- 单实例：Windows 命名互斥量 `Local\TeleagentTeammateBatch_<state-dir 基名>`。
- 升级不弹窗：state=escalated + 结构化事件；默认静默，--popup 才重弹。
- guard 用 `GetExitCodeProcess == STILL_ACTIVE(259)` 判活，不用
  `os.kill(pid, 0)`（在部分 Windows Python 运行时上会对刚退出进程的
  僵尸句柄误报存活）。心跳 > heartbeat-min 先杀进程树后按 launch.json 重拉。

## 零依赖与可移植性

- 全部脚本仅用 Python 标准库，Windows 为主、POSIX 尽力兼容
  （POSIX 下 PEB 读密钥不可用，改走环境变量；开机自启打印
  cron/systemd 配置指引）。
- 所有 CLI 输出单行 JSON（除 markdown 结果文件），方便调用方解析。
