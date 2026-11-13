# -*- coding: utf-8 -*-
"""验证打散模式: 主盘写满后自动溢出到下一块盘, 不产生不完整文件"""
import os, sys, time, shutil, traceback
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

from PyQt5.QtWidgets import QApplication
app = QApplication(sys.argv)

from sd_mover.transfer_worker import TransferWorker

GB = 1024 ** 3
results = []


class FakeSD:
    def __init__(self, letter, root, total, free):
        self.letter = letter
        self.root = root
        self.total = total
        self.free = free
        self.label = 'SDXC'
        # serial 必须是 0: transfer_worker._source_identity_changed() 会用
        # get_volume_info() 读真实卷序列号来比对, 假的 serial=1 会被判成
        # "源卡已被更换"而立刻停止传输(什么都不拷)。0 表示无法比对, 跳过校验。
        self.serial = 0
        self.reader_serial = ''

    @property
    def is_sd_card(self):
        return True

    @property
    def is_external_drive(self):
        return False

    @property
    def is_removable(self):
        return True

    @property
    def size_gb(self):
        return self.total / (1024 ** 3)

    @property
    def free_gb(self):
        return self.free / (1024 ** 3)


def fake_sd(sd, total):
    return FakeSD('F', sd, total, 0)




def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print("[%s] %s %s" % ("PASS" if cond else "FAIL", name, detail), flush=True)


def _mk_sd(base, spec):
    if os.path.exists(base):
        shutil.rmtree(base, ignore_errors=True)
    sd = os.path.join(base, 'SD')
    os.makedirs(sd)
    total = 0
    for i, (n, sz) in enumerate(spec):
        for k in range(n):
            p = os.path.join(sd, 'file_%d_%d.bin' % (i, k))
            with open(p, 'wb') as f:
                f.write(b'\xBB' * sz)
            total += sz
    return sd, total


def _run_worker(sd, total, d1, d2, folder, mode=True, pri='X'):
    import sd_mover.main_window as mw
    import sd_mover.transfer_worker as tw

    sd_n = sd.replace('\\', '/')
    d1_n = d1.replace('\\', '/')
    d2_n = d2.replace('\\', '/')

    def usage(p):
        r = p.replace('\\', '/')
        if r.startswith(d1_n):
            return (900 * GB, 400 * GB)
        if r.startswith(d2_n):
            return (900 * GB, 600 * GB)
        if r.startswith(sd_n):
            return (1000 * GB, 1000 * GB - total)
        return (0, 0)
    mw.get_disk_usage = usage
    tw.get_disk_usage = usage
    w = TransferWorker(
        sd_info=fake_sd(sd, total), dest_roots=[('X', d1), ('Y', d2)],
        ego_num=1, prefix='ego', auto_eject=False, auto_delete=False,
        reader_serial="", test_mode=True, scatter_mode=mode, primary_dest=pri,
        folder_name=folder)
    return w


def _wait(w, secs=120):
    # 必须驱动 Qt 事件循环: worker 的 pyqtSignal 是 queued 连接,
    # 不 processEvents() 就收不到 progress/log 回调。
    from PyQt5.QtWidgets import QApplication as _QA
    _a = _QA.instance()
    t0 = time.time()
    while w.isRunning() and time.time() - t0 < secs:
        if _a is not None:
            _a.processEvents()
        time.sleep(0.01)
    w.wait(30000)


def _incomplete(d1, d2, sd):
    bad = []
    for dp in (d1, d2):
        for root, dn, fn in os.walk(dp):
            for f in fn:
                p = os.path.join(root, f)
                src = os.path.join(sd, f)
                if os.path.exists(src) and os.path.getsize(p) != os.path.getsize(src):
                    bad.append(f)
    return bad


def scenario_overflow_spreads_across_disks():
    print("\n=== SCENARIO: scatter overflow across disks ===", flush=True)
    base = r'C:\sd_test_scatter'
    sd, total = _mk_sd(base, [(3, 150 * 1024 * 1024)])
    d1 = os.path.join(base, 'D1'); d2 = os.path.join(base, 'D2')
    os.makedirs(d1); os.makedirs(d2)
    w = _run_worker(sd, total, d1, d2, 'ego01_test')
    w.start()
    _wait(w)
    n1 = sum(len(f) for dp, dn, f in os.walk(d1))
    n2 = sum(len(f) for dp, dn, f in os.walk(d2))
    print("  D1=%d D2=%d" % (n1, n2))
    bad = _incomplete(d1, d2, sd)
    check("no incomplete files after overflow", not bad, "bad=%s" % bad)
    check("all 3 files transferred", n1 + n2 == 3, "got=%d" % (n1 + n2))


