---
name: teleagent-teammate
description: 把编码子任务分包给本机 TeleAgent 智能体并全程无人值守，调用方智能体对分包负总责，自动裁决安全授权、异常结构化兜底、批次编排与进程保活
---

# teleagent-teammate

面向**调用方智能体**（发分包的那个 agent）的 TeleAgent 分包技能。你把一个
编码/运维子任务派给本机 TeleAgent 会话，本技能负责派单、看守、授权裁决、
断点续派、批次编排与保活；**你对分包结果负总责**。

## 0. 第一原则（每次调用都适用）

1. **默认入口永远是 `run`**（dispatch + 一体化看守）。裸 `dispatch` 只用于
   "只派不看"的特殊场景。
2. **脚本永不弹窗等人**。默认零弹窗、零响铃；一切非完成态以末行
   `fallback_needed` JSON 事件上交给你。用户给的 `--popup` 才允许桌面提醒。
3. **你负总责，不是用户、不是 TeleAgent 界面**。收到 fallback_needed 后
   按第 3 节决策表**立即执行**事件中给出的 `recommended_actions[].argv`
   （每条都是可直接运行的绝对路径命令），或自己完成该任务。禁止把用户
   拉去点 TeleAgent 的授权弹窗或排查界面。
4. **项目不得因 TeleAgent 停工**。环境挂了 doctor 自愈拉起；会话死了
   resume 断点续派或换模型；所有自动路径都走不通时，你自己接手完成任务。
5. 脚本的自动动作只在白名单内（连接退避/自动拉起、401 密钥轮换重试、
   安全授权 session 放行、宽申首次 reject+纠偏、abort 自动重派 1 次）。
   其余一律结构化上交，脚本不替你做危险决策。
6. **同轮有界阻塞是唯一可靠的收口方式**（2026-10-14 双会话真机实测）。
   后台任务完成通知**不会开启你的新一轮对话**——实测两个后台看守 rc0 退出
   后无人唤醒、空等 100 秒以上，通知只在下一条用户消息时才被一并送达。
   因此工作在途时**绝不能结束当前回合**，正确做法是"短窗口前台调用、同轮
   立即续"：
   - 单会话：`run`/`attach` 前台阻塞；rc2（窗口到点，非失败）→ **同一轮**
     立即原样再发一次 `attach --session <sid>`，可重复，直到 rc0/3/4/6。
   - 批次：批次进程后台启动后，**同一轮**立刻循环前台调用
     `batch_ctl.py wait --state-dir <dir> --timeout 420`：rc2 立即再 wait，
     rc3 同一轮执行末行 `--resolve` argv 后再 wait，rc0 才继续收割。
   - 严禁：裸 dispatch 后结束回合、把看守/批次丢后台就结束回合、sleep 轮询
     status、让用户"等好了叫你"。派给 Task 子智能体时，任务描述必须写明
     "rc2 同轮续窗/再 wait，工作在途不得结束回合"。
7. **批次升级默认即退**。batch.py 阶段失败时默认输出末行 fallback_needed
   后以 rc3 退出（escalation 同一次迭代内退出，零额外等待），guard 不会
   重拉 escalated 批次。你在同轮 wait 循环中拿到 rc3 后，执行事件 argv
   （`--resolve retry|skip|abort|redispatch:<key>`）即完成裁决并断点续跑，
   然后重新进入 wait 循环。

## 1. 派单前检查点（30 秒，必做）

1. `python scripts/teammate.py doctor` —— 探活、自动拉起 TeleAgent、
   验证本地 API 与模型选择。`"ok": true` 才继续。
2. `python scripts/teammate.py models` —— 看自动选中的模型。只有
   **凭据就绪**的供应商会入选（config 内置或所需 API key 环境变量存在）；
   市场目录里未配置的供应商不选，否则会话 5 秒内以
   `create chat model: provider not found` 死亡。
3. `--directory` 必须是**已真实存在**的目录。git worktree 场景先
   `git worktree list` 核实（分支名不等于目录名，目录不存在会让会话
   所有 shell fork 失败并空转）。
4. 任务书用 `--prompt-file` 传文件，不要塞进命令行。`--output-file`
   不得与 prompt-file 同名（脚本会拦截）。收割摘要 md 不要与工程师
   自己要写的产物文件同路径。
