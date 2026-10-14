# 批次示例：两个工程师并行 + 本地验证

对照 `assets/playbook.example.json`。本示例演示从派单到本地产物就绪
（批次红线：不 push、不 tag）。

## 1. 准备

1. 两个隔离 worktree 已存在（不存在先 `git worktree add`；playbook 的
   `directory` 必须真实存在）。
2. 两份任务书已写到证据目录，产物路径与完成标记写死（见
   `assets/team_prompt_template.md`）。
3. 改 playbook 中的全部占位路径；`roots` 给全批次统一授权口径。

## 2. 启动批次

```
python scripts/batch.py --playbook playbook.json --state-dir orch/EX
```

- stdout 与 `orch/EX/logs/batch.jsonl` 是单行 JSON 事件流；
- 每阶段落 `orch/EX/state.json`，进程崩溃/机器重启后**用同一条命令重启**
  即从断点继续（已派 sid 持久化，不重复派单；abort 自动重派 1 次并
  自动模型故障转移）；
- Windows 下每个 state-dir 有命名互斥量，重复启动直接退出，不会双开。

## 3. 无人值守保活

```
python scripts/guard_loop.py --state-dir orch/EX
```

180s 巡检一次：批次进程死了/僵尸了按 `orch/EX/launch.json` 无损重拉
（escalated 驻停态除外——那是在等调用方 `--resolve`，重拉只会空转），
批次进入终态后守卫自退。开机自启（零管理员）：

```
python scripts/guard_loop.py --state-dir orch/EX --install-startup
python scripts/guard_loop.py --state-dir orch/EX --uninstall-startup
```

## 4. 升级与裁决（调用方智能体通道，不找用户）

阶段超时/验证不过/shell 失败/会话两次中止 → state=escalated。默认
**escalate-exit**：批次输出末行 `fallback_needed` 后以 rc3 退出，用进程
退出唤醒阻塞等待中的你（你本来就该在同一次工具调用里阻塞等它）。按末行
`recommended_actions[].argv` 场外处置后，用 `--resolve` 重拉即裁决+断点
续跑（sid 已持久化，不重复派单）：

```
# 只重试验证/等待（不重复派单）
python scripts/batch.py --playbook playbook.json --state-dir orch/EX --resolve retry
# 跳过当前阶段 / 终止批次
python scripts/batch.py --playbook playbook.json --state-dir orch/EX --resolve skip
python scripts/batch.py --playbook playbook.json --state-dir orch/EX --resolve abort
# 会话两次中止/停滞：先 teammate.py abort 旧 sid，再作废 sid 重新派单
python scripts/batch.py --playbook playbook.json --state-dir orch/EX --resolve redispatch:<agent_key>
```

随时可查状态（进程不存活时返回码 2，JSON 照读）：

```
python scripts/batch_ctl.py status --state-dir orch/EX
```

需要旧语义（升级后进程常驻、经 batch_ctl 指令通道裁决，配合 --popup/
人工在场）时启动加 `--escalate-wait`；该标志写入 launch.json，守卫重拉
会保留。无论哪种模式，guard 都**不会**重拉 escalated 状态的批次。

## 5. 红线复核（启动前逐条）

- playbook 无 push/tag/远程写；merge/rebase 步骤 retries 被强制为 1。
- shell cwd 用证据目录/系统临时目录；可执行文件全部绝对路径。
- 工程师任务书含禁 commit/push 与唯一 worktree+基线 SHA。
