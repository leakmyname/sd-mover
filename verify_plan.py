# -*- coding: utf-8 -*-
"""验证 _plan_and_start 并发预占逻辑 (v11.12.11 回归)

覆盖用户报告的问题:
1. 配了同时写2块硬盘, 实测只写1块
2. R盘只剩48G, 476G的卡还在往里写
3. 打散模式先写到空间大的盘 (排序反了 -> 要"先填满小盘")
4. 多卡同时看到同一块盘的相同剩余空间而全部挤进去(过订阅)
"""
import os, sys, types
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

results = []
GB = 1024 ** 3


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print("[%s] %s %s" % ("PASS" if cond else "FAIL", name, detail), flush=True)


class FakeSpin:
    def __init__(self, v):
        self.v = v
    def value(self):
        return self.v


class FakeDrive:
    def __init__(self, letter, total, free):
        self.letter = letter
        self.total = total
        self.free = free
        self.root = letter + ":\\"
        self.label = "HDD"


class FakeRadio:
    def __init__(self, on):
        self.on = on
    def isChecked(self):
        return self.on


def install_fake_disk_usage(free_map):
    """main_window 顶层 import 了 get_disk_usage, 必须 monkeypatch 模块属性,
    否则 _plan_and_start 会去探测真实的 R:/O: 盘符(在 Linux 上直接抛异常)。"""
    import sd_mover.main_window as mw_mod

    def _fake(root):
        letter = root.strip('\\').rstrip(':').upper()
        if letter in free_map:
            t, f = free_map[letter]
            return t, f
        return 0, 0
    mw_mod.get_disk_usage = _fake
    return _fake


def make_win(free_map, max_sd=10, max_hd=2, scatter=True):
    """构造假的 MainWindow, 只挂 _plan_and_start 需要的属性"""
    import sd_mover.main_window as mw_mod
    install_fake_disk_usage(free_map)
    win = types.SimpleNamespace()
    win._ready_to_start = set()
    win.processing = set()
    win.waiting_set = set()
    win.pending_queue = []
    win.ext_drives = {}
    for l, (t, f) in free_map.items():
        win.ext_drives[l] = FakeDrive(l, t, f)
    win.sd_cards = {}
    win.workers = {}
    win.max_concurrent_sd_spin = FakeSpin(max_sd)
    win.max_concurrent_hd_spin = FakeSpin(max_hd)
    win.scatter_radio = FakeRadio(scatter)
    win.whole_radio = FakeRadio(not scatter)
    win._last_plans = {}
    win._card_folder_map = {}
    win._log_calls = []
    win._log = lambda m: win._log_calls.append(m)

    def _queue(d):
        win.waiting_set.add(d.letter)
        win.pending_queue.append(d)
    win._queue_sd = _queue
    win._started = []

    def _start(d, forced_primary=None):
        win._started.append((d.letter, forced_primary))
    win._start_transfer = _start
    win._plan_and_start = lambda: mw_mod.MainWindow._plan_and_start(win)
    return win


def card(letter, used_gb):
    d = types.SimpleNamespace()
    d.letter = letter
    d.total = 256 * GB
    d.free = d.total - int(used_gb * GB)
    d.root = letter + ":\\"
    return d


def scenario_small_disk_first():
    print("\n=== SCENARIO: 先填满小盘 (选盘排序) ===")
    win = make_win({'R': (931 * GB, 48 * GB),
                    'O': (931 * GB, 890 * GB)})
    win._ready_to_start.add('J')
    win.sd_cards['J'] = card('J', 238)
    win._plan_and_start()
    started = win._started
    check("card started", len(started) == 1, "started=%s" % started)
    check("scatter binds small disk first",
          started and started[0][1] == 'R',
          "primary=%s" % (started[0][1] if started else None))
    plan = win._last_plans.get('J', [])
    letters = [p[0] for p in plan]
    check("plan fills R then overflows to O", letters == ['R', 'O'],
          "plan=%s" % letters)
    check("R reserved ~its free space",
          plan and abs(plan[0][1] - 48 * GB) < 2 * GB,
          "reserved=%s" % (plan[0][1] if plan else None))


def scenario_two_disks_parallel():
    print("\n=== SCENARIO: 同时写2块硬盘 ===")
    win = make_win({'R': (931 * GB, 900 * GB),
                    'O': (931 * GB, 900 * GB)}, max_hd=2)
    win._ready_to_start.update(['J', 'K'])
    win.sd_cards['J'] = card('J', 238)
    win.sd_cards['K'] = card('K', 238)
    win._plan_and_start()
    primaries = {s[1] for s in win._started}
    check("2 cards started", len(win._started) == 2, "started=%s" % win._started)
    check("bound to 2 different disks", len(primaries) == 2,
          "primaries=%s" % primaries)


