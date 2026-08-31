# 每日自我复盘：让分析越用越强

## 和每周复盘的区别

| | [`review.py`](../src/review.py)（每周） | [`selfreview.py`](../src/selfreview.py)（每天） |
|---|---|---|
| 看的对象 | **对外**：群里谁有价值、信号引擎该怎么调参 | **对内**：我们自己写的简报差在哪 |
| 核心问题 | "这周群里发生了什么" | "今天我们哪里分析得不够好，明天怎么改" |
| 产出去向 | 复盘报告 + VIP 建议 | 复盘报告 + **改写 playbook** + VIP 建议 |
| 是否影响下一轮 pulse | 否 | **是**（这是闭环的关键） |

## 闭环长这样

```
pulse 产出简报 ──► 当天价格/消息验证 ──► selfreview 找差距
      ▲                                          │
      └────── playbook.md（分析方法库）◄──────────┘
```

`pulse.summarize()` 每轮把 playbook 前 8 条注入 prompt 当作分析指引。
所以 playbook 变强 = 明天的每一条简报都变强。这是整个系统里唯一会自己
迭代的部分。

## 喂给模型的六份材料

| # | 材料 | 来源 | 作用 |
|---|---|---|---|
| 1 | 今天群里的完整聊天 | `data/chats_by_date/<日>/*/merged.enriched.txt` | 谁说了什么、怎么推理的 |
| 2 | **今天我们推过的所有简报** | `store.pulse_rounds` | 被审视的对象 |
| 3 | 今天的信号 + 历史兑现 | `signals` 表 + `backtest.report_text()` | 我们说对了没有 |
| 4 | **Frank 的 Substack 复盘** | `data/substack_post/*.pdf` | 方法论标杆 |
| 5 | 当前 playbook | `prompts/playbook.md` | 避免重复沉淀已有条目 |
| 6 | 当前 VIP 名单 | `prompts/vip_speakers.txt` | 判断该增该减 |

材料 2 是关键。没有它，模型只能复述今天发生了什么；有了它，模型能对照
"我们当时是怎么说的"和"事后看本该怎么说"。

## 为什么要学 Frank 的复盘

他的写法示范了一条完整的分析链路，正是我们容易偷懒的地方：

1. **多层因果链**——「第一层…第七层」，每层都是可验证的传导机制，
   而不是"因为 A 所以 B"。例：压住长端收益率 → 外国买家回报不足 → 美元必须
   贬值补偿 → 黄金三要素共振。
2. **关键位交叉确认**——0DTE Put Support 7700 / 0DTE HVL 7705 / 全期限 HVL 7715
   / Gamma Wall 7725 聚在同一区间，这个区间才叫"分水岭"。单一来源的位置不算数。
3. **情景路径**——路径一/二/三 + 各自触发信号 + 对应仓位，而不是赌一个方向。
4. **明确的执行参数**——建仓、加仓线、止损、目标，四个数字缺一不可。

## PDF 解析的坑

[`src/substack.py`](../src/substack.py) 用 PyMuPDF 取文字层，但**导出方式不同，
结果天差地别**：某些导出方式产生的 PDF 字体没有 ToUnicode 映射，取出来的文本
会**丢掉所有英文和数字**——「10Y 收 4.736%」变成「收」，价位、代码全没了，
喂给 AI 只会得到幻觉。

所以 `extract()` 会做质量检查：英文数字占比低于 5% 直接报错，宁可跳过也不喂残缺文本。

```powershell
python src\substack.py            # 解析全部 PDF 并报告质量
python src\substack.py --force    # 忽略缓存重新解析
```

正常输出长这样（占比 20%+ 才健康）：

```
✅ 20260824.pdf：9305 字，英文数字占比 24.8% → 20260824.txt
```

报错的话，换一种方式导出 PDF（浏览器"打印为 PDF"通常没问题）。
抽出的文本缓存成同名 `.txt`，不必每次重新解析。

## 报告结构

