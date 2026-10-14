# fallback_needed 协议

所有非 done 终态都由 `run/attach/resume` 的末行事件上交，调用方智能体
凭事件即可闭环处置，不需要用户、不需要 TeleAgent 界面。

## 事件结构

```json
{
  "event": "fallback_needed",
  "reason": "session_aborted | model_fault | permission_required | stalled | timeout | env_unavailable",
  "blocking": true,
  "session_id": "ses_...",
  "title": "任务标题",
  "context": {
    "terminal": { "event": "...", "elapsed": 0, "detail": {} },
    "held": [ { "id": "per_...", "sessionID": "ses_...", "permission": "external_directory",
                "patterns": ["..."], "broad_attempts": null } ],
    "breakpoint": { "messages": 3, "tail": [ {"role": "assistant", "type": "model_error"} ] }
  },
  "recommended_actions": [
    { "label": "人类可读说明", "argv": ["python 绝对路径", "teammate.py 绝对路径", "..."],
      "effect": "执行后会发生什么" }
  ],
  "last_resort": "若下列动作均不可行，调用方智能体必须自行完成该任务（或改派其他执行通道），不得让项目因 TeleAgent 停工，也不得要求用户去操作 TeleAgent 界面"
}
```

进程退出码与 reason 对应：

| reason | rc | blocking | 处置顺序 |
|--------|----|----------|----------|
| env_unavailable | 1 | true | doctor 自愈 → attach；会话已死则 resume |
| timeout | 2 | false | 还在推进 → attach 续窗（可重复）；不愿等 → abort+resume |
| session_aborted | 3 | true | messages 看断点 → resume 同模型；现场损坏则重写任务书 run |
| model_fault | 3 | true | resume --next-model（首条 argv 已带）；连续失败不无限换 |
| permission_required | 4 | true | 你裁决 held.patterns：界内 reply session 后 attach；越界 reject+注入纠偏后 attach |
| stalled | 6 | true | prompt 推进 + attach；仍停滞 abort+resume |

## 处置纪律

1. **先执行 argv，不要自己拼命令**：argv 携带正确的 python/脚本绝对路径、
   会话 id 和授权根参数（--workspace/--source/--allow/--strict/--popup）。
2. 只解析末行 JSON；中间的 progress 事件用于观测，不作为分支依据。
3. timeout 的 blocking=false：不得当作失败重派（会丢现场）。
4. permission 只用 `once/session/reject`，**永不回复 always**。
5. 同一问题连续两次自动处置无效，必须升级你自己的策略（改写任务书/
   换执行通道/自己完成），而不是机械第三次重试。
6. 任何情况下不把用户引导到 TeleAgent GUI 点按钮。

## 脚本自愈白名单（不产生 fallback 的自动动作）

- 连接拒绝退避重连；401 密钥轮换自动重试一次；TeleAgent 未运行自动拉起。
- 安全授权 session 自动放行（PERMISSION_AUTO）。
- 宽申首次自动 reject + 注入精确化纠偏（同 patterns 第 2 次才上交）。
- 授权 reply 返回 404（请求已被覆盖/消费）视为成功，不报错。
- 批次内会话 abort 自动重派 1 次并自动模型故障转移。

除此之外的一切异常（真越界授权、供应商错误、停滞、现场损坏、阶段失败、
环境长期不可用）都必须结构化上交，脚本不替调用方做危险决策。
