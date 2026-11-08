"""
Test: whole-file skip logic and scatter overflow (no Windows deps)
"""
import os
import sys

def test_whole_file_skip_small_target():
    """Target too small, whole-file mode should skip large files"""
    print("=== Test: whole_file_skip_small_target ===")
    QUOTA = 3 * 1024 * 1024  # 3MB
    files = [("big.bin", 50*1024*1024), ("small.bin", 1*1024*1024)]
    copied = 0
    skipped = 0
    for fname, fsize in files:
        if QUOTA >= fsize + 1024*1024:
            copied += fsize
        else:
            skipped += fsize
    assert copied == 1*1024*1024, f"Should copy 1MB, got {copied}"
    assert skipped == 50*1024*1024, f"Should skip 50MB, got {skipped}"
    print("  PASS")

def test_whole_file_all_fit():
    """All files fit, whole-file mode should copy all"""
    print("=== Test: whole_file_all_fit ===")
    quota = 100*1024*1024
    files = [("a.bin", 50*1024*1024), ("b.bin", 30*1024*1024)]
    copied = 0
    for fname, fsize in files:
        if quota >= fsize + 1024*1024:
            copied += fsize
            quota -= fsize
    assert copied == 80*1024*1024
    print("  PASS")

def test_scatter_fills_then_overflows():
    """Scatter mode: fill first disk, overflow to second"""
    print("=== Test: scatter_fills_then_overflows ===")
    QUOTA_A = 10*1024*1024
    QUOTA_B = 50*1024*1024
    files = [("f1.bin",5*1024*1024),("f2.bin",5*1024*1024),("f3.bin",5*1024*1024),("f4.bin",5*1024*1024)]
    dest_a = 0
    dest_b = 0
    for fname, fsize in files:
        free_a = QUOTA_A - dest_a
        free_b = QUOTA_B - dest_b
        if free_a >= fsize + 1024*1024:
            dest_a += fsize
        elif free_b >= fsize + 1024*1024:
            dest_b += fsize
    # QUOTA_A=10MB, each file=5MB+1MB buffer=6MB needed
    # f1: free_a=10MB >= 6MB -> A (used=5MB)
    # f2: free_a=5MB < 6MB  -> B (used=5MB)
    # f3: free_a=5MB < 6MB  -> B (used=10MB)
    # f4: free_a=5MB < 6MB  -> B (used=15MB)
    assert dest_a == 5*1024*1024, f"A should have 5MB, got {dest_a//1024//1024}MB"
    assert dest_b == 15*1024*1024, f"B should have 15MB, got {dest_b//1024//1024}MB"
    print("  PASS")

def test_whole_file_primary_only():
    """Whole-file mode: primary can't hold file -> skip, don't use other drives"""
    print("=== Test: whole_file_primary_only ===")
    primary_free = 48*1024*1024*1024  # 48GB
    other_free = 930*1024*1024*1024     # 930GB
    file_size = 476*1024*1024*1024     # 476GB
    scatter_mode = False
    primary_dest = "O"
    chosen = None
    all_dests = [("O", primary_free), ("M", other_free)]
    if not scatter_mode and primary_dest:
        for dl, free in all_dests:
            if dl != primary_dest:
                continue
            if free >= file_size + 1024*1024:
                chosen = dl
            break
    else:
        for dl, free in all_dests:
            if free >= file_size + 1024*1024:
                chosen = dl
                break
    assert chosen is None, f"Should skip, but chose {chosen}"
    print("  PASS")

if __name__ == '__main__':
    tests = [test_whole_file_skip_small_target, test_whole_file_all_fit,
             test_scatter_fills_then_overflows, test_whole_file_primary_only]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"  FAIL: {e}")
            failed += 1
    print(f"\n=== Results: {passed} passed, {failed} failed ===")
    sys.exit(1 if failed else 0)