| 段落 | 内容 | 去向 |
|---|---|---|
| TL;DR | 最值得记住的判断 + 我们最大的短板 + 明天盯什么 | **推送 Discord** |
| 一、谁说对了，以及他是怎么得出来的 | 只写**能验证**的：说了什么 / 推理路径 / 结果 / 能否复用 | 报告 |
| 二、可以沉淀的方法 | 1~4 条可执行的判断动作 | **写回 playbook.md** |
| 三、我们分析的不足 | 对照材料 2 和材料 4 找差距，具体到某条简报 | 报告 |
| 四、明天重点关注 | 带**具体验证条件**的跟进项 | 报告 |
| 五、VIP 名单建议 | 加谁 / 减谁 / 为什么 | `vip_suggestions.md` |

第一段强调"只写能验证的，没有就写'今天没有可验证的观点'"——不硬凑，
否则 playbook 会被灌满听起来很对但没用的话。

## playbook.md 的维护规则

```markdown
<!-- BEGIN AUTO -->
- 条目一
- 条目二
<!-- END AUTO -->
```

- 只有 `BEGIN AUTO`/`END AUTO` 之间的条目会被注入 prompt。文件头部的使用说明
  里也有 bullet，不能混进去。
- 新条目插在**最前面**（pulse 只取前 8 条，所以最近学到的权重最高）。
- 上限 `PLAYBOOK_MAX`(20)；挤出去的落到「## 已淘汰」，保留是为了记住"试过但没用"。
- **近义去重**：模型很容易把同一条方法换个说法再写一遍（实测出现过"分批止盈的具体操作"
  和"分批止盈 + 保留仓位 + 准备回补"两条并存）。精确匹配挡不住，所以用
  `difflib` 相似度，超过 `PLAYBOOK_SIMILAR`(0.45) 就当重复丢弃。
  阈值这么定是因为实测同义条目相似度 **0.59**、真正不同的条目最高才 **0.05**，中间空档很大。
- 已经攒出来的重复可以随时清理：`python src\selfreview.py --dedupe`（保留靠前那条）。
- **可以手工编辑**。觉得某条没用就删掉或挪到已淘汰，下一轮 pulse 立刻生效。

## VIP 名单为什么不自动改

`vip_speakers.txt` 决定 pulse 会重点关注谁的发言，影响很大。所以复盘只把建议
写到 `prompts/vip_suggestions.md`，**人工确认后再改正式名单**。这是刻意保留的人工闸门。

## 用法

```powershell
python src\selfreview.py                    # 复盘今天（UTC）
python src\selfreview.py --yesterday        # 复盘昨天
python src\selfreview.py --day 20260827     # 复盘指定某天
python src\selfreview.py --no-post --no-apply   # 只出报告，不推送、不改 playbook
python src\selfreview.py --no-api           # 只打印 prompt，不花钱
python src\selfreview.py --dedupe           # 只清理 playbook 近义重复，不跑复盘
```

计划任务 `DiscordSelfReviewDaily` 每天 **23:45** 跑一次——排在 23:30 的
`DiscordBacktestDaily` 之后，这样复盘能看到当天信号的兑现结果。
所有定时任务的全貌见 [计划任务总览](scheduled-tasks.md)。

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_maintenance_tasks.ps1
```

## 产出文件

| 路径 | 内容 |
|---|---|
| `trade_notes/reviews/<日期>-daily.md` | 完整报告 |
| `prompts/playbook.md` | 自动追加的方法条目 |
| `prompts/vip_suggestions.md` | VIP 名单建议（待人工确认） |

## 成本

一天一次 sonnet 调用，输入约 3~6 万字（聊天 + 简报 + Substack），
输出 8000 token 上限。比每 15 分钟一次的 haiku 脉搏便宜得多。

## 相关

- [delta-pulse.md](delta-pulse.md) —— playbook 怎么进入每轮简报
- [review.md](review.md) —— 每周复盘
- [backtest.md](backtest.md) —— 兑现统计从哪来
