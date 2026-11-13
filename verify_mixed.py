# -*- coding: utf-8 -*-
"""验证: 256G + 512G SD 卡混插, 防闪退, 取消后续传"""
import os, sys, time, traceback
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from PyQt5.QtWidgets import QApplication
app = QApplication(sys.argv)
from sd_mover.drive_info import DriveInfo
import sd_mover.main_window as mw
from sd_mover.main_window import MainWindow
from sd_mover.transfer_worker import TransferWorker

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


def fake_sd(letter, used, total_gb=256):
    total = total_gb * GB
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


def scenario_mixed_256_512():
    print("\n=== SCENARIO: mixed 256G + 512G cards ===", flush=True)
    ext = {'M': fake_drive('M', 900 * GB, 800 * GB),
           'N': fake_drive('N', 900 * GB, 800 * GB)}
    sds = {'F': fake_sd('F', 256 * GB, 256),
           'G': fake_sd('G', 512 * GB, 512)}
    w = build_window(ext, sds)
    w._free_map['_total_M'] = 900 * GB
    w._free_map['_total_N'] = 900 * GB
    w.max_concurrent_sd_spin.setValue(10)
    w.max_concurrent_hd_spin.setValue(2)
    w.scatter_radio.setChecked(True)
    for letter in sorted(sds):
        w._ready_to_start.add(letter)
    w._plan_and_start()
    print("  started=%s plans=%s" % (w._started, dict(w._last_plans)))
    check("both cards started (mixed sizes)", len(w._started) == 2,
          "n=%d" % len(w._started))
    ok = True
    for letter, plan in w._last_plans.items():
        want = 256 * GB if letter == 'F' else 512 * GB
        if sum(t for _, t in plan) != want:
            ok = False
    check("each card's data size planned exactly", ok,
          "plans=%s" % {k: sum(t for _, t in v) // GB
                        for k, v in w._last_plans.items()})
    per = {}
    for _, plan in w._last_plans.items():
        for dl, t in plan:
            per[dl] = per.get(dl, 0) + t
    check("no disk over-reserved", all(v <= 800 * GB for v in per.values()),
          "per=%s" % {k: v // GB for k, v in per.items()})


def scenario_whole_file_512_on_small_disk():
    print("\n=== SCENARIO: whole-file 512G card vs 300G disk ===", flush=True)
    ext = {'M': fake_drive('M', 900 * GB, 300 * GB)}
    sds = {'G': fake_sd('G', 512 * GB, 512)}
    w = build_window(ext, sds)
    w._free_map['_total_M'] = 900 * GB
    w.max_concurrent_sd_spin.setValue(10)
    w.max_concurrent_hd_spin.setValue(1)
    w.whole_radio.setChecked(True)
    w._ready_to_start.add('G')
    w._plan_and_start()
    print("  started=%s waiting=%s" % (w._started, sorted(w.waiting_set)))
    check("512G card queued (300G disk can't hold it)",
          len(w._started) == 0 and 'G' in w.waiting_set,
          "started=%s waiting=%s" % (w._started, sorted(w.waiting_set)))


def scenario_crash_guard_no_exception():
    print("\n=== SCENARIO: callbacks swallow exceptions (anti-crash) ===",
          flush=True)
    w = MainWindow()
    bad = []
    for cb, args in [
        (w._on_progress, ('ZZ', 10, 100, 'f', 1.0, 1, 2)),
        (w._on_completed, ('ZZ', 'M', 'x', 10, 1)),
        (w._on_dest_changed, ('ZZ', 'M')),
        (w._update_sd_status, ('ZZ', 'x')),
    ]:
        try:
            cb(*args)
            app.processEvents()
        except Exception as e:
            bad.append((cb.__name__, repr(e)))
    check("no callback raises on unknown letter", not bad, "bad=%s" % bad)


def scenario_resume_after_cancel():
    print("\n=== SCENARIO: resume after cancel ===", flush=True)
    w = MainWindow()
    w._card_folder_map['F'] = (7, 'ego07_20261113_120000', 'M', 12345)
    d = DriveInfo(letter='F', label='SDXC', serial=12345, total=256 * GB,
                  free=0, drive_type=2, fs='exFAT')
    w.sd_cards = {'F': d}
    w.ext_drives = {'M': fake_drive('M', 900 * GB, 800 * GB)}

    def fake_usage(p):
        return (900 * GB, 800 * GB)
    mw.get_disk_usage = fake_usage
    # 保留真实 _start_transfer, 这样 TransferWorker 会被构造, 才能拦截参数
    w._started = []
    captured = {}
    orig = TransferWorker

    class SpyWorker(orig):
        def __init__(self, *a, **kw):
            captured['resume_ok'] = kw.get('resume_ok')
            captured['folder_name'] = kw.get('folder_name')
            super().__init__(*a, **kw)
            try:
                self.terminate()
            except Exception:
                pass
    mw.TransferWorker = SpyWorker
    try:
        w._ready_to_start.add('F')
        w._plan_and_start()
        app.processEvents()
        time.sleep(0.2)
        app.processEvents()
        print("  captured=%s" % captured)
        check("resume_ok True for same serial + same dest",
              captured.get('resume_ok') is True, "captured=%s" % captured)
        check("folder name reused (not regenerated)",
              captured.get('folder_name') == 'ego07_20261113_120000',
              "folder=%r" % captured.get('folder_name'))
        # 模拟: 同一盘符换了一张不同序列号的卡 -> 绝不能续传(否则误删源数据)
        w._card_folder_map['F'] = (7, 'ego07_20261113_120000', 'M', 999)
        captured.clear()
        w.workers.pop('F', None)
        w.processing.discard('F')
        w._ready_to_start.add('F')
        w._plan_and_start()
        app.processEvents()
        time.sleep(0.2)
        app.processEvents()
        check("different SD serial -> resume_ok False (anti data-loss)",
              captured.get('resume_ok') is False, "captured=%s" % captured)
    finally:
        mw.TransferWorker = orig


if __name__ == '__main__':
    for fn in [scenario_mixed_256_512, scenario_whole_file_512_on_small_disk,
               scenario_crash_guard_no_exception, scenario_resume_after_cancel]:
        try:
            fn()
        except Exception:
            traceback.print_exc()
            check(fn.__name__ + " (exception)", False, "see traceback")
    p = sum(1 for _, c, _ in results if c)
    f = len(results) - p
    print("\n===== %d passed, %d failed =====" % (p, f), flush=True)
    sys.exit(1 if f else 0)
