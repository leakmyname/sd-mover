"""文件传输工作线程 - 多硬盘流水线: 先填满快满的盘, 满了等新盘"""

import os
import time
import shutil
import threading
from datetime import datetime

from PyQt5.QtCore import QThread, pyqtSignal

from .winapi import format_size, eject_drive, get_disk_usage
from .constants import CHUNK_SIZE, SKIP_ITEMS


class TransferWorker(QThread):
    progress_updated = pyqtSignal(str, int, int, str, float, int, int)
    status_changed = pyqtSignal(str, str)
    transfer_completed = pyqtSignal(str, str, str, int, int)
    transfer_failed = pyqtSignal(str, str)
    log_message = pyqtSignal(str)
    dest_changed = pyqtSignal(str, str)
    # 盘符, 剩余文件数, 剩余字节数 — 所有盘满了, 请求UI弹窗等新盘
    need_space = pyqtSignal(str, int, int)

    def __init__(self, sd_info, dest_roots, ego_num, prefix,
                 auto_eject=True, auto_delete=True,
                 reader_serial="", test_mode=False):
        super().__init__()
        self.sd_info = sd_info
        self.dest_roots = dest_roots  # [(letter, path), ...]
        self.ego_num = ego_num
        self.prefix = prefix or 'ego'
        self.auto_eject = auto_eject
        self.auto_delete = auto_delete
        self.reader_serial = reader_serial
        self.test_mode = test_mode
        self._cancel = False
        self._lock = threading.Lock()
        self._last_pct10 = -1
        self._last_file_pct10 = -1
        self._start_time = 0.0
        # 等待新硬盘的同步机制
        self._space_event = threading.Event()
        self._give_up_flag = False
        self._new_dests = []
        self._dest_lock = threading.Lock()

    def cancel(self):
        with self._lock:
            self._cancel = True
        self._space_event.set()  # 唤醒可能在等待的线程

    @property
    def is_cancelled(self):
        with self._lock:
            return self._cancel

    def add_dest(self, letter, root):
        """UI调用: 新硬盘已插入, 添加目标盘并唤醒worker"""
        with self._dest_lock:
            self._new_dests.append((letter, root))
        self._space_event.set()

    def give_up(self):
        """UI调用: 用户点忽略, 放弃等待新盘"""
        self._give_up_flag = True
        self._space_event.set()

    def run(self):
        letter = self.sd_info.letter
        src_root = self.sd_info.root
        try:
            self.status_changed.emit(letter, "扫描文件...")
            files = []
            total_size = 0
            for dirpath, dirnames, filenames in os.walk(src_root):
                if self.is_cancelled:
                    self._cancelled(letter)
                    return
                dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_ITEMS]
                for fname in filenames:
                    if fname.lower() in SKIP_ITEMS:
                        continue
                    fpath = os.path.join(dirpath, fname)
                    try:
                        fsize = os.path.getsize(fpath)
                        files.append((fpath, fsize))
                        total_size += fsize
                    except OSError:
                        pass

            if not files:
                self.log_message.emit(f"[{letter}] SD卡中没有数据")
                self.status_changed.emit(letter, "无数据")
                if self.auto_eject:
                    self._do_eject(letter)
                self.transfer_completed.emit(letter, "", "", 0, 0)
                return

            self.log_message.emit(
                f"[{letter}] 共 {len(files)} 个文件, {format_size(total_size)}"
            )

            # 按 remaining space 升序: 先填快满的盘, 像流水线
            all_dests = []
            for dl, dr in self.dest_roots:
                _, free = get_disk_usage(dr)
                all_dests.append((dl, dr, free))
            all_dests.sort(key=lambda x: x[2])  # 最少剩余的排前面
            self.log_message.emit(
                f"[{letter}] 目标盘顺序(剩余空间升序): "
                + ", ".join(f"{dl}:{format_size(fr)}" for dl, _, fr in all_dests)
            )

            copied = 0
            errors = []
            file_idx = 0
            copied_files = set()  # 已成功传输的源文件路径
            current_dest_letter = ""
            current_dest_path = ""
            current_folder = None
            dest_idx = 0

            self.status_changed.emit(letter, "传输中")
            self._start_time = time.time()
            self._last_pct10 = -1
            self._last_file_pct10 = -1
            self.progress_updated.emit(letter, 0, total_size, "准备传输...", 0, 0, 0)

            while file_idx < len(files):
                if self.is_cancelled:
                    self._cancelled(letter, copied)
                    return

                # 收集新添加的目标盘 (追加到末尾, 不重新排序, 保持流水线)
                with self._dest_lock:
                    if self._new_dests:
                        for dl, dr in self._new_dests:
                            _, free = get_disk_usage(dr)
                            all_dests.append((dl, dr, free))
                            self.log_message.emit(
                                f"[{self.sd_info.letter}] 新增目标盘 {dl}: (剩余 {format_size(free)})"
                            )
                        self._new_dests.clear()

                # 找一个能放下当前文件的目标盘
                fpath, fsize = files[file_idx]
                chosen = None
                # 先从当前盘往后找 (流水线顺序)
                for i in range(dest_idx, len(all_dests)):
                    dl, dr, _ = all_dests[i]
                    _, free = get_disk_usage(dr)
                    if free >= fsize + 1024 * 1024:
                        chosen = i
                        break
                # 当前及后续盘都放不下, 回头检查前面的盘 (避免浪费空间)
                if chosen is None:
                    for i in range(0, dest_idx):
                        dl, dr, _ = all_dests[i]
                        _, free = get_disk_usage(dr)
                        if free >= fsize + 1024 * 1024:
                            chosen = i
                            break

                if chosen is None:
                    # 所有盘都放不下当前文件, 请求新盘
                    remainingF = len(files) - file_idx
                    remainingB = sum(f[1] for f in files[file_idx:])
                    self.status_changed.emit(letter, "等待新硬盘...")
                    self.log_message.emit(
                        f"[{letter}] 所有目标盘已满! 剩余 {remainingF} 个文件 "
                        f"({format_size(remainingB)}), 等待新硬盘..."
                    )
                    self.need_space.emit(letter, remainingF, remainingB)
                    self._space_event.wait()
                    self._space_event.clear()
                    if self._give_up_flag:
                        # 用户点忽略: 只删已传输的文件, 保留未传输的
                        self._partial_cleanup(letter, copied_files)
                        self.transfer_failed.emit(letter, "GAVE_UP")
                        return
                    if self.is_cancelled:
                        self._cancelled(letter, copied)
                        return
                    # 新盘已添加, 继续循环重试
                    continue

                dest_idx = chosen
                dest_letter, dest_root, _ = all_dests[dest_idx]

                # 换盘时创建新文件夹
                if dest_letter != current_dest_letter:
                    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                    folder = f"{self.prefix}{self.ego_num:02d}_{ts}"
                    dest = os.path.join(dest_root, folder)
                    if os.path.exists(dest):
                        dest = os.path.join(dest_root, f"{folder}_{int(time.time())}")
                    os.makedirs(dest, exist_ok=True)
                    current_dest_letter = dest_letter
                    current_dest_path = dest
                    self.dest_changed.emit(letter, dest_letter)
                    self.log_message.emit(
                        f"[{letter}] 写入 -> {dest_letter}: {dest}"
                    )

                # 拷贝文件
                rel = os.path.relpath(fpath, src_root)
                dst_path = os.path.join(current_dest_path, rel)
                os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                fname = os.path.basename(fpath)
                try:
                    copied = self._copy_file(fpath, dst_path, copied, total_size, fname, fsize)
                    copied_files.add(fpath)
                    file_idx += 1
                except Exception as e:
                    errors.append((fpath, str(e)))
                    self.log_message.emit(f"[{letter}] 错误: {fname} - {e}")
                    file_idx += 1

            self.progress_updated.emit(letter, copied, total_size, "", 0, 0, 0)

            all_ok = (file_idx >= len(files)) and not errors
            if all_ok:
                self.log_message.emit(f"[{letter}] 传输完成, 共 {format_size(copied)}")
            elif errors:
                self.log_message.emit(f"[{letter}] 完成, {len(errors)} 个错误")

            # 只有全部文件都成功传输才清空SD卡
            if all_ok and self.auto_delete and not self.is_cancelled and not self.test_mode:
                self.status_changed.emit(letter, "清空SD卡...")
                self._clear_sd(src_root, letter)
            elif all_ok and self.test_mode:
                self.log_message.emit(f"[{letter}] 测试模式: 跳过删除SD卡文件")

            if self.auto_eject and not self.is_cancelled and not self.test_mode:
                self._do_eject(letter)
            elif self.test_mode:
                self.log_message.emit(f"[{letter}] 测试模式: 跳过弹出SD卡")

            self.transfer_completed.emit(
                letter, current_dest_letter, current_dest_path, copied, len(files)
            )
        except Exception as e:
            self.transfer_failed.emit(letter, str(e))
            self.log_message.emit(f"[{letter}] 传输失败: {e}")

    def _copy_file(self, src, dst, offset, total, fname, file_size):
        with open(src, 'rb') as fsrc, open(dst, 'wb') as fdst:
            file_copied = 0
            while True:
                if self.is_cancelled:
                    raise RuntimeError("Cancelled")
                chunk = fsrc.read(CHUNK_SIZE)
                if not chunk:
                    break
                fdst.write(chunk)
                offset += len(chunk)
                file_copied += len(chunk)
                pct10 = int(offset / total * 1000) if total else 0
                if pct10 != self._last_pct10:
                    self._last_pct10 = pct10
                    elapsed = time.time() - self._start_time
                    speed = offset / elapsed / (1024 * 1024) if elapsed > 0 else 0
                    self.progress_updated.emit(
                        self.sd_info.letter, offset, total, fname, speed,
                        file_copied, file_size
                    )
        try:
            shutil.copystat(src, dst)
        except Exception:
            pass
        return offset

    def _partial_cleanup(self, letter, copied_files):
        """只删除已成功传输的文件, 保留未传输的"""
        self.status_changed.emit(letter, "清理已传输文件...")
        count = 0
        for fpath in copied_files:
            if self.is_cancelled:
                break
            try:
                os.remove(fpath)
                count += 1
            except Exception:
                pass
        # 删除空目录
        for dirpath, dirnames, filenames in os.walk(self.sd_info.root, topdown=False):
            if self.is_cancelled:
                break
            if not filenames and not dirnames:
                basename = os.path.basename(dirpath)
                if basename.lower() not in SKIP_ITEMS:
                    try:
                        os.rmdir(dirpath)
                    except Exception:
                        pass
        self.log_message.emit(
            f"[{letter}] 已清理 {count} 个已传输文件, 保留未传输文件"
        )

    def _clear_sd(self, root, letter):
        for item in os.listdir(root):
            if self.is_cancelled:
                break
            if item.lower() in SKIP_ITEMS:
                continue
            p = os.path.join(root, item)
            try:
                if os.path.isfile(p) or os.path.islink(p):
                    os.remove(p)
                elif os.path.isdir(p):
                    shutil.rmtree(p)
            except Exception as e:
                self.log_message.emit(f"[{letter}] 删除失败: {item} - {e}")
        self.log_message.emit(f"[{letter}] SD卡已清空")

    def _do_eject(self, letter):
        self.status_changed.emit(letter, "弹出中...")
        time.sleep(0.5)
        ok = eject_drive(letter)
        if ok:
            reader_sn = self.reader_serial or "未知"
            self.log_message.emit(f"[{letter}] SD卡已弹出 (读卡器SN: {reader_sn})")
            self.status_changed.emit(letter, "已弹出")
        else:
            reader_sn = self.reader_serial or "未知"
            self.log_message.emit(f"[{letter}] 弹出失败, 请手动弹出 (读卡器SN: {reader_sn})")
            self.status_changed.emit(letter, "弹出失败")

    def _cancelled(self, letter, copied=0):
        self.status_changed.emit(letter, "已取消")
        self.log_message.emit(f"[{letter}] 已取消 (已复制 {format_size(copied)})")
        self.transfer_failed.emit(letter, "CANCELLED")