5. 授权根参数按任务真实边界给（见第 4 节），不要图省事给盘符根。

## 2. 标准用法

Windows 上 python 用 TeleAgent 自带运行时（免装依赖，本技能零三方库）：
`%USERPROFILE%\.local\share\TeleAgent\runtimes\python\python.exe`。
其他平台用任意 python3。

单次分包（推荐）：

```
python scripts/teammate.py run ^
  --title "任务标题" ^
  --prompt-file 任务书.md ^
  --directory D:\worktrees\TASK-12 ^
  --workspace D:\worktrees\TASK-12 ^
  --allow D:\worktrees\TASK-12 ^
  --window 240 --stall-seconds 300 ^
  --output-file evidence\TASK-12_result.md
```

- 所有输出都是**单行 JSON 事件**；只解析末行判断终态。
- 进程退出码即终态语义（下表），末行 fallback_needed 带处置动作。
- 会话卡死/被杀后跨终端找回：`harvest --latest` 或直接对注册表中的 sid
  执行 `attach --session <sid>`。
- 多会话一个值班进程：用 `scripts/watch.py <sid1> <sid2> ...`。

退出码：

| rc | 含义 | 你要做什么 |
|----|------|-----------|
| 0 | done，产物就绪 | 读 answer / output-file，进入你的收割/验证 |
| 2 | timeout（窗口到点，非失败，blocking=false） | **同一轮**立即执行事件给的 attach 续窗；可重复，禁止结束本轮等通知 |
| 3 | session_aborted / model_fault | 执行 resume argv（model_fault 自带 --next-model） |
| 4 | permission_required | 按第 4 节裁决后执行 reply argv，再 attach |
| 6 | stalled | 先 prompt 注入推进 + attach；仍停滞则 abort+resume |
| 1 | env_unavailable | doctor 自愈后 attach；会话已死则 resume |

补充：完成判据是"忙闲表已清 + 末消息 step-finish=stop"；忙闲表清除
滞后时连续两次相同 stop 签名也按 done 收口（done 事件带
`busy_map_lag: true`），单次瞬态掉表不会误判 abort（连续两次同签名
才判 rc3）。批次进程（batch.py）的 rc3 语义不同：表示批次 escalated
等待你裁决续跑，见第 6 节。

## 3. fallback_needed 决策表（收到末行就照做）

事件结构：`reason`、`blocking`、`context.terminal/held/breakpoint`、
`recommended_actions[]`（label/argv/effect）、`last_resort`。

- **timeout（rc2）**：不是失败。会话 busy 且签名在增长 → **同一轮**立即
  attach 续窗，不要重派（重派会丢现场），也不要结束本轮"等它叫你"
  （后台完成不会开启新轮次，见第 0 节铁律 6）。
- **model_fault（rc3）**：供应商创建模型失败/限流/key 失效。第一条 action
  是 `resume --next-model`（自动换凭据就绪的下一供应商）。连续两家都挂 →
  你判断是否降级任务复杂度或自己接手，不要无限换模型。
- **session_aborted（rc3）**：先执行第二条 action `messages --last 8`
  看断点；现场完好 → resume 同模型续派（第一条 action）；现场损坏 →
  你重写任务书后重新 run。
- **permission_required（rc4）**：你是裁决者。看 `context.held[].patterns`：
  在本任务真实边界内 → 执行 `permission reply --id ... --reply session`
  然后 attach；越界（盘符/主目录/其他项目）→ reject 并注入纠偏文案
  （事件里有现成 argv），再 attach。**禁止回复 always**（永不过期的授权）。
- **stalled（rc6）**：先 prompt 推进 + attach；仍停滞 → abort 旧会话 +
  resume 断点续派。停滞常见原因就是授权等待，本技能默认已在窗口内自动
  轮询裁决；held 出现时按 rc4 处理。
- **env_unavailable（rc1）**：doctor（自动拉起 TeleAgent 桌面应用并等
  API 就绪）→ attach；密钥轮换由客户端 401 自动重试，无需你介入。

所有 argv 都是绝对 python + 绝对脚本路径，直接执行即可。跨轮次处理时
务必带上原来的 `--workspace/--source/--allow/--strict/--popup`
（事件 argv 已自动携带）。

