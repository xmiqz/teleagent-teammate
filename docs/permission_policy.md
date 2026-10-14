# 授权策略（policy.py）

TeleAgent 的工程师在访问会话声明目录之外的路径时会产生 external_directory
授权请求，本技能在看守窗口内每 5 秒轮询一次 `/permission`，只处理**属于
本值班会话**的请求（其他会话/操作者的请求绝不碰）。

## 根参数

- `--workspace DIR`（可重复）：工作区根。归一化后，区内、且与源码区无
  覆盖关系的路径自动放行。
- `--source DIR`（可重复）：源码主检出。请求路径在其下，或请求路径
  包住源码区，一律不自动放行（防止工程师借并行 worktree 名义改主检出）。
- `--allow DIR`（可重复）：精确放行根，优先级最高。即使该目录物理上
  位于 source 区内（如本任务 worktree）也放行。
- 自动工具链根：TeleAgent 数据/配置目录、系统临时目录、通用程序安装
  目录等，仅在文件系统上**真实存在**时加入；不内置任何个人路径。
- `--strict`：宽申第一次即上交（默认给一次自动纠偏机会）。

## 三级裁决

对请求的全部 patterns 取交集语义（任一越界即整条不放行）：

1. **session** —— 全部 patterns 位于白名单（allow > workspace 判定；
   source 覆盖一票否决）。自动回复 `session`（仅本会话有效），
   工程师零等待。落 PERMISSION_AUTO 事件。
2. **broad** —— 非严格模式下，请求根是任一已知安全根的**严格祖先**
   （盘符根、用户主目录、工作区父目录等宽申）。自动 `reject` 并
   `prompt_async` 注入纠偏指令（要求只申请最精确叶子目录、shell 用
   安全 cwd+绝对路径）。同 sid 同 patterns 第 2 次出现 → 升级 rc4。
3. **manual** —— 真越界（其他项目、系统目录、真实 UNC 共享）、
   非 external_directory 类授权、patterns 无法解析 → 立即 rc4 上交，
   附 held 明细与可直接执行的 reply/reject argv。

## 路径归一化规则

- Windows 容错 `/` 与 `\`；尾部 `*` 通配剥除后比较（`D:\wt\*` 等同
  `D:\wt`）；盘符根 `C:\` 的尾分隔符保留。
- 覆盖关系用 `os.path.commonpath` 判定，相等算"在区内"。
- 主机名不存在的裸通配 UNC（`\*`、`\tmp\*`）实际打不开，视同安全，
  避免无意义卡单。
- 永不使用 `always` 回复（永不过期授权违背最小权限）。

## 给调用方智能体的设根建议

- 并行多 worktree：每个 worktree 同时给 `--workspace` 与 `--allow`，
  主检出给 `--source`。
- 证据/产物目录在 worktree 外（如系统临时目录）时单独 `--allow`
  精确到该子目录，不要给 TEMP 根以上。
- 吃不准边界就收紧：越界会收到 rc4，你可在秒级 reply 放行；
  放太宽则没有后悔机制。