def scenario_big_files_on_small_disk():
    print("\n=== SCENARIO: big files on 48GB disk ===", flush=True)
    base = r'C:\sd_test_scatter2'
    sd, total = _mk_sd(base, [(1, 200 * 1024 * 1024), (1, 200 * 1024 * 1024),
                               (1, 200 * 1024 * 1024)])
    d1 = os.path.join(base, 'D1'); d2 = os.path.join(base, 'D2')
    os.makedirs(d1); os.makedirs(d2)
    w = _run_worker(sd, total, d1, d2, 'ego02_test')
    w.start()
    _wait(w)
    n1 = sum(len(f) for dp, dn, f in os.walk(d1))
    n2 = sum(len(f) for dp, dn, f in os.walk(d2))
    bad = _incomplete(d1, d2, sd)
    check("no incomplete files when primary almost full", not bad, "bad=%s" % bad)
    check("all 3 files transferred", n1 + n2 == 3, "total=%d" % (n1 + n2))


def scenario_copied_bytes_monotonic():
    print("\n=== SCENARIO: copied_bytes monotonic ===", flush=True)
    base = r'C:\sd_test_mono'
    sd, total = _mk_sd(base, [(5, 10 * 1024 * 1024)])
    d1 = os.path.join(base, 'D1'); d2 = os.path.join(base, 'D2')
    os.makedirs(d1); os.makedirs(d2)
    import sd_mover.main_window as mw
    import sd_mover.transfer_worker as tw

    def usage(p):
        return (1000 * GB, 900 * GB)
    mw.get_disk_usage = usage
    tw.get_disk_usage = usage
    w = TransferWorker(
        sd_info=fake_sd(sd, total), dest_roots=[('X', d1)],
        ego_num=3, prefix='ego', auto_eject=False, auto_delete=False,
        reader_serial="", test_mode=True, scatter_mode=False, primary_dest='X',
        folder_name='ego03_mono')
    seen = []
    w.progress_updated.connect(lambda *a: seen.append(a[1]))
    w.start()
    _wait(w)
    bad = [i for i in range(1, len(seen)) if seen[i] < seen[i - 1]]
    check("copied_bytes never decreases", not bad, "bad=%d" % len(bad))
    check("final copied == total", seen and seen[-1] == total,
          "final=%s" % (seen[-1] if seen else None))


def scenario_cancel_removes_partial_file():
    print("\n=== SCENARIO: cancel removes partial file ===", flush=True)
    base = r'C:\sd_test_cancel'
    sd, total = _mk_sd(base, [(1, 500 * 1024 * 1024)])
    d1 = os.path.join(base, 'D1'); d2 = os.path.join(base, 'D2')
    os.makedirs(d1); os.makedirs(d2)
    if os.path.isdir(d1):
        shutil.rmtree(d1, ignore_errors=True)
        os.makedirs(d1)
    import sd_mover.main_window as mw
    import sd_mover.transfer_worker as tw

    def usage(p):
        return (1000 * GB, 900 * GB)
    mw.get_disk_usage = usage
    tw.get_disk_usage = usage
    w = TransferWorker(
        sd_info=fake_sd(sd, total), dest_roots=[('X', d1)],
        ego_num=4, prefix='ego', auto_eject=False, auto_delete=False,
        reader_serial="", test_mode=True, scatter_mode=False, primary_dest='X',
        folder_name='ego04_cancel')
    w.start()
    time.sleep(1.0)
    w.cancel()
    _wait(w)
    # 取消时 _copy_file 的 except 分支会删除不完整的目标文件。
    # 真实不变量: 目标盘不允许残留"半个文件"。
    # (若拷贝在 cancel 生效前已完成, 文件是完整的, 不算残留)
    leftovers = []
    for dp, dn, fn in os.walk(d1):
        leftovers.extend(fn)
    partial = []
    for dp, dn, fn in os.walk(d1):
        for f in fn:
            fp = os.path.join(dp, f)
            srcp = os.path.join(sd, f)
            if os.path.exists(srcp) and os.path.getsize(fp) != os.path.getsize(srcp):
                partial.append((f, os.path.getsize(fp), os.path.getsize(srcp)))
    check("no partial files left after cancel", not partial,
          "partial=%s leftovers=%s" % (partial, leftovers))
    src_files = [f for f in os.listdir(sd)]
    check("SD source data preserved on cancel", len(src_files) == 1,
          "src=%s" % src_files)


