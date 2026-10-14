# 最小示例：一次只读分包

场景：让 TeleAgent 工程师统计某目录下的 Python 文件数，把结果写入临时目录，
全程不需要任何人碰 TeleAgent 界面。

## 1. 任务书 `task.md`

```
只读测试任务：禁止修改本文件之外的任何文件，禁止 git commit/push。
统计 <项目目录> 下的 .py 文件数量，把结果一行写入
<TEMP目录>/py_count.txt（内容形如 py_files=N），然后回复 COUNT_DONE。
```

## 2. 派单并一体化看守

Windows（用 TeleAgent 自带 python，零安装）：

```
%USERPROFILE%\.local\share\TeleAgent\runtimes\python\python.exe scripts\teammate.py run ^
  --title "py-count" ^
  --prompt-file task.md ^
  --directory <项目目录> ^
  --workspace <项目目录> ^
  --allow <TEMP目录> ^
  --window 240 --stall-seconds 300 ^
  --output-file <TEMP目录>/harvest.md
```

POSIX：

```
python3 scripts/teammate.py run --title py-count --prompt-file task.md \
  --directory /path/to/project --workspace /path/to/project \
  --allow /tmp/tt --window 240 --stall-seconds 300 \
  --output-file /tmp/tt/harvest.md
```

## 3. 预期事件流

1. `dispatched`（带 session_id，自动选择凭据就绪的模型）；
2. 工程师若申请 `<TEMP目录>\*` 授权，窗口内自动 `PERMISSION_AUTO`，
   会话不中断；
3. `done`（rc=0）并写 harvest.md；读 `<TEMP目录>/py_count.txt` 验收。

异常时末行为 `fallback_needed`：直接执行事件中
`recommended_actions[0].argv`（续窗/续派/换模型/裁决授权）。
