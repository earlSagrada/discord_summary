# Discord 交易群聊天记录整理流程

把 Discord 频道的当日聊天导出成纯文本（保留引用关系和图片内容），
再交给 Claude 做话题聚类和黑话注释。

```
Discord 网页
   │  discord-digest-exporter.user.js（浏览器里跑，读渲染后的 DOM）
   ▼
discord-YYYYMMDDHHMM.txt          精简文本，图片是 [IMG#1] 占位符
discord-YYYYMMDDHHMM.json         完整结构化数据（备份用）
discord-YYYYMMDDHHMM-images.txt   图片 URL 清单
   │  enrich_images.py（下载 + 转写 + 缓存）
   ▼
discord-YYYYMMDDHHMM.enriched.txt 图片已替换成文字
   │  digest.py
   ▼
discord-YYYYMMDDHHMM.digest.md    日报
```

---

## 一次性准备

### 1. 浏览器脚本

装 [Tampermonkey](https://www.tampermonkey.net/)，新建脚本，把
`discord-digest-exporter.user.js` 的内容整段粘进去保存。

> 不建议直接往 Discord 的 F12 控制台粘代码。Discord 在控制台里放了一个
> 红色警告，是为了防止有人骗你粘恶意脚本——用 Tampermonkey 至少代码是
> 存在你自己这儿的。无论如何，粘任何脚本之前先自己读一遍。

打开脚本顶部的 `CFG` 可以改配置：

| 项 | 默认 | 说明 |
|---|---|---|
| `hoursBack` | 24 | 往回抓多少小时 |
| `limitByHours` | `true` | 导出时是否按 `hoursBack` 过滤；设为 `false` 导出全部已采集消息 |
| `autoScroll` | `false` | `false` = 被动模式，你自己滚，脚本只记录 |
| `scrollDelayMs` | 800 | 自动模式下每次滚动的等待时间，网慢就调大 |

**关于模式选择**：Discord 条款禁止用自动化手段访问服务。被动模式下滚动是你
自己做的，脚本只读取浏览器已经渲染给你看的内容，性质上更接近"复制粘贴"；
自动模式严格说仍在灰色地带，虽然服务端流量和真人滚动没有区别、检测面接近零。
建议默认用被动模式。

### 2. Python 环境

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install anthropic requests pillow
```

### 3. API Key

```bash
export ANTHROPIC_API_KEY=sk-ant-...
# 国内直连 api.anthropic.com 可能不通，需要代理：
export HTTPS_PROXY=http://127.0.0.1:7890
```

（Windows PowerShell 用 `$env:ANTHROPIC_API_KEY="..."`）

---

## 每天的操作

**第一步：导出。** 打开 `#tradingroom`，右下角出现小面板。往上滚到你想要的
起点（比如昨天这个时候），再滚回底部，面板计数会一路涨。点"导出"，浏览器会
下载三个文件。

如果你想导出"全部已抓取内容"（不按时间裁剪），把面板里的
"仅导出最近 hoursBack" 取消勾选再点"导出"。

被动模式下滚动要慢一点——滚太快 Discord 来不及渲染，中间的消息会漏。
按住 `PageUp` 大概是合适的速度。

**第二步：处理图片。** 这一步要尽快做，Discord 的图片 URL 带签名、
**大约 24 小时后失效**。

```bash
python enrich_images.py discord-202607261830.txt
```

图片按内容哈希缓存在 `./cache/` 下，同一张图（比如反复转发的那张）
只会调一次 API。不想花钱可以加 `--no-api`，只下载不转写。

**第三步：生成日报。**

```bash
python digest.py discord-202607261830.enriched.txt
```

排查问题时建议开启调试日志：

```bash
python digest.py discord-202607261830.enriched.txt --debug
```

`digest.py` 现在支持以下能力：

- `--debug`：把每次 API 调用的 `stop_reason`、输入/输出 token、内容块类型等写到
  `.digest.debug.jsonl`，用于定位空输出、截断等问题。
- `--debug-file <path>`：自定义调试日志文件路径。
- `--max-tokens <n>`：调整单次调用的输出上限（默认 8000）。
- 自动续写：如果某次返回 `stop_reason=max_tokens`，脚本会自动发起"继续"请求并拼接结果，
  避免生成半截日报。

得到 `.digest.md`，包含：一句话概览、拆开的话题线索（每条列出参与者、
正反论据、结论）、提到的标的表格、**黑话与术语注释**、以及哪些说法
是断言但没给论据。

---

## 成本

一天几百条消息的精简文本大约几千到一万 token。用 `claude-sonnet-5` 做日报，
单次成本很低。图片转写默认走 `claude-haiku-4-5-20251001`，更便宜，
且有缓存不会重复计费。想要更好的图表理解可以：

```bash
python enrich_images.py in.txt --model claude-sonnet-5
```

---

## 出问题时

**导出的作者名全是 `(unknown)`，或者一条都抓不到**
Discord 的 CSS class 名是 hash 过的，改版会失效。在 F12 控制台运行：

```js
__digest.diagnose()
```

会打一张表，哪一项是 0 就是哪个选择器坏了。修复方法：在 Discord 页面里右键
一条消息 → 检查，看看新的属性名，改脚本里对应的 `[class*="..."]`。
用 `id^=` 开头的那几个（`message-content-`、`chat-messages-`）比较稳定，
基本不会变。

**图片下载报 403 / 404**
签名过期了。重新导出一次，或者用 JSON 文件里保留的消息 ID 回到 Discord 手动看。
养成导出后立刻跑 `enrich_images.py` 的习惯。

**消息有缺漏**
滚动太快。降低速度重滚一遍，脚本按 message ID 去重，重复采集不会产生重复条目。
也可以不点"清空"，分几次滚完再一起导出。

**API 连不上**
检查 `HTTPS_PROXY`。`requests` 和 `anthropic` SDK 都读这个环境变量。

**日报看起来没写完（末尾半句话/半张表）**
先用 `--debug` 重跑，查看 `.digest.debug.jsonl` 里是否出现
`"stop_reason": "max_tokens"`。脚本会自动续写；如果仍连续触发上限，
请提高 `--max-tokens`，或减少输入规模后再生成。

---

## 后续计划

- 结构完整性检查（计划中）：生成后自动校验关键章节是否齐全（如"一句话概览"、
  "话题线索"、"提到的具体标的与事件"、"黑话与术语注释"、"值得追问的地方"）。
  若缺失章节，再自动补一次请求。这个功能后续再加。

---

## 可以自己改的地方

- **`enrich_images.py` 里的 `TRANSCRIBE_PROMPT`**：现在是文字截图逐字提取、
  图表简述、表情包直接标记为无关。如果群里图表变多，可以让它多说一点读数。
- **`digest.py` 里的 `PROMPT`**：章节结构直接改这里。比如你只想要术语注释
  不要话题摘要，删掉对应段落即可。
- **`SYSTEM` 里对你自己的描述**：现在写的是"有量化金融背景，数学不用解释，
  但对交易黑话和板块叙事不熟"。等你熟了之后把这句改掉，输出会更精简。
- **积累术语表**：跑一段时间后，把 digest 里的术语注释汇总成一个固定文件，
  在 `PROMPT` 里作为已知词汇传进去，让模型只解释新出现的词。