## 4. 授权根与三级裁决（policy）

授权裁决在**每个收割窗内部每轮轮询执行**，不是窗口之间，所以会话中途
申请授权不会把工程师挂住。参数语义：

- `--workspace DIR`（可重复）：工作区根。区内非源码路径自动 `session` 放行。
- `--source DIR`（可重复）：源码区根。与区内路径有任何覆盖关系都不自动放行。
- `--allow DIR`（可重复）：精确放行根，优先级最高（本任务 worktree 即使
  物理位于某个 source 内也放行）。
- 工具链根（TeleAgent 数据目录、系统 TEMP、通用安装目录等）由脚本
  按环境变量/通用位置自动探测加入白名单，**不内置任何个人路径**。

三级裁决（只用 once/session/reject，永不使用 always）：

1. **session**：patterns 全部位于白名单内（支持 `目录\*` 通配归一）→
   自动回复 session，会话零等待，落 `PERMISSION_AUTO` 事件。
2. **broad**：申请根是已知安全根的严格祖先（如盘符根、主目录、工作区父
   目录）→ 自动 reject 并向会话注入精确化纠偏指令；同 sid 同 patterns
   第 2 次才 fallback 上交。`--strict` 下第一次即上交。
3. **manual**：真出界/危险路径/非 external_directory 授权 → 立即 rc4
   上交，脚本绝不替你放行。

请求的 `patterns` 同时含安全和越界路径时按越界处理（一票否决）。

## 5. 命令速查

| 命令 | 用途 |
|------|------|
| doctor [--no-autostart] | 体检、自动拉起、验证 API/模型 |
| models | 凭据就绪的候选模型与 auto_select |
| config | 数据目录/注册表/配置路径（不打印任何密钥） |
| run | 派单+一体化看守（默认入口），出 fallback_needed |
| dispatch [--wait] [--confirm-busy 12] | 只派单；--wait 单窗等待 |
| attach --session SID | 对已有会话一体化看守（授权+续看） |
| resume --session SID [--abort-old] [--next-model] | 带归档任务书与断点轨迹续派新会话 |
| monitor --session SID | 纯轮询观察（不做授权裁决） |
| harvest [--latest/--session/--title] [--wait] | 收割/找回结果 |
| status [--session SID] | 忙闲 |
| messages --session SID [--last N] | 断点消息明细 |
| prompt --session SID (--text/--file) | 追加指令（纠偏/推进/接续） |
| permission list / reply --id ID --reply R | 授权队列查看与裁决 |
| abort --session SID | 中止当前任务 |
| list [--limit N] / delete --session SID | 会话列表/删除 |

通用看守参数：`--timeout`（默认 3600）、`--window`（默认 45）、
`--interval`（默认 5）、`--stall-seconds`（默认 600，0 关）、
`--quiet`、`--jsonl PATH`、`--tag`、`--popup`、`--strict`。

隐私与密钥：本地 HMAC 会话密钥只在内存（Windows 从 TeleAgent 进程
PEB 读取，不落盘、不进日志）；重启轮换后自动重发现。`/config/providers`
响应含供应商明文凭据，本技能任何命令都不打印该端点原文，你也不要
在任务书/日志里转存。

## 6. 多工程师批次（playbook）

适合 N 个工程师并行 + 本地收口（合并检查/回归/构建）的无人值守批次。

```
python scripts/batch.py --playbook playbook.json --state-dir orch\T1
```

**收口范式（必须遵守，见第 0 节铁律 6）**：批次进程后台启动后，立刻在
**同一轮**前台进入有界等待循环——这是唯一可靠收口；后台任务完成不会
开启你的新一轮：

```
python scripts/batch_ctl.py wait --state-dir orch\T1 --timeout 420
```

rc0 终态（complete/aborted，末行 wait_terminal）→ 收割；rc2 到点仍 running
→ 同一轮立即再发一次 wait（可重复）；rc3 escalated（末行带
recommended_actions）→ 同一轮执行其中 argv 裁决续跑，再 wait。
playbook 要点（完整字段见 assets/playbook.example.json）：

