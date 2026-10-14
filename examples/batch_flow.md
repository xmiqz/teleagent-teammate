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

180s 巡检一次：批次进程死了/僵尸了按 `orch/EX/launch.json` 无损重拉，
批次进入终态后守卫自退。开机自启（零管理员）：

```
python scripts/guard_loop.py --state-dir orch/EX --install-startup
python scripts/guard_loop.py --state-dir orch/EX --uninstall-startup
```

## 4. 升级与裁决（调用方智能体通道，不找用户）

阶段超时/验证不过/shell 失败 → state=escalated，批次暂停等你：

```
python scripts/batch_ctl.py status --state-dir orch/EX
python scripts/batch_ctl.py resolve retry --state-dir orch/EX --note "环境抖动，重跑"
python scripts/batch_ctl.py resolve skip  --state-dir orch/EX --note "该步骤本批次豁免"
python scripts/batch_ctl.py resolve abort --state-dir orch/EX --note "放弃批次"
```

`retry/skip` 仅在 escalated 时生效；批次进程不存活时 status 会提示
先重启 batch.py（guard 也会自动重拉）。

## 5. 红线复核（启动前逐条）

- playbook 无 push/tag/远程写；merge/rebase 步骤 retries 被强制为 1。
- shell cwd 用证据目录/系统临时目录；可执行文件全部绝对路径。
- 工程师任务书含禁 commit/push 与唯一 worktree+基线 SHA。
