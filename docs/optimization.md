> 返回 [文档索引](./README.md) · [项目 README](../README.md)

# 自我优化：系统怎么改进自己

## 和每日自我复盘的分工

| | [`selfreview.py`](../src/selfreview.py)（每天，sonnet） | [`optimize.py`](../src/optimize.py)（每周，**opus**） |
|---|---|---|
| 改的是 | **分析方法**：今天该怎么看盘 | **系统本身**：阈值、信号权重、方法库排序 |
| 输入 | 聊天 + 我们的简报 + Substack | **胜率统计** + 方法库 + 参数 + 历次复盘 |
| 典型产出 | "利好不涨比利空下跌更强" | "「量不足突破」是反向信号，删掉重复条目腾位置" |
| 为什么用贵模型 | — | 要权衡取舍，还要论证**为什么不选别的做法** |

一周一次 opus，成本可以忽略，但判断质量直接决定系统会不会**越调越差**。

---

## 安全设计（这是会自己改自己的系统，护栏比功能重要）

| 护栏 | 做法 |
|---|---|
| **碰不到源码** | AI 只能写 `data/tunables.json`，永远不修改 `.py` 文件 |
| **越界即夹住** | 每个参数在 `config.TUNABLES` 里有上下界，提议 `IV_HIGH=99` 会被夹到 2.0 并标注 |
| **未知参数拒绝** | 不在注册表里的名字直接跳过并记一行"⚠ 跳过 X：不是可调参数" |
| **一键回滚** | 应用前自动快照 → `python src\optimize.py --revert` |
| **整体复原** | 删掉 `data/tunables.json` 就回到出厂默认 |
| **样本量纪律** | prompt 明令 `n < 15` 的信号不许用来调参，只能写进「继续观察」 |
| **全程留痕** | 每次改动写 `trade_notes/optimizations/<日期>-optimization.md` |

`data/tunables.json` 只是**覆盖层**：`config.py` 先定义出厂默认，再把这个 JSON 叠上去。
所以「当前值 vs 默认值」永远看得见：

```powershell
python src\optimize.py --show
```

```
当前可调参数：
  EXTENSION_PCT_MAX      = 3.0      [1.0, 10.0]
  BREAKOUT_STALE_DAYS    = 2        [1, 10]
  EARNINGS_STALE_DAYS    = 2        [1, 30]     ← 已调整（默认 3）
  ...
调整历史：
  2026-08-31  EARNINGS_STALE_DAYS: 3 → 2
```

### 哪些能调、哪些不能

能调的就是 `config.TUNABLES` 里那 7 个阈值（新鲜度、期权、回报倍数）。
**不能调的**：信号是否启用、档位规则、推送逻辑——这些要改代码，
AI 会在报告里写进「本次没做的事」并说明理由，由你决定要不要动手。

想让某个参数彻底不可被 AI 调整，把它从 `TUNABLES` 里删掉即可。

---

## 决策记录：这才是重点

用户不会逐行审代码，只看决策记录。所以 prompt 强制每条改动写四件事：

```
### PARAM: 参数名
- 现值 / 建议
- 依据: 必须引用具体数字（信号名、n、胜率、平均收益），不许写"感觉偏松"
- 预期效果: 用可观察的现象描述
- 否决的替代方案: 至少一条考虑过但没选的做法 + 为什么没选
- 风险: 可能的副作用，以及怎么发现它
```

报告还有两段专门用来暴露思考过程：

- **继续观察**——样本不够、暂时不动的，写明「还差多少样本」。
- **本次没做的事**——**主动决定不做**的改动及原因。
  这段和改动本身同样重要：你需要能区分"它想过但否决了"和"它根本没想到"。

首次实跑（2026-08-31）就体现了价值——它提出 **0 项参数调整**：

> 我审视了所有参数，没有发现可靠依据支持任何调整。
> `EARNINGS_STALE_DAYS = 3`：财报 beat T+1 胜率 66.7%，T+3 跌到 35.7%（n=28）……
> 当前参数值刚好是 3 天，已经对齐了这个衰减窗口。改成 2 天会误伤仍有效的 T+1 信号。

同时它在「本次没做的事」里点出了自己的能力边界：

> 没有把「财报 beat」改成只推 T+1：这是信号逻辑层面的改动，超出参数调整范围。

