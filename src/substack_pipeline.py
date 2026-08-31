"""新 Substack 复盘落地 → 自动跑一遍「复盘 + 自我优化 + VIP 确认」。

Frank 每周一更新 Substack。那是我们能拿到的最高质量分析范式，所以新文件一到，
就该重新校准一次系统，而不是干等周日的定时任务。

流程（顺序有讲究，后一步要用前一步的产出）：
  1. `substack.extract()`  解析 PDF，**质量不合格就直接中止**——
     宁可不跑，也不能拿缺了数字的残缺文本去优化系统。
  2. `review.py`      周期复盘 → 更新 reviews/ 和 vip_suggestions.md
  3. `optimize.py`    自我优化（opus）→ 调参数、改方法库、写决策记录
  4. `vips.py --auto` 把连续 2 期都被建议的名单改动落地
  5. 标记 PDF 已处理（**全部成功才标**，失败下轮会重试）

触发方式有两种：
  - `cycle.py` 每 15 分钟顺手检查一次（几乎零成本），发现新文件就**后台**跑这个脚本，
    不阻塞脉搏推送；
  - 手动 `python src/substack_pipeline.py --force`。

用法：
    python src/substack_pipeline.py            # 有新 PDF 才跑
    python src/substack_pipeline.py --force    # 不管有没有新文件都跑一遍
    python src/substack_pipeline.py --dry-run  # 各步骤都不落盘、不推送
"""

import argparse
import traceback
from datetime import datetime

import config
import substack

LOG = config.DATA_DIR / "pipeline.log"


def log(msg: str) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line, flush=True)
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _notify(text: str) -> None:
    try:
        import discord_post
        discord_post.send(text)
    except Exception as e:
        log(f"通知发送失败：{type(e).__name__}: {e}")


def run(force: bool = False, dry_run: bool = False, post: bool = True) -> int:
    fresh = substack.list_posts() if force else substack.new_posts()
    if not fresh:
        log("没有新的 Substack 复盘，跳过。")
        return 0
    names = "、".join(p.stem for p in fresh)
    log(f"发现新 Substack 复盘：{names} → 开始重新校准系统")

    # 1) 解析：质量不合格就中止，绝不拿残缺文本去改系统
    ok_files = []
    for pdf in fresh:
        try:
            text = substack.extract(pdf, force=True)
            log(f"解析 {pdf.name}：{len(text)} 字")
            ok_files.append(pdf)
        except substack.ExtractError as e:
            log(f"解析失败，中止本次校准：{e}")
            if post:
                _notify(f"⚠️ **新的 Substack 复盘解析失败，没有触发系统校准。**\n```{e}```\n"
                        f"多半是 PDF 导出方式的问题，换成浏览器「打印为 PDF」通常可以。")
            return 1

    if dry_run:
        log("dry-run：解析成功，后续步骤跳过。")
        return 0

    if post:
        _notify(f"📥 **收到新的 Substack 复盘（{names}），正在重新校准系统……**\n"
                f"_稍后会依次推送：周期复盘 → 系统自我优化。_")

    failures = []

    # 2) 周期复盘（会刷新 vip_suggestions.md，第 4 步要用）
    try:
        import review
        review.run(days=7, post=post)
        log("周期复盘完成")
    except Exception as e:
        failures.append(f"周期复盘：{type(e).__name__}: {e}")
        log(f"周期复盘失败：{traceback.format_exc().splitlines()[-1]}")

    # 3) 自我优化（读第 2 步产出的复盘）
    try:
        import optimize
        optimize.run(post=post)
        log("自我优化完成")
    except Exception as e:
        failures.append(f"自我优化：{type(e).__name__}: {e}")
        log(f"自我优化失败：{traceback.format_exc().splitlines()[-1]}")

    # 4) VIP 名单：只落地连续 2 期都被建议的
    try:
        import vips
        added, removed = vips.run_auto()
        if added or removed:
            msg = (f"👥 **重点发言人名单已更新**（连续 2 期建议才会自动生效）\n"
                   + (f"加入：{'、'.join(added)}\n" if added else "")
                   + (f"移除：{'、'.join(removed)}\n" if removed else "")
                   + "_不同意的话：`python src/vips.py --undo`_")
            log(f"VIP 名单变更：加 {added}，减 {removed}")
            if post:
                _notify(msg)
        else:
            log("VIP 名单无变更（建议还没到连续 2 期的门槛）")
    except Exception as e:
        failures.append(f"VIP 名单：{type(e).__name__}: {e}")
        log(f"VIP 处理失败：{traceback.format_exc().splitlines()[-1]}")

    if failures:
        log(f"本次校准有 {len(failures)} 步失败，不标记 PDF，下轮会重试")
        if post:
            _notify("⚠️ **系统校准部分失败**，下一轮会自动重试：\n"
                    + "\n".join(f"- {f}" for f in failures))
        return 1

    substack.mark_seen(ok_files)
    log(f"校准完成，已标记 {len(ok_files)} 个 PDF 为已处理")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="新 Substack 复盘 → 自动复盘+优化+VIP")
    ap.add_argument("--force", action="store_true", help="不管有没有新文件都跑一遍")
    ap.add_argument("--dry-run", action="store_true", help="只解析，不跑复盘/优化")
    ap.add_argument("--no-post", action="store_true", help="不推送 Discord")
    args = ap.parse_args()
    raise SystemExit(run(force=args.force, dry_run=args.dry_run, post=not args.no_post))


if __name__ == "__main__":
    main()
