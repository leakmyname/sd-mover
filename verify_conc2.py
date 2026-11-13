# -*- coding: utf-8 -*-
"""验证: 并发写入多盘 + 进度/速度/文件大小/ETA 显示

照 sched_test.py 的可行模式:
  - 直接构造 MainWindow, 手动赋 ext_drives / sd_cards
  - 用 fake _start_transfer 记录 (letter, primary)
  - 直接调 _plan_and_start()
不走 _poll_drives(), 避免 _test_processed 与 old_sd 干扰。
"""
import os, sys, time, shutil, traceback
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QCoreApplication

app = QApplication(sys.argv)

from sd_mover.drive_info import DriveInfo
import sd_mover.main_window as mw
from sd_mover.main_window import MainWindow

GB = 1024 ** 3
results = []


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print("[%s] %s %s" % ("PASS" if cond else "FAIL", name, detail), flush=True)


def fake_drive(letter, total, free, label="EXT"):
    d = DriveInfo(letter=letter, label=label, serial=0, total=total,
                  free=free, drive_type=2, fs="NTFS")
    d._min_ext_gb = 700
    return d


def fake_sd(letter, used):
    total = 256 * GB
    d = DriveInfo(letter=letter, label="SDXC", serial=0, total=total,
                  free=total - used, drive_type=2, fs="exFAT")
    return d


def build_window(ext, sds):
    w = MainWindow()
    w.ext_drives = ext
    w.sd_cards = sds
    w._started = []

    def fake_start(d, forced_primary=None):
        w._started.append((d.letter, forced_primary))
        w.processing.add(d.letter)
    w._start_transfer = fake_start
    free_map = {k: v.free for k, v in ext.items()}
    w._free_map = free_map

    def fake_usage(p):
        if len(p) >= 2 and p[1] == ':':
            l = p[0]
            if l in free_map:
                return (free_map.get('_total_' + l, 900 * GB), free_map[l])
        return (900 * GB, 900 * GB)
    mw.get_disk_usage = fake_usage
    return w


