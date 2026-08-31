# 计划任务：谁在什么时候跑什么

Windows 任务计划程序里一共 **6 个**任务：1 个核心推送 + 5 个维护。
`Get-ScheduledTask -TaskName Discord*` 可以一次看全。

| 任务名 | 频率 | 跑什么 | 花 AI 钱 | 产出去哪 |
|---|---|---|---|---|
| `DiscordDigestCycle` | **每 15 分钟** | `cycle.py --once` | 是（haiku，便宜） | **Discord 推送** |
| `DiscordBacktestDaily` | 每天 **23:30** | `backtest.py --backfill` | 否 | `signals.db` |
| `DiscordSelfReviewDaily` | 每天 **23:45** | `selfreview.py` | 是（sonnet） | 文件 + **Discord TL;DR** + playbook |
| `DiscordWeeklyReview` | 每周日 **20:00** | `review.py --days 7` | 是（sonnet） | 文件 + **Discord TL;DR** |
| `DiscordOptimizeWeekly` | 每周日 **21:00** | `optimize.py` | 是（**opus**） | 决策记录 + **Discord** + 参数/方法库 |
| `DiscordCleanupWeekly` | 每周日 **22:00** | `cleanup.py --apply` | 否 | 删文件、轮转日志 |

除此之外还有一条**没有定时任务的自动流程**：新的 Substack 复盘 PDF 落地时，
`cycle.py` 会检测到并在后台跑一遍「周期复盘 + 自我优化 + VIP 确认」，
见 [自我优化](optimization.md)。`DiscordOptimizeWeekly` 只是万一某周没更新时的兜底。

