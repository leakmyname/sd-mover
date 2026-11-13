# -*- coding: utf-8 -*-
"""验证对账口径: 有跳过的卡不能计入"搬完几块盘" (v11.12.11 修复)
用户的原始抱怨: 日志写 [OK]传输完成: 0.0B -> R:..., 每日汇总虚增 1 张 0.0B
"""
import os, sys, tempfile, shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sd_mover.log_manager import LogManager

results = []


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print("[%s] %s %s" % ("PASS" if cond else "FAIL", name, detail), flush=True)


def scenario_all_skipped_not_counted():
    base = tempfile.mkdtemp(prefix='sdm_acct1_')
    try:
        lm = LogManager(base)
        # 复现: 主盘只剩48MB, 3个50MB文件全部跳过 (用户日志 [OK]传输完成: 0.0B)
        lm.add_transfer(sd_letter='R', sd_label='SDXC', sd_size='238.4 GB',
                        dest_letter='R', dest_label='', folder='ego11_x',
                        bytes_tx=0, file_count=136, status="部分完成",
                        skipped_bytes=150 * 1024 * 1024, note=150 * 1024 * 1024)
        st = lm.load_session_stats()
        rec = open(os.path.join(base, '传输记录.txt'), encoding='utf-8').read()
        daily = open(os.path.join(base, '每日汇总.txt'), encoding='utf-8').read()
        check("all-skipped not counted as a card", st['total_sd_cards'] == 0,
              "cards=%s" % st['total_sd_cards'])
        check("all-skipped adds no bytes", st['total_bytes'] == 0,
              "bytes=%s" % st['total_bytes'])
        check("partial tracked separately", st.get('partial_count') == 1,
              "partial=%s" % st.get('partial_count'))
        check("record shows skipped size", '跳过' in rec, "rec=%r" % rec.strip())
        # 每日汇总对部分完成的卡不计数, 总计应为 0 张, 且不应出现 "1张SD卡"
        check("daily summary excludes partial card",
              ('1张SD卡' not in daily) and ('总计: 0张SD卡' in daily),
              "daily=%r" % daily)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def scenario_full_completion_still_counted():
    base = tempfile.mkdtemp(prefix='sdm_acct2_')
    try:
        lm = LogManager(base)
        lm.add_transfer(sd_letter='F', sd_label='SDXC', sd_size='238.4 GB',
                        dest_letter='M', dest_label='HDD04', folder='ego05_a',
                        bytes_tx=237 * 1024 ** 3, file_count=103, status="完成")
        st = lm.load_session_stats()
        daily = open(os.path.join(base, '每日汇总.txt'), encoding='utf-8').read()
        check("full completion counted", st['total_sd_cards'] == 1,
              "cards=%s" % st['total_sd_cards'])
        check("full completion bytes counted", st['total_bytes'] == 237 * 1024 ** 3,
              "bytes=%s" % st['total_bytes'])
        check("drive recorded", 'M' in st.get('drives_used', []),
              "drives=%s" % st.get('drives_used'))
        check("daily summary shows 1 card", '1张SD卡' in daily,
              "daily=%r" % daily)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def scenario_mixed_day_summary():
    base = tempfile.mkdtemp(prefix='sdm_acct3_')
    try:
        lm = LogManager(base)
        # 2 full + 1 partial
        for i in range(2):
            lm.add_transfer(sd_letter='F%d' % i, sd_label='SDXC', sd_size='256 GB',
                            dest_letter='M', dest_label='HDD04', folder='ego0%d' % i,
                            bytes_tx=200 * 1024 ** 3, file_count=50, status="完成")
        lm.add_transfer(sd_letter='R', sd_label='SDXC', sd_size='238 GB',
                        dest_letter='R', dest_label='HDD03', folder='ego09',
                        bytes_tx=196 * 1024 ** 3, file_count=88, status="部分完成",
                        skipped_bytes=40 * 1024 ** 3, note=40 * 1024 ** 3)
        st = lm.load_session_stats()
        daily = open(os.path.join(base, '每日汇总.txt'), encoding='utf-8').read()
        check("mixed: only 2 full cards counted", st['total_sd_cards'] == 2,
              "cards=%s" % st['total_sd_cards'])
        check("mixed: bytes only from full cards", st['total_bytes'] == 400 * 1024 ** 3,
              "bytes=%s" % st['total_bytes'])
        check("mixed: 2 partials skipped counted", st.get('partial_count') == 1,
              "partial=%s" % st.get('partial_count'))
        check("mixed: skipped bytes tracked",
              st.get('partial_bytes_skipped') == 40 * 1024 ** 3,
              "skipped=%s" % st.get('partial_bytes_skipped'))
        check("mixed: daily shows 2 cards", '2张SD卡' in daily,
              "daily=%r" % daily)
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == '__main__':
    for fn in [scenario_all_skipped_not_counted,
               scenario_full_completion_still_counted,
               scenario_mixed_day_summary]:
        try:
            fn()
        except Exception:
            import traceback
            traceback.print_exc()
            check(fn.__name__ + " (exception)", False, "see traceback")
    p = sum(1 for _, c, _ in results if c)
    f = len(results) - p
    print("\n===== %d passed, %d failed =====" % (p, f), flush=True)
    sys.exit(1 if f else 0)