---

## 方法库调整

除了参数，它还能重排 [playbook](self-review.md)：

| 操作 | 作用 |
|---|---|
| `add` | 新增一条方法（会做近义去重） |
| `remove` | 删掉过时或与他条重复的 |
| `promote` | 把已有条目挪到最前面 |

`promote` 存在的理由：pulse **只注入前 8 条**，排在第 12 位的方法等于没写。
所以"提权"和"新增"一样重要。

---

## VIP 名单：怎么确认，怎么自动化

复盘只把建议写进 `prompts/vip_suggestions.md`，[`vips.py`](../src/vips.py) 负责落地：

```powershell
python src\vips.py            # 看当前名单 + 待确认建议 + 各自攒了几期
python src\vips.py --apply    # 全部采纳
python src\vips.py --auto     # 只采纳「连续 2 期都被建议」的
python src\vips.py --add 张三 --remove 李四
python src\vips.py --undo     # 撤回上一次改动
```

**为什么自动模式要求连续 2 期**：单期建议往往来自一两句话的印象，
而这份名单决定 pulse 重点关注谁，影响很大。两期都提说明是稳定观察，不是偶然。
计数存在 `data/vip_state.json`，落地后清零；本期没再提的自动清零。

每次改动都会备份到 `data/vip_speakers.bak`，`--undo` 原样还原。

---

## 新 Substack 复盘 → 自动重新校准

Frank 每周一更新。那是我们能拿到的最高质量分析范式，所以**新文件一落地就重新校准**，
不用干等周日的定时任务。

[`substack_pipeline.py`](../src/substack_pipeline.py) 按顺序跑四步（后一步用前一步的产出）：

```
新 PDF 落地
   │
   ├─ 1. 解析 PDF ──── 质量不合格 → 中止 + 告警（绝不拿残缺文本改系统）
   ├─ 2. review.py ─── 周期复盘 → 刷新 reviews/ 和 vip_suggestions.md
   ├─ 3. optimize.py ─ 自我优化（opus）→ 调参数 + 改方法库 + 写决策记录
   ├─ 4. vips.py --auto ─ 落地连续 2 期的名单改动
   └─ 5. 标记已处理 ── 全部成功才标；失败下轮自动重试
```

**触发方式**：`cycle.py` 每 15 分钟顺手检查一次（只是列目录 + 读个小 JSON，开销可忽略），
发现新文件就 **spawn 一个后台进程**跑流水线——因为它要几分钟，
而 15 分钟的脉搏推送绝不能为它等着。

```powershell
python src\substack.py --check-new        # 看有哪些还没触发过
python src\substack_pipeline.py           # 手动跑（有新文件才动）
python src\substack_pipeline.py --force   # 强制跑一遍
python src\substack.py --mark-all-seen    # 把现有 PDF 全标为已处理
```

日志在 `data/pipeline.log`。

**为什么"检测"和"标记"分开**：如果解析或复盘中途失败了，PDF 不会被标记，
下一轮还会重试。要是检测时就标记，一次网络抖动就会让一期内容被永久漏掉。

---

## 兜底的定时任务

万一某周 Substack 没更新，`DiscordOptimizeWeekly`（周日 21:00）还是会跑一次优化。
排在周复盘（20:00）之后，这样它能读到当周的复盘产出。
完整任务列表见 [计划任务总览](scheduled-tasks.md)。

---

## 你要做的事

平时**什么都不用做**。每周会收到两条推送：

1. 🔧 **系统自我优化** —— 摘要 + 本次实际生效的改动
2. 👥 **重点发言人名单已更新**（只有名单真变了才发）

觉得哪条改得不对：

| 想撤销什么 | 命令 |
|---|---|
| 参数改动 | `python src\optimize.py --revert` |
| 全部参数回到出厂默认 | 删掉 `data/tunables.json` |
| VIP 名单改动 | `python src\vips.py --undo` |
| 某条方法 | 直接编辑 `prompts/playbook.md` |

完整理由（含它否决了哪些替代方案）在 `trade_notes/optimizations/`。

---

## 相关

- [每日自我复盘](self-review.md) —— 方法库怎么积累
- [回测统计](backtest.md) —— 优化依据的胜率数据从哪来
- [计划任务总览](scheduled-tasks.md) —— 什么时候跑什么
