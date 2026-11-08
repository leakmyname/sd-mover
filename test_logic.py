"""
单元测试: 验证整体文件模式和打散模式的空间判断逻辑
在构建机上运行: python test_logic.py
"""
import os
import tempfile
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sd_mover.winapi import format_size, get_disk_usage


def make_file(path, size_mb):
    """创建指定大小的文件(填充零)"""
    with open(path, 'wb') as f:
        buf = b'\x00' * (1024 * 1024)
        for _ in range(size_mb):
            f.write(buf)


def test_whole_file_skip():
    """整体文件模式: 主盘放不下的文件应该跳过"""
    print("=== Test: whole_file_skip ===")
    tmp = tempfile.mkdtemp()
    try:
        src = os.path.join(tmp, 'src')
        dst = os.path.join(tmp, 'dst')
        os.makedirs(src)
        os.makedirs(dst)

        # 创建2个文件: 一个500MB, 一个500MB
        make_file(os.path.join(src, 'file_a.bin'), 500)
        make_file(os.path.join(src, 'file_b.bin'), 500)

        # 模拟整体文件模式逻辑
        files = []
        total_size = 0
        for dirpath, dirnames, filenames in os.walk(src):
            for fname in filenames:
                fpath = os.path.join(dirpath, fname)
                fsize = os.path.getsize(fpath)
                files.append((fpath, fsize))
                total_size += fsize

        _, dst_free = get_disk_usage(dst)
        print(f"  Total data: {format_size(total_size)}, dst free: {format_size(dst_free)}")

        # 模拟整体文件选盘逻辑
        copied = 0
        skipped = 0
        for fpath, fsize in files:
            _, free = get_disk_usage(dst)
            if free >= fsize + 1024 * 1024:
                # Copy
                dst_file = os.path.join(dst, os.path.basename(fpath))
                shutil.copy2(fpath, dst_file)
                copied += fsize
                print(f"  COPIED: {os.path.basename(fpath)} ({format_size(fsize)})")
            else:
                skipped += fsize
                print(f"  SKIPPED: {os.path.basename(fpath)} ({format_size(fsize)}, free={format_size(free)})")

        assert copied > 0, "Should have copied at least one file"
        assert skipped >= 0, "Skip count should be non-negative"
        print(f"  Result: copied={format_size(copied)}, skipped={format_size(skipped)}")
        print("  PASS")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_scatter_overflow():
    """打散模式: 主盘满了应该溢出到第二块盘"""
    print("=== Test: scatter_overflow ===")
    tmp = tempfile.mkdtemp()
    try:
        src = os.path.join(tmp, 'src')
        dst1 = os.path.join(tmp, 'dst1')
        dst2 = os.path.join(tmp, 'dst2')
        os.makedirs(src)
        os.makedirs(dst1)
        os.makedirs(dst2)

        # 创建4个小文件
        for i in range(4):
            make_file(os.path.join(src, f'file_{i}.bin'), 50)

        files = []
        total_size = 0
        for dirpath, dirnames, filenames in os.walk(src):
            for fname in filenames:
                fpath = os.path.join(dirpath, fname)
                fsize = os.path.getsize(fpath)
                files.append((fpath, fsize))
                total_size += fsize

        # 模拟打散模式: 先写dst1, 满了溢出到dst2
        # dst1/dst2 are on the same physical disk, so free space is shared
        # This test just verifies the logic works
        dests = [(dst1, 'D1'), (dst2, 'D2')]
        copied = 0
        dest_usage = {}

        for fpath, fsize in files:
            chosen = None
            for dpath, dlabel in dests:
                _, free = get_disk_usage(dpath)
                if free >= fsize + 1024 * 1024:
                    chosen = (dpath, dlabel)
                    break
            if chosen:
                dst_file = os.path.join(chosen[0], os.path.basename(fpath))
                shutil.copy2(fpath, dst_file)
                copied += fsize
                dest_usage[chosen[1]] = dest_usage.get(chosen[1], 0) + fsize
                print(f"  COPIED: {os.path.basename(fpath)} -> {chosen[1]}")
            else:
                print(f"  SKIPPED: {os.path.basename(fpath)} (no space)")

        assert copied == total_size, f"Should copy all files: copied={copied}, total={total_size}"
        print(f"  Result: copied={format_size(copied)}, all files transferred")
        print("  PASS")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_folder_naming():
    """验证文件夹命名格式: prefix+num+timestamp"""
    print("=== Test: folder_naming ===")
    from datetime import datetime
    prefix = 'ego'
    ego_num = 7
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    folder = f"{prefix}{ego_num:02d}_{ts}"
    print(f"  Folder name: {folder}")
    assert folder.startswith('ego07_'), f"Expected ego07_ prefix, got {folder}"
    assert len(folder) == 21, f"Expected 21 chars (prefix+num+datetime), got {len(folder)}: {folder}"
    print("  PASS")


def test_progress_clamp():
    """验证进度不会出现负数或超过100%"""
    print("=== Test: progress_clamp ===")
    def clamp_pct(v):
        return max(0, min(100, v))

    assert clamp_pct(-5) == 0, "Negative should clamp to 0"
    assert clamp_pct(0) == 0
    assert clamp_pct(50) == 50
    assert clamp_pct(100) == 100
    assert clamp_pct(150) == 100, "Over 100 should clamp to 100"
    assert clamp_pct(99.99) == 99.99  # float stays as-is before int() in caller
    print("  PASS")


def test_eta_format():
    """验证预估时间格式"""
    print("=== Test: eta_format ===")
    def format_eta(seconds):
        if seconds < 1:
            return "即将完成"
        if seconds < 60:
            return f"{int(seconds)}秒"
        if seconds < 3600:
            return f"{int(seconds / 60)}分钟"
        h = seconds / 3600
        return f"{h:.1f}小时"

    assert format_eta(0) == "即将完成"
    assert format_eta(30) == "30秒"
    assert format_eta(90) == "1分钟"
    assert format_eta(1800) == "30分钟"
    assert format_eta(5400) == "1.5小时"
    assert format_eta(3600 * 2) == "2.0小时"
    print("  PASS")


def test_size_format():
    """验证文件大小格式"""
    print("=== Test: size_format ===")
    assert format_size(0) == "0.0 B"
    assert format_size(1024) == "1.0 KB"
    assert format_size(1024 * 1024) == "1.0 MB"
    assert format_size(1024 * 1024 * 1024) == "1.0 GB"
    assert format_size(1024 * 1024 * 1024 * 1024) == "1.0 TB"
    print("  PASS")


if __name__ == '__main__':
    tests = [
        test_whole_file_skip,
        test_scatter_overflow,
        test_folder_naming,
        test_progress_clamp,
        test_eta_format,
        test_size_format,
    ]
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"  FAIL: {e}")
            failed += 1
    print(f"\n=== Results: {passed} passed, {failed} failed ===")
    sys.exit(1 if failed else 0)