def scenario_no_oversubscribe():
    print("\n=== SCENARIO: 多卡不挤爆同一块盘 ===")
    win = make_win({'O': (931 * GB, 400 * GB)}, max_hd=2)
    for l in ('J', 'K', 'L'):
        win._ready_to_start.add(l)
        win.sd_cards[l] = card(l, 238)
    win._plan_and_start()
    check("only 1 card bound to 400G disk", len(win._started) == 1,
          "started=%s" % win._started)
    check("others queued not dropped", len(win.pending_queue) == 2,
          "queue=%d" % len(win.pending_queue))


def scenario_whole_mode_skip_small():
    print("\n=== SCENARIO: 整体模式 R盘48G 不放476G卡 ===")
    win = make_win({'R': (931 * GB, 48 * GB),
                    'O': (931 * GB, 890 * GB)}, scatter=False)
    win._ready_to_start.add('J')
    d = card('J', 238)
    d.total = 512 * GB
    d.free = d.total - 476 * GB
    win.sd_cards['J'] = d
    win._plan_and_start()
    check("whole mode started a card", len(win._started) == 1,
          "started=%s" % win._started)
    check("whole mode binds the big disk",
          win._started and win._started[0][1] == 'O',
          "primary=%s" % (win._started[0][1] if win._started else None))


def scenario_all_full_queues():
    print("\n=== SCENARIO: 所有盘都放不下 -> 排队不硬塞 ===")
    win = make_win({'R': (931 * GB, 10 * GB)}, scatter=False)
    win._ready_to_start.add('J')
    d = card('J', 238)
    d.total = 512 * GB
    d.free = d.total - 476 * GB
    win.sd_cards['J'] = d
    win._plan_and_start()
    check("nothing started when no disk can hold", len(win._started) == 0,
          "started=%s" % win._started)
    check("card queued waiting for new disk", len(win.pending_queue) == 1,
          "queue=%d" % len(win.pending_queue))


def scenario_hd_limit_respected():
    print("\n=== SCENARIO: max_hd=1 时只写一块盘 ===")
    win = make_win({'R': (931 * GB, 900 * GB),
                    'O': (931 * GB, 900 * GB)}, max_hd=1)
    for l in ('J', 'K'):
        win._ready_to_start.add(l)
        win.sd_cards[l] = card(l, 238)
    win._plan_and_start()
    primaries = {s[1] for s in win._started}
    check("max_hd=1 -> single disk", len(primaries) == 1,
          "primaries=%s" % primaries)
    check("second card queued", len(win.pending_queue) == 1,
          "queue=%d" % len(win.pending_queue))


def scenario_sd_limit_respected():
    print("\n=== SCENARIO: max_sd=2 时最多2张卡 ===")
    win = make_win({'R': (931 * GB, 900 * GB),
                    'O': (931 * GB, 900 * GB)}, max_sd=2, max_hd=10)
    for l in ('A', 'B', 'C', 'D'):
        win._ready_to_start.add(l)
        win.sd_cards[l] = card(l, 238)
    win._plan_and_start()
    check("max_sd=2 -> 2 cards started", len(win._started) == 2,
          "started=%s" % win._started)
    check("rest queued", len(win.pending_queue) == 2,
          "queue=%d" % len(win.pending_queue))


def scenario_reserved_accumulates():
    print("\n=== SCENARIO: 预占累计, 后续卡看到扣掉后的空间 ===")
    win = make_win({'R': (931 * GB, 500 * GB),
                    'O': (931 * GB, 500 * GB)}, max_hd=2)
    for l in ('J', 'K'):
        win._ready_to_start.add(l)
        win.sd_cards[l] = card(l, 238)
    win._plan_and_start()
    p1 = dict(win._last_plans.get('J', []))
    p2 = dict(win._last_plans.get('K', []))
    check("both cards got a plan", bool(p1) and bool(p2),
          "p1=%s p2=%s" % (p1, p2))
    sum_r = p1.get('R', 0) + p2.get('R', 0)
    check("R not over-reserved", sum_r <= 500 * GB, "sum_R=%d" % sum_r)
    check("no disk over-reserved across both cards",
          sum_r <= 500 * GB and
          (p1.get('O', 0) + p2.get('O', 0)) <= 500 * GB,
          "sum_R=%d sum_O=%d" % (sum_r, p1.get('O', 0) + p2.get('O', 0)))


if __name__ == '__main__':
    try:
        scenario_small_disk_first()
        scenario_two_disks_parallel()
        scenario_no_oversubscribe()
        scenario_whole_mode_skip_small()
        scenario_all_full_queues()
        scenario_hd_limit_respected()
        scenario_sd_limit_respected()
        scenario_reserved_accumulates()
    except Exception:
        import traceback
        traceback.print_exc()
        results.append(("exception", False, traceback.format_exc()))
    print("\n===== %d passed, %d failed =====" % (
        sum(1 for _, c, _ in results if c),
        sum(1 for _, c, _ in results if not c)))
    sys.exit(0 if all(c for _, c, _ in results) else 1)