注册/卸载：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1              # 核心推送
powershell -ExecutionPolicy Bypass -File scripts\register_maintenance_tasks.ps1 # 4 个维护
powershell -ExecutionPolicy Bypass -File scripts\register_maintenance_tasks.ps1 -Remove
```

---

## 1. `DiscordDigestCycle` —— 每 15 分钟的脉搏推送

这是你每天真正在看的东西。一轮做五件事：

1. `watch_inbox` 把浏览器新导出的聊天去重合并、图片转写，落到 `data/chats_by_date/`；
2. 切出最近 45 分钟的新消息（**窗口内没有新消息就直接跳过**，不调 AI、不推送，省钱也不刷屏）；
3. 算「和上一轮相比变了什么」（见 [delta-pulse.md](delta-pulse.md)）；
4. 生成中文简报 + 抽标的打分出信号卡；
5. 拼成一条消息推到你的 Discord 频道，并把这轮推了什么**记进 `pulse_rounds` 表**。

第 5 步的记忆是下一轮"只讲增量"的依据，也是每日自我复盘的材料之一。

**为什么它有时候不发消息**：窗口内没新消息（正常）、浏览器导出停摆（会单独告警）、
或者出错（也会单独告警）。详见 [troubleshooting.md](troubleshooting.md)。

## 2. `DiscordBacktestDaily` —— 每天 23:30 回填结果

把历史信号拉出来，用行情算 T+1 / T+3 / T+5 之后到底涨了还是跌了，写进 `outcomes` 表。
**不调 AI，不花钱**，只用行情数据。

它回答的是"我们的信号到底准不准"。注意样本要按「票 × 天」去重——
cycle 每 15 分钟给同一只票记一条，不去重会把同一个信号重复计 96 次，
统计出来的胜率是假的。详见 [backtest.md](backtest.md)。

**排在 23:45 之前是故意的**：这样自我复盘能看到当天信号的兑现结果。

## 3. `DiscordSelfReviewDaily` —— 每天 23:45 自我复盘 ⭐

**这是整个系统里唯一会让自己变强的部分**，下面单独展开。

## 4. `DiscordWeeklyReview` —— 每周日 20:00 周期复盘

看的是**外部**：过去 7 天群里谁值得听、什么方法赚钱、信号引擎该怎么调参。
和每日自我复盘的分工见 [self-review.md](self-review.md) 开头的对比表。

## 5. `DiscordOptimizeWeekly` —— 每周日 21:00 自我优化 ⭐

用 **opus** 按胜率统计调阈值、增删方法库，并把「改了什么、为什么、
**否决了哪些替代方案**」写成决策记录。详见 [optimization.md](optimization.md)。

排在周复盘之后，这样它能读到当周的复盘产出。
**新 Substack 复盘落地时会自动触发同一条流水线**，这个定时任务是兜底。

## 6. `DiscordCleanupWeekly` —— 每周日 22:00 磁盘清理

按分级保留策略删旧文件、轮转日志。**永不删**：聊天语料、`signals.db`、
花过钱的图片转写结果、watcher 去重状态。平时可以先 `python src\cleanup.py`
（默认试运行）看看会删什么。

---

# 每天的复盘存在哪、发到哪

一次自我复盘产生 **4 处输出**，各有各的用途：

| 输出 | 位置 | 你会不会看到 | 说明 |
|---|---|---|---|
| **TL;DR** | **Discord 频道** | ✅ 每天看到 | 3~5 行：今天最值得记住的判断、我们最大的短板、明天盯什么 |
| 完整报告 | `trade_notes/reviews/<日期>-daily.md` | 想看时翻 | 五段完整分析，约 7 KB |
| 方法条目 | `prompts/playbook.md` | 间接看到 | **自动写入**，影响之后每一条简报 |
| VIP 建议 | `prompts/vip_suggestions.md` | 需要你确认 | 只提建议，**不自动改**正式名单 |

推送用的是**同一个** `DISCORD_WEBHOOK_URL`，也就是脉搏推送的那个频道，
所以你不用多开一个地方看。消息长这样：

```
🪞 每日自我复盘 · 2026-08-27
今天 Frank 做了三件事：在半导体反弹时大幅止盈、维持现金 50% 以上、
明确说"随时准备大仓位做空半导体，但 not now，这里到支撑了"。
我们最大的短板：没有输出任何简报，错过了帮用户在反弹中保护利润的窗口。
明天重点盯 Warsh 演讲和 NVDA 财报后的价格反应。
完整报告见 trade_notes/reviews/20260827-daily.md
```

`trade_notes/reviews/` 和 `vip_suggestions.md` 都在 `.gitignore` 里
（可再生的产出，不是源码）。

---

# 自我变强的过程，具体是怎么回事

## 一句话

**每天晚上让 AI 批评我们自己白天写的简报，把学到的方法写进一个文件，
第二天每一轮简报都会读这个文件。**

## 闭环图

```
        ┌──────────────────────────────────────────┐
        │                                          │
   每 15 分钟                                       │
   pulse 产出简报 ──► 推送 Discord                   │
        │                                          │
        │ 存进 pulse_rounds 表                      │
        ▼                                          │
   当天价格/消息验证（backtest 23:30 回填）           │
        │                                          │
        ▼                                          │
   selfreview 23:45 ──► 找差距、提炼方法             │
        │                                          │
        └──► prompts/playbook.md ──────────────────┘
                （下一轮 pulse 读它）
