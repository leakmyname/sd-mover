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
    # letter, copied, total, fname, speed, file_copied, file_total
    progress_updated = pyqtSignal(str, float, float, str, float, float, float)
    status_changed = pyqtSignal(str, str)
    transfer_completed = pyqtSignal(str, str, str, float, int)
    transfer_failed = pyqtSignal(str, str)
    log_message = pyqtSignal(str)
    dest_changed = pyqtSignal(str, str)
    need_space = pyqtSignal(str, int, float)

    def __init__(self, sd_info, dest_roots, ego_num, prefix,
                auto_eject=True, auto_delete=True,
                reader_serial="", test_mode=False,
                scatter_mode=True):
        super().__init__()
        self.sd_info = sd_info
        self.dest_roots = dest_roots  # [(letter, path), ...]
        self.ego_num = ego_num
        self.prefix = prefix or 'ego'
        self.auto_eject = auto_eject
        self.auto_delete = auto_delete
        self.reader_serial = reader_serial
        self.test_mode = test_mode
        self.scatter_mode = scatter_mode
        self._cancel = False
        self._lock = threading.Lock()
        self._last_pct10 = -1
        self._start_time = 0.0
        self._last_emit_time = 0.0
        self._space_event = threading.Event()
        self._give_up_flag = False
        self._new_dests = []
        self._dest_lock = threading.Lock()
        self._folder_name = ""  # consistent folder name per SD card

    def cancel(self):
        with self._lock:
            self._cancel = True
        self._space_event.set()

    @property
    def is_cancelled(self):
        with self._lock:
            return self._cancel

    def add_dest(self, letter, root):
        with self._dest_lock:
            self._new_dests.append((letter, root))
        self._space_event.set()

    def give_up(self):
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
                if self.auto_eject and not self.test_mode:
                    self._do_eject(letter)
                self.transfer_completed.emit(letter, "", "", 0, 0)
                return

            self.log_message.emit(
                f"[{letter}] 共 {len(files)} 个文件, {format_size(total_size)}"
            )

            # Build dest list sorted by free space ascending (fill fullest first)
            all_dests = []
            for dl, dr in self.dest_roots:
                try:
                    _, free = get_disk_usage(dr)
                    all_dests.append((dl, dr, free))
                except Exception:
                    pass
            # Sort by free space DESCENDING (most free first)
            # This distributes files across drives instead of filling one at a time
            all_dests.sort(key=lambda x: x[2], reverse=True)
            if all_dests:
                self.log_message.emit(
                    f"[{letter}] 目标盘: "
                    + ", ".join(f"{dl}:{format_size(fr)}" for dl, _, fr in all_dests)
                )

            copied = 0
            errors = []
            file_idx = 0
            copied_files = set()
            current_dest_letter = ""
            current_dest_path = ""
            dest_idx = 0

            self.status_changed.emit(letter, "传输中")
            self._start_time = time.time()
            self._last_pct10 = -1
            self._last_emit_time = time.time()
            self.progress_updated.emit(letter, 0, total_size, "准备传输...", 0, 0, 0)

            while file_idx < len(files):
                if self.is_cancelled:
                    self._cancelled(letter, copied)
                    return

                # Collect newly added dests
                with self._dest_lock:
                    if self._new_dests:
                        for dl, dr in self._new_dests:
                            _, free = get_disk_usage(dr)
                            all_dests.append((dl, dr, free))
                            self.log_message.emit(
                                f"[{self.sd_info.letter}] 新增目标盘 {dl}: (剩余 {format_size(free)})"
                            )
                        self._new_dests.clear()

                fpath, fsize = files[file_idx]
                chosen = None

                # Find the dest with MOST free space that can fit this file
                # This distributes files across multiple drives naturally
                best_free = -1
                for i in range(len(all_dests)):
                    dl, dr, _ = all_dests[i]
                    _, free = get_disk_usage(dr)
                    if free >= fsize + 1024 * 1024 and free > best_free:
                        chosen = i
                        best_free = free

                if chosen is None:
                    if not self.scatter_mode:
                        # Whole-file mode: skip files that don't fit anywhere
                        self.log_message.emit(
                            f"[{letter}] 跳过文件(无盘可容): {os.path.basename(fpath)} "
                            f"({format_size(fsize)})"
                        )
                        file_idx += 1
                        continue
                    # Scatter mode: all dests full, request new disk
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
                        self._partial_cleanup(letter, copied_files)
                        self.transfer_failed.emit(letter, "GAVE_UP")
                        return
                    if self.is_cancelled:
                        self._cancelled(letter, copied)
                        return
                    continue

                dest_idx = chosen
                dest_letter, dest_root, _ = all_dests[dest_idx]

                # Use consistent folder name per SD card across all dest drives
                # so files from same SD card are easy to find
                if not hasattr(self, '_folder_name') or not self._folder_name:
                    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                    self._folder_name = f"{self.prefix}{self.ego_num:02d}_{ts}"
                if dest_letter != current_dest_letter:
                    folder = self._folder_name
                    dest = os.path.join(dest_root, folder)
                    os.makedirs(dest, exist_ok=True)
                    current_dest_letter = dest_letter
                    current_dest_path = dest
                    self.dest_changed.emit(letter, dest_letter)
                    self.log_message.emit(
                        f"[{letter}] 写入 -> {dest_letter}: {dest}"
                    )

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

            skipped = total_size - copied
            # all_ok: 所有文件都传输成功, 没有跳过, 没有错误
            # 如果整体文件模式下有跳过(skipped>0), 不能清空SD卡(有文件没传过去)
            all_ok = (file_idx >= len(files)) and not errors and skipped == 0
            # partial_ok: 传完了但有些文件跳过/放不下, 不删除SD卡
            partial_ok = (file_idx >= len(files)) and not errors and skipped > 0
            if all_ok:
                self.log_message.emit(f"[{letter}] 传输完成, 共 {format_size(copied)}")
            elif partial_ok:
                self.log_message.emit(
                    f"[{letter}] 传输完成, 已传 {format_size(copied)}, "
                    f"跳过 {format_size(skipped)}(放不下), 未删除SD卡"
                )
            elif errors:
                self.log_message.emit(f"[{letter}] 完成, {len(errors)} 个错误")

            # 只有all_ok才清空SD卡; partial_ok不删除(有文件没传)
            if all_ok and self.auto_delete and not self.is_cancelled and not self.test_mode:
                self.status_changed.emit(letter, "清空SD卡...")
                self._clear_sd(src_root, letter)
            elif all_ok and self.test_mode:
                self.log_message.emit(f"[{letter}] 测试模式: 跳过删除SD卡文件")
            elif partial_ok:
                self.log_message.emit(f"[{letter}] 有跳过文件, 保留SD卡数据")

            # all_ok才弹出; partial_ok不弹出(保留数据让用户处理)
            if all_ok and self.auto_eject and not self.is_cancelled and not self.test_mode:
                self._do_eject(letter)
            elif all_ok and self.test_mode:
                self.log_message.emit(f"[{letter}] 测试模式: 跳过弹出SD卡")
            elif partial_ok:
                self.log_message.emit(f"[{letter}] 有跳过文件, 不弹出SD卡")

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
                now = time.time()
                elapsed = now - self._start_time
                speed = offset / elapsed / (1024 * 1024) if elapsed > 0 else 0
                pct10 = int(offset / total * 1000) if total else 0
                if pct10 != self._last_pct10 or (now - self._last_emit_time) > 0.5:
                    self._last_pct10 = pct10
                    self._last_emit_time = now
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