def scenario_multi_disk_spread():
    """3 cards x 256G, 2 disks (400G each), max_hd=2
    -> must use BOTH disks, not only one."""
    print("\n=== SCENARIO: cards spread over 2 disks ===", flush=True)
    ext = {'M': fake_drive('M', 900 * GB, 400 * GB),
           'N': fake_drive('N', 900 * GB, 400 * GB)}
    sds = {chr(ord('F') + i): fake_sd(chr(ord('F') + i), 256 * GB)
           for i in range(3)}
    w = build_window(ext, sds)
    w._free_map['_total_M'] = 900 * GB
    w._free_map['_total_N'] = 900 * GB
    w.max_concurrent_sd_spin.setValue(10)
    w.max_concurrent_hd_spin.setValue(2)
    w.scatter_radio.setChecked(True)
    for letter in sorted(sds):
        w._ready_to_start.add(letter)
    w._plan_and_start()
    primaries = [p for _, p in w._started]
    print("  started=%s" % (w._started,))
    # max_hd=2 是"同一时刻最多写几块硬盘"的硬上限: 3张卡也只有2张能同时开工,
    # 第3张必须排队(等某张卡写完释放目标盘), 不能铺开到第3块盘。
    check("max_hd=2 -> exactly 2 cards started",
          len(w._started) == 2, "n=%d" % len(w._started))
    check("3rd card queued waiting for a free disk",
          len(w.waiting_set) == 1 or len(w.pending_queue) == 1,
          "waiting=%s queue=%s" % (sorted(w.waiting_set),
                                   [d.letter for d in w.pending_queue]))
    check("cards spread across BOTH disks",
          set(primaries) == {'M', 'N'}, "primaries=%s" % primaries)
    # 用计划器记录的真实分配方案校验: 每块盘预占的字节不得超过其剩余空间
    per_disk = {}
    for letter, plan in getattr(w, '_last_plans', {}).items():
        for dl, take in plan:
            per_disk[dl] = per_disk.get(dl, 0) + take
    print("  real plan=%s per_disk=%s" % (dict(w._last_plans),
                                          {k: v // GB for k, v in per_disk.items()}))
    ok = per_disk and all(v <= 400 * GB for v in per_disk.values())
    check("per-disk reservation <= free", ok,
          "per_disk=%s" % {k: v // GB for k, v in per_disk.items()})
    # 每个卡的数据总量必须被完整分配(不丢数据)
    ok2 = True
    for letter, plan in w._last_plans.items():
        if sum(t for _, t in plan) != 256 * GB:
            ok2 = False
    check("each card's data fully planned (no loss)", ok2, "")


def scenario_max_hd_1_serial():
    """max_hd=1 -> all cards must go to ONE disk."""
    print("\n=== SCENARIO: max_hd=1 forces single disk ===", flush=True)
    ext = {'M': fake_drive('M', 900 * GB, 500 * GB),
           'N': fake_drive('N', 900 * GB, 500 * GB)}
    sds = {chr(ord('F') + i): fake_sd(chr(ord('F') + i), 256 * GB)
           for i in range(3)}
    w = build_window(ext, sds)
    w._free_map['_total_M'] = 900 * GB
    w._free_map['_total_N'] = 900 * GB
    w.max_concurrent_sd_spin.setValue(10)
    w.max_concurrent_hd_spin.setValue(1)
    w.scatter_radio.setChecked(True)
    for letter in sorted(sds):
        w._ready_to_start.add(letter)
    w._plan_and_start()
    primaries = [p for _, p in w._started]
    print("  started=%s" % (w._started,))
    check("max_hd=1 -> only 1 disk used", len(set(primaries)) == 1,
          "primaries=%s" % primaries)


def scenario_no_over_subscribe_3_cards():
    """1 disk 300G, 3 cards x 256G -> only 1 starts, other 2 queued."""
    print("\n=== SCENARIO: no over-subscription on 1 disk ===", flush=True)
    ext = {'M': fake_drive('M', 900 * GB, 300 * GB)}
    sds = {chr(ord('F') + i): fake_sd(chr(ord('F') + i), 256 * GB)
           for i in range(3)}
    w = build_window(ext, sds)
    w._free_map['_total_M'] = 900 * GB
    w.max_concurrent_sd_spin.setValue(10)
    w.max_concurrent_hd_spin.setValue(1)
    w.scatter_radio.setChecked(True)
    for letter in sorted(sds):
        w._ready_to_start.add(letter)
    w._plan_and_start()
    print("  started=%s queued=%s" % (w._started, sorted(w.waiting_set)))
    check("only 1 card started (300G fits one 256G)", len(w._started) == 1,
          "started=%s" % w._started)
    check("other 2 queued", len(w.waiting_set) == 2,
          "waiting=%s" % sorted(w.waiting_set))


def scenario_ui_columns_and_eta_format():
    """UI: sd_table 列数与 ETA 格式化, 不出现负数/超100%."""
    print("\n=== SCENARIO: UI columns + ETA format ===", flush=True)
    w = MainWindow()
    check("sd_table has 10 columns", w.sd_table.columnCount() == 10,
          "cols=%d" % w.sd_table.columnCount())
    hdr = [w.sd_table.horizontalHeaderItem(i).text()
           for i in range(w.sd_table.columnCount())]
    print("  headers=%s" % hdr)
    check("has 进度/速度/剩余时间 columns",
          "进度" in hdr and "速度" in hdr and "剩余时间" in hdr,
          "headers=%s" % hdr)
    check("has 暂停/取消/弹出 buttons in headers",
          "暂停" in hdr and "取消/开始" in hdr and "弹出" in hdr,
          "headers=%s" % hdr)
    # ETA format edge cases
    from sd_mover.main_window import format_eta
    eta_cases = [(-5, "即将完成"), (0, "即将完成"), (45, "45秒"),
                 (90, "1分钟"), (3600, "1.0小时"), (5400, "1.5小时"),
                 (100000, "27.8小时")]
    bad = []
    for sec, expect in eta_cases:
        got = format_eta(sec)
        if got != expect:
            bad.append((sec, got, expect))
    check("format_eta correct for all edge cases (incl. negative)",
          not bad, "bad=%s" % bad)


def scenario_progress_clamp():
    """进度: 0~100 不越界, 速度非负."""
    print("\n=== SCENARIO: progress clamp ===", flush=True)
    w = MainWindow()
    d = DriveInfo(letter='F', label='SDXC', serial=1, total=256 * GB,
                  free=0, drive_type=2, fs='exFAT')
    w.sd_cards = {'F': d}
    w._add_sd_row(d)
    row = w._find_row('F')
    check("row added", row >= 0, "row=%s" % row)
    # simulate over-100 progress
    w._on_progress('F', 300 * GB, 256 * GB, 'f.bin', 150.0, 1, 3)
    app.processEvents()
    bar = w.sd_table.cellWidget(row, 4)
    v = bar.value() if bar else -1
    check("progress clamped to <=100", 0 <= v <= 100, "value=%s" % v)
    sp = w.sd_table.item(row, 5)
    eta = w.sd_table.item(row, 6)
    print("  speed=%r eta=%r" % (sp.text() if sp else None,
                                 eta.text() if eta else None))
    check("speed cell non-empty", bool(sp and sp.text()), "speed=%r" % (sp.text() if sp else None))
    check("eta cell non-empty", bool(eta and eta.text()), "eta=%r" % (eta.text() if eta else None))
    # negative copied
    w._on_progress('F', -100, 256 * GB, 'f.bin', 0.0, 0, 3)
    app.processEvents()
    bar = w.sd_table.cellWidget(row, 4)
    v2 = bar.value() if bar else -1
    check("progress never negative", v2 >= 0, "value=%s" % v2)


def scenario_eject_button_widget_exists():
    """每行必须有 弹出 按钮 widget (列9) 与 暂停(列7)/取消(列8) widget"""
    print("\n=== SCENARIO: per-row eject/pause/cancel widgets ===", flush=True)
    from PyQt5.QtWidgets import QPushButton
    w = MainWindow()
    d = DriveInfo(letter='Z', label='SDXC', serial=9, total=256 * GB,
                  free=0, drive_type=2, fs='exFAT')
    w.sd_cards = {'Z': d}
    w._add_sd_row(d)
    row = w._find_row('Z')
    b7 = w.sd_table.cellWidget(row, 7)
    b8 = w.sd_table.cellWidget(row, 8)
    b9 = w.sd_table.cellWidget(row, 9)
    check("pause button widget exists at col7",
          isinstance(b7, QPushButton) and b7.text() == "暂停",
          "obj=%s text=%r" % (type(b7).__name__,
                              b7.text() if isinstance(b7, QPushButton) else None))
    check("cancel button widget exists at col8",
          isinstance(b8, QPushButton), "obj=%s" % type(b8).__name__)
    check("eject button widget exists at col9",
          isinstance(b9, QPushButton) and b9.text() == "弹出",
          "obj=%s text=%r" % (type(b9).__name__,
                              b9.text() if isinstance(b9, QPushButton) else None))

if __name__ == '__main__':
    for fn in [scenario_multi_disk_spread, scenario_max_hd_1_serial,
               scenario_no_over_subscribe_3_cards,
               scenario_ui_columns_and_eta_format, scenario_progress_clamp,
               scenario_eject_button_widget_exists]:
        try:
            fn()
        except Exception:
            traceback.print_exc()
            check(fn.__name__ + " (exception)", False, "see traceback")
    p = sum(1 for _, c, _ in results if c)
    f = len(results) - p
    print("\n===== %d passed, %d failed =====" % (p, f), flush=True)
    sys.exit(1 if f else 0)