- `roots.workspace/source/allow/strict`：全批次统一授权口径。
- 阶段类型按序执行：`dispatch_group`（并行派 N 个工程师；每 agent 支持
  `verify.files_exist/files_contain/files_match` 产物校验，正则容错）→
  `shell`（步骤支持 `stdout_contains/stdout_empty/timeout/retries`，
  输出与重试落 logs；merge/rebase 强制 retries=1，冲突必须上交）→
  `notify`。
- 断点：每步落 state.json，崩溃重启从断点继续，sid 持久化不重复派单；
  会话 abort 自动重派 1 次（自动模型故障转移）；API 抖动 catch-all
  退避 60s 不崩。
- 升级：阶段失败/超时/验证不过/两次中止 → state=escalated。**默认
  escalate-exit**：批次在升级的同一次迭代内输出末行 `fallback_needed`
  （含可直接执行的 `recommended_actions`）后以 **rc3 退出**（零额外等待，
  非崩溃），你的同轮 wait 循环随即拿到 rc3；场外处置执行事件 argv 即裁决
  +断点续跑，然后重新进入 wait 循环：
  - `... batch.py --playbook pb.json --state-dir orch\T1 --resolve retry`
    （只重试验证/等待，不重复派单）
  - `... --resolve skip` / `... --resolve abort`
  - `... --resolve redispatch:<agent_key>`（先自行 abort 旧 sid；作废
    sid 重新派单，保留故障模型规避）
  需要旧语义（升级后常驻等 batch_ctl，配合 --popup/人工在场）加
  `--escalate-wait`；该标志会写入 launch.json 随守卫重拉保留。
  状态查询（进程在/不在都能用）：
  `python scripts/batch_ctl.py status --state-dir orch\T1`
- Windows 命名互斥量保证每个 state-dir 硬单实例。

保活（长时间批次跨进程重启）：

- `python scripts/guard.py --state-dir orch\T1` 单轮巡检
  （GetExitCodeProcess 防僵尸误判；心跳超时先杀后拉，按 launch.json
  无损重拉）。rc：0 健康/批次终态/**escalated 等待调用方（不重拉）**，
  10 已重拉，11 僵尸，12 重拉失败。守卫绝不在 escalated 状态重拉批次
  （那会与你的 --resolve 续跑撞互斥量并形成空转）；你续跑后心跳恢复，
  监控自动接续。
- `python scripts/guard_loop.py --state-dir orch\T1`
  循环巡检（默认 180s，pythonw 静默常驻，批次终态自退；命名互斥量
  单实例）。开机自启：`--install-startup`（在用户 Startup 文件夹
  生成 VBS，零管理员；卸载 `--uninstall-startup`）。

批次红线（评审 playbook 时逐条核对）：

- **push / tag / 远程写操作永不入 playbook**；批次止于本地产物就绪。
- 不引新依赖、不真实联网、不碰真实账号数据。
- shell 的 cwd 用 TeleAgent 数据目录或系统 TEMP，不要用含特殊字符/
  权限敏感的路径；全部可执行文件用绝对路径。

## 7. 派给 TeleAgent 工程师的任务书规范

复制 assets/team_prompt_template.md 填空。核心：唯一 worktree+基线
SHA、文件域互斥、三凭证（python/runner/证据目录）、禁 commit/push、
产物写死路径与完成标记、收割由你独立复跑不信任自报、abort 接续规程。

## 8. 自检与排障

- 零三方依赖冒烟（内置假 TeleAgent 服务，不联网）：
  `python tests/smoke_test.py`，应输出 16 个场景全过。
- 改任何 scripts 后必须重跑冒烟；真实链路用只读小任务验证
  （数文件、写 TEMP 标记文件），不要用真实推送测试。
- 常见现象：dispatch 返回 204 但会话秒死 → 先看 messages 末条
  info.error；5 秒空 assistant + finish=error 是模型/供应商问题
  （models 复查就绪供应商）；busy 不增长先查 permission list；
  TeleAgent 没运行任何命令都能自愈拉起，doctor 是唯一手动入口。

## 目录

scripts/  teammate.py（CLI 核心）、watch.py（值班）、policy.py（授权）、
batch.py（编排）、batch_ctl.py（人工裁决通道）、guard.py/guard_loop.py
（保活/自启）、procutil.py（进程判活，禁用 os.kill(pid,0) 误判）。
assets/ 任务书模板与 playbook 样例；examples/ 端到端示例；docs/ 协议细节。
