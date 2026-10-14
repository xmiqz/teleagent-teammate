# teleagent-teammate

把编码子任务分包给本机 [TeleAgent](https://www.teleagent.ai) 智能体，并让
**调用方智能体**（发分包的 AI agent）对分包全程负总责的无人值守技能：

- `run` 一条命令完成派单 + 看守 + 授权裁决 + 收割，输出单行 JSON 事件流；
- 安全路径授权自动放行，越界/宽申按三级策略处置，**全程不需要人碰
  TeleAgent 界面，默认零弹窗零响铃**；
- 一切非完成态收敛为末行 `fallback_needed` 事件，携带可直接执行的
  处置命令 argv（续窗 / 断点续派 / 供应商故障转移 / 授权裁决）；
- **进程退出即唤醒**：完成/异常都在一个轮询 tick 内收口退出；批次升级
  默认以 rc3 退出上交（不常驻空等），调用方用 `--resolve` argv 处置后
  即断点续跑；忙闲表残留有双确认兜底，不会"工程师做完了还在干等"；
- 多工程师批次 playbook（并行派单 → 产物校验 → shell 验证 → 通知），
  崩溃断点续跑、进程守卫、开机自启；
- 纯 Python 标准库、零三方依赖；Windows 为主，POSIX 尽力兼容。

## 工作模型

调用方智能体是组长，TeleAgent 会话是工程师，本技能是值班台：
脚本只做白名单内无副作用的自愈（重连/拉起、密钥轮换重试、安全授权
放行、宽申纠偏、批次内单次重派），其余异常全部结构化上交，由调用方
智能体决策、处置、必要时自己接手——**项目绝不分包后停工**。

```
调用方智能体 ──子进程 JSON 事件──▶ teammate.py run ──本地签名 HTTP──▶ TeleAgent
                     ▲ fallback_needed（含可执行 argv）│
                     └────────────────────────────────┘
```

## 前置条件

1. 已安装并登录 TeleAgent 桌面应用（本地 API 默认 `127.0.0.1:4397`；
   应用没运行时 `doctor/run` 会自动拉起）。
2. Python 3.9+（直接用 TeleAgent 自带运行时即可，无需 pip install）。
3. TeleAgent 中至少有一个**凭据就绪**的模型供应商（`models` 命令可查；
   市场目录里的供应商需要先配置 API key 才会入选）。

## 快速开始

```bash
# 体检（自动拉起 TeleAgent、验证 API、选择模型）
python scripts/teammate.py doctor
python scripts/teammate.py models

# 派一个只读小任务并全程看守
python scripts/teammate.py run \
  --title "py-count" --prompt-file task.md \
  --directory /path/to/project --workspace /path/to/project \
  --allow /tmp/tt --window 240 --stall-seconds 300 \
  --output-file /tmp/tt/harvest.md
```

退出码 `0=完成 2=窗口到点(非失败) 3=会话中止/模型故障 4=需裁决授权
6=停滞 1=环境不可用`；末行非 0 必为 `fallback_needed`，直接执行其中
`recommended_actions[].argv`。完整操作规范见 [SKILL.md](SKILL.md)，
协议细节见 [docs/](docs/)，端到端示例见 [examples/](examples/)。

## 测试

零联网冒烟（内置假 TeleAgent 服务，16 个场景）：

```bash
python tests/smoke_test.py
```

## 安装为智能体技能

本仓库本身就是一个技能目录。把整个目录放到目标 agent 的技能目录下，
目录名必须为 `teleagent-teammate`：

- TRAE CN 版：`%USERPROFILE%\.trae-cn\skills\teleagent-teammate`
- TRAE 国际版：`%USERPROFILE%\.trae\skills\teleagent-teammate`
- 其他读取同类技能目录的 agent：其配置的 skills 目录下

PowerShell 一键安装（git）：

```powershell
git clone https://github.com/xmiqz/teleagent-teammate.git `
  "$env:USERPROFILE\.trae-cn\skills\teleagent-teammate"
```

无 git 时下载 zip 解压到上述目录即可。安装后让 agent 读一遍 SKILL.md。

### 给其他智能体的「网页 + 提示词」安装方式

把下面这段提示词连同本仓库 GitHub 地址发给任意支持本地技能的智能体：

```
打开 https://github.com/xmiqz/teleagent-teammate 阅读 README 与
SKILL.md，然后把该仓库（保持目录名 teleagent-teammate）安装到我的
技能目录（Windows TRAE CN 版为 %USERPROFILE%\.trae-cn\skills\，
国际版为 %USERPROFILE%\.trae\skills\；其他平台放对应 skills 目录）。
要求：只使用 Python 标准库；安装后运行 python tests/smoke_test.py
自检并回报结果；不要修改脚本内容，不要上传任何本机路径、密钥或任务数据。
```

卸载：删除技能目录；若安装过开机自启，先运行
`python scripts/guard_loop.py --state-dir <批次目录> --uninstall-startup`。

## 安全与隐私

- 本地 HMAC 会话密钥只存在于内存（Windows 从进程环境块读取），
  不打印、不日志、不落盘；`/config/providers` 含供应商明文 key，
  本技能任何命令都不输出该端点原文。
- 授权最小化：自动放行只覆盖任务根与通用工具链目录，永不回复 always；
  盘符/主目录等宽申默认驳回并纠偏。
- 批次红线：push / tag / 远程写永不进入 playbook，批次止于本地产物就绪。
- 仓库内不含任何个人路径、账号或任务数据；欢迎在提交前用
  `grep -rEi 'sk-[a-z0-9]{10,}|api[_-]?key.*=.*[a-z0-9]{12,}' .` 自查。

## 许可证

[MIT](LICENSE)