def scenario_cancel_mid_copy_deletes_partial():
    print("\n=== SCENARIO: cancel mid-copy deletes partial file ===", flush=True)
    base = r'C:\sd_test_midcancel'
    sd, total = _mk_sd(base, [(1, 300 * 1024 * 1024)])
    d1 = os.path.join(base, 'D1')
    if os.path.isdir(d1):
        shutil.rmtree(d1, ignore_errors=True)
    os.makedirs(d1)
    import sd_mover.main_window as mw
    import sd_mover.transfer_worker as tw

    d1n = d1.replace('\\', '/')
    sdn = sd.replace('\\', '/')

    def usage(p):
        r = p.replace('\\', '/')
        if r.startswith(d1n):
            return (1000 * GB, 900 * GB)
        if r.startswith(sdn):
            return (1000 * GB, 1000 * GB - total)
        return (0, 0)
    mw.get_disk_usage = usage
    tw.get_disk_usage = usage

    w = TransferWorker(
        sd_info=fake_sd(sd, total), dest_roots=[('X', d1)],
        ego_num=5, prefix='ego', auto_eject=False, auto_delete=False,
        reader_serial="", test_mode=True, scatter_mode=False, primary_dest='X',
        folder_name='ego05_midcancel')
    w.start()
    # 轮询触发: 目标文件大小落在 10%~90% 之间时立刻取消,
    # 确保一定命中"写到一半"的时刻, 不依赖拷贝快慢。
    src_name = os.path.join(sd, os.listdir(sd)[0])
    src_size = os.path.getsize(src_name)
    cancel_at = None
    for _ in range(int(120 / 0.005)):
        if not w.isRunning():
            break
        hit = None
        for root, dn, fn in os.walk(d1n):
            for f in fn:
                fp = os.path.join(root, f)
                try:
                    sz = os.path.getsize(fp)
                except OSError:
                    continue
                if 0.1 * src_size < sz < 0.9 * src_size:
                    hit = (fp, sz)
                    break
            if hit:
                break
        if hit:
            cancel_at = hit
            w.cancel()
            break
        app.processEvents()
        time.sleep(0.005)
    _wait(w)

    leftovers = []
    partial = []
    for root, dn, fn in os.walk(d1n):
        for f in fn:
            leftovers.append(f)
            fp = os.path.join(root, f)
            srcp = os.path.join(sd, f)
            if os.path.exists(srcp) and os.path.getsize(fp) != os.path.getsize(srcp):
                partial.append((f, os.path.getsize(fp), os.path.getsize(srcp)))
    check("cancel landed mid-copy", cancel_at is not None, "at=%s" % (cancel_at,))
    check("no partial file left after mid-copy cancel", not partial,
          "partial=%s leftovers=%s" % (partial, leftovers))
    src_files = [f for f in os.listdir(sd)]
    check("SD source data preserved", len(src_files) == 1, "src=%s" % src_files)


if __name__ == '__main__':
    for fn in [scenario_overflow_spreads_across_disks,
               scenario_big_files_on_small_disk,
               scenario_copied_bytes_monotonic,
               scenario_cancel_removes_partial_file,
               scenario_cancel_mid_copy_deletes_partial]:
        try:
            fn()
        except Exception:
            traceback.print_exc()
            check(fn.__name__ + " (exception)", False, "see traceback")
    p = sum(1 for _, c, _ in results if c)
    f = len(results) - p
    print("\n===== %d passed, %d failed =====" % (p, f), flush=True)
    sys.exit(1 if f else 0)
