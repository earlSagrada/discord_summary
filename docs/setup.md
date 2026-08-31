> 返回 [文档索引](./README.md) · [项目 README](../README.md)

# 安装与日常操作

## 一次性准备

### 1. 浏览器脚本

装 [Tampermonkey](https://www.tampermonkey.net/)，新建脚本，把
`userscript/discord-digest-exporter.user.js` 的内容整段粘进去保存。

> 不建议直接往 Discord 的 F12 控制台粘代码。Discord 在控制台里放了一个红色警告，是为了防止有人骗你粘恶意脚本。用 Tampermonkey 至少代码存在你自己这儿；粘任何脚本前先自己读一遍。

打开脚本顶部的 `CFG` 可以改配置：

| 项 | 默认 | 说明 |
|---|---|---|
| `hoursBack` | 24 | 往回抓多少小时 |
| `limitByHours` | `true` | 导出时是否按 `hoursBack` 过滤；设为 `false` 导出全部已采集消息 |
| `autoScroll` | `false` | `false` = 被动模式，你自己滚，脚本只记录 |
| `scrollDelayMs` | 800 | 自动模式下每次滚动的等待时间，网慢就调大 |

**关于模式选择**：Discord 条款禁止用自动化手段访问服务。被动模式下滚动是你自己做的，脚本只读取浏览器已经渲染给你看的内容，性质上更接近“复制粘贴”；自动模式严格说仍在灰色地带，虽然服务端流量和真人滚动没有区别、检测面接近零。建议默认用被动模式。

### 2. Python 环境

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. API Key

把 `.env.example` 复制成 `.env`，填入你的 key（`.env` 已被 `.gitignore` 忽略，不会提交）：

```
ANTHROPIC_API_KEY=sk-ant-...
# 国内直连 api.anthropic.com 可能不通，需要代理：
# HTTPS_PROXY=http://127.0.0.1:7890
FINNHUB_API_KEY=...
FMP_API_KEY=...
FRED_API_KEY=...
POLYGON_API_KEY=...
```

所有脚本 `import config` 会自动加载 `.env`；也可临时用 `$env:ANTHROPIC_API_KEY="..."` 覆盖。

## 每天的操作

### 悬浮面板按钮说明

脚本跑起来后，右下角出现一个 **📥 聊天导出器** 小面板。从上到下：

| 元素 | 名字 | 作用 |
|---|---|---|
| 顶部计数 | `已采集 N 条消息` | 当前脚本被动记录到的消息数，会随你滚动实时增长 |
| 复选框 | `仅导出最近 N 小时` | 勾选=只导出最近 `CFG.hoursBack` 小时；**取消勾选=导出全部已采集消息**（不按时间裁剪） |
| 按钮 ① | `自动往上滚·补历史` | 脚本模拟往上滚动，把更早的历史消息也采集进来（被动模式下你也可以自己按 `PageUp` 慢慢滚） |
| 按钮 ② | `立即导出（下载文件）` | 立刻把已采集消息导出成 **3 个文件**（`.json` / `.txt` / `-images.txt`）到下载目录 |
| 按钮 ③ | `定时导出：关 / 开(每15分钟)` | 点一下开启，之后每 `CFG.autoExportMin` 分钟自动导出一次；**标签页要保持打开**，关掉就停 |
| 按钮 | `清空计数（重新采集）` | 清掉已采集缓存并从 0 重新计数，**不会**删除已下载的文件 |

> 每个按钮/复选框都带 **鼠标悬停提示（title）**，忘了含义时把鼠标移上去即可看到。
> 控制台里也可手动调用：`__digest.toggleAuto(true)` 开定时导出、`__digest.diagnose()` 排查选择器、`__digest.exportAll()` 手动导出。

### 走一遍

**第一步：导出。** 打开 `#tradingroom`，右下角出现小面板。往上滚到你想要的起点（比如昨天这个时候），再滚回底部，面板计数会一路涨。点 **②立即导出**，浏览器会下载三个文件。

如果你想导出“全部已抓取内容”（不按时间裁剪），把面板里的 **“仅导出最近 N 小时”** 取消勾选再点 **②立即导出**。

被动模式下滚动要慢一点——滚太快 Discord 来不及渲染，中间的消息会漏。按住 `PageUp` 大概是合适的速度。

**第二步：处理图片。** 这一步要尽快做，Discord 的图片 URL 带签名、**大约 24 小时后失效**。

```bash
python src/enrich_images.py discord-202607261830.txt
```

图片按内容哈希缓存在 `data/cache/` 下，同一张图（比如反复转发的那张）只会调一次 API。不想花钱可以加 `--no-api`，只下载不转写。

**第三步：生成日报。**

```bash
python src/digest.py discord-202607261830.enriched.txt
```

排查问题时建议开启调试日志：

```bash
python src/digest.py discord-202607261830.enriched.txt --debug
```

`digest.py` 支持：

| 参数/能力 | 说明 |
|---|---|
| `--debug` | 把每次 API 调用的 `stop_reason`、输入/输出 token、内容块类型等写到 `.digest.debug.jsonl` |
| `--debug-file <path>` | 自定义调试日志文件路径 |
| `--max-tokens <n>` | 调整单次调用的输出上限（默认 8000） |
| 自动续写 | 如果某次返回 `stop_reason=max_tokens`，脚本会自动发起“继续”请求并拼接结果，避免生成半截日报 |

得到 `.digest.md`，包含：一句话概览、拆开的话题线索（每条列出参与者、正反论据、结论）、提到的标的表格、**黑话与术语注释**、以及哪些说法是断言但没给论据。