```

关键在于这个环是**闭合**的：playbook 不是写给人看的笔记，它会真的被塞进
下一轮的 prompt 里。

## 四个步骤

### 第一步：留下证据

`cycle.py` 每次推送后调 `remember_round()`，把**实际推给你的那段正文**
存进 `store.pulse_rounds` 表。

这一步看着不起眼，但它是整件事的前提。没有它，复盘时模型只能看到
"今天群里聊了什么"，只能复述新闻；有了它，模型才能对照
**"我们当时是怎么说的" vs "事后看本该怎么说"**。

### 第二步：拿六份材料做对照

`selfreview.py` 把六样东西一起喂给 sonnet：

| # | 材料 | 作用 |
|---|---|---|
| 1 | 今天群里的完整聊天 | 谁说了什么、**怎么推理的** |
| 2 | **今天我们推过的所有简报** | 被审视的对象 |
| 3 | 今天的信号 + 历史兑现统计 | 我们说对了没有 |
| 4 | **Frank 的 Substack 复盘** | 方法论标杆 |
| 5 | 当前 playbook | 避免重复沉淀已有条目 |
| 6 | 当前 VIP 名单 | 判断该增该减 |

材料 4 是"参照系"。Frank 的复盘示范了四件我们容易偷懒的事：
**多层因果链**（每层都是可验证的传导机制）、**关键位交叉确认**（多个来源
聚在同一区间才算数）、**情景路径**（列 2~3 条路径而不是赌一个方向）、
**明确的执行参数**（建仓/加仓/止损/目标，四个数字缺一不可）。

没有参照系，模型只会说"今天分析得不错"。有了它，才会说
"我们只给了结论没给传导机制"。

### 第三步：产出可执行的方法，而不是感想

报告第二段「可以沉淀的方法」有硬性要求：写成
**"遇到 X 情况 → 检查 Y → 得出 Z"** 这种能照做的形式，不能写成感想。

首次实跑（复盘 08-27）产出的三条：

- 业务本质质疑 > 技术面反弹：核心问题没在财报里解决时，反弹是止盈机会不是追高信号。
- 分批止盈 + 保留仓位 + 准备回补：到统计位置（如 2 标准差）trim 1/3，回调加回来。
- 风险事件前 24 小时降杠杆是纪律：央行演讲/关键财报前一天主动提高现金比例。

prompt 里还写了"确实没有新东西就写'今天没有新方法'"——**不硬凑**。
否则 playbook 会被灌满听起来很对但没用的话，反而稀释了有用的条目。

### 第四步：写回 playbook，下一轮立刻生效

`update_playbook()` 把这些条目插进 `prompts/playbook.md` 的标记区间：

```markdown
<!-- BEGIN AUTO -->
- 最新学到的（权重最高）
- ...
<!-- END AUTO -->
```

规则：

- **新的插最前面**——`pulse` 只取前 8 条，所以最近学到的权重最高。
- 上限 20 条，挤出去的落到「## 已淘汰」，保留是为了记住"试过但没用"。
- 去重按**去掉标点后**的文本比对，同一条方法不会因措辞略有不同被记两遍
  （已验证：重复应用同一份报告，条目数 11 → 11 不变）。
- 只有 `BEGIN/END AUTO` 之间的会被注入——文件头部说明里也有 bullet，不能混进去。

然后 `pulse.summarize()` 每轮把前 8 条塞进 prompt 的「分析方法指引」段。

## 它真的起作用了吗

起作用了，而且能直接看出来。08-27 的复盘学到"风险事件前 24 小时降杠杆是纪律"
这条，写进 playbook 之后，下一次生成的简报里出现了这句：

> Frank 在明天 Warsh 演讲前执行了风险管理：大面积止盈一波，把现金比例提到 50% 以上。
> **这是他之前反复强调的"风险事件前 24 小时降杠杆"纪律的实际操作。**

模型主动把当下的行为**归类到了昨天学到的模式**上——这正是我们要的：
不是每天从零开始理解群里在干嘛，而是带着积累的框架去看。

同一次复盘里，它还自己指出了一个我们没意识到的真实缺口：

> 我们的信号系统只做了"发射"，没有做"验证闭环"。
> 这导致用户不知道该信号是否还有效。

## 你能怎么干预

这个环是自动的，但**不是黑箱**，三个地方你随时可以插手：

| 想做什么 | 怎么做 |
|---|---|
| 某条方法你觉得没用 | 直接编辑 `prompts/playbook.md` 删掉，或挪到「## 已淘汰」 |
| 想强调某条 | 把它挪到 `BEGIN AUTO` 后面第一行（前 8 条才会被用） |
| 加个人进重点发言人 | 看 `prompts/vip_suggestions.md` 的建议，认可了再手动写进 `vip_speakers.txt` |

改完**下一轮简报立刻生效**，不用重启任何东西。

VIP 名单为什么不自动改：它决定 pulse 会重点关注谁，影响很大，
所以刻意保留了人工闸门。

## 手动跑

```powershell
python src\selfreview.py                  # 复盘今天（UTC）
python src\selfreview.py --yesterday      # 复盘昨天
python src\selfreview.py --day 20260827   # 复盘指定某天
python src\selfreview.py --no-post --no-apply  # 只出报告，不推送、不改 playbook
python src\selfreview.py --no-api         # 只打印 prompt，不花钱
```

## 成本

每天一次 sonnet 调用，输入 3~6 万字，输出上限 8000 token。
比每 15 分钟一次的 haiku 脉搏便宜得多，量级上可以忽略。

---

## 相关

- [self-review.md](self-review.md) —— 自我复盘的实现细节、PDF 解析的坑
- [delta-pulse.md](delta-pulse.md) —— playbook 怎么进入每轮简报
- [backtest.md](backtest.md) —— 兑现统计怎么算
- [troubleshooting.md](troubleshooting.md) —— 任务没跑 / 没收到消息怎么查
