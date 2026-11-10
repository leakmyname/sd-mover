"""文件传输工作线程 - 每张SD卡绑定主目标盘, 写满才换下一块"""

import os
import time
import shutil
import threading
from datetime import datetime

from PyQt5.QtCore import QThread, pyqtSignal

from .winapi import format_size, eject_drive, get_disk_usage
from .constants import CHUNK_SIZE, SKIP_ITEMS, MAX_PATH_LEN


def _long_path(path):
    """将路径转换为 Windows 长路径格式 (\\\\?\\ 前缀), 绕过 MAX_PATH 260 限制。

    仅对绝对路径且未已有前缀时添加。\\??\\ 前缀也支持。
    """
    if not path:
        return path
    # 已经是长路径格式
    if path.startswith('\\\\?\\') or path.startswith('\\\\??\\'):
        return path
    # 只处理绝对路径 (如 C:\...)
    if len(path) >= 2 and path[1] == ':':
        # 规范化为绝对路径
        abs_path = os.path.abspath(path)
        return '\\\\?\\' + abs_path
    return path


def _is_device_error(exc):
    """判断异常是否为目标设备断开 (WinError 433 / 设备不存在 / Errno 22 写入失败)

    注意: USB设备断开过程中, Windows 可能先返回 Errno 22 (Invalid argument)
    而不是 WinError 433, 所以也需要捕获。但如果同时有路径过长的情况,
    会通过 _is_path_too_long 单独处理 (仅当 open 失败时)。
    """
    msg = str(exc)
    if '433' in msg or 'WinError' in msg:
        return True
    if '不存在' in msg or 'not ready' in msg.lower():
        return True
    # Errno 22 在写入/读取大文件时通常意味着设备已断开
    # (路径过长会被 _truncate_path 提前处理, 不会走到这里)
    if 'Errno 22' in msg:
        return True
    return False


def _is_path_too_long(exc):
    """判断异常是否为路径过长 (Errno 22 on open/makedirs)"""
    msg = str(exc)
    if 'Errno 22' in msg or 'Invalid argument' in msg:
        return True
    return False


def _truncate_path(rel_path, max_len):
    """截断过长的相对路径, 保留文件扩展名, 确保结果不超过 max_len。

    策略: 保留扩展名, 尽量保留第一层目录和文件名, 中间截断。
    """
    if not rel_path or len(rel_path) <= max_len:
        return rel_path
    # 分离扩展名
    root, ext = os.path.splitext(rel_path)
    parts = root.replace('/', '\\').split('\\')
    if not parts:
        return rel_path
    filename = parts[-1]
    first_dir = parts[0] if len(parts) > 1 else ''
    # 计算固定开销: ext + separator(如果有first_dir)
    if first_dir:
        fixed = len(first_dir) + 1 + len(ext)
    else:
        fixed = len(ext)
    available = max_len - fixed
    if available < 10:
        # first_dir太长, 也截断first_dir
        if first_dir:
            first_dir = first_dir[:max(5, (max_len - len(ext) - 10) // 3)]
            fixed = len(first_dir) + 1 + len(ext)
            available = max_len - fixed
        if available < 10:
            available = 10
    # 截断文件名以适应可用空间
    if len(filename) > available:
        filename = filename[:max(1, available - 6)] + '_trunc'
    result = (first_dir + '\\' + filename + ext) if first_dir else (filename + ext)
    # 最终安全检查: 如果还是太长, 暴力截断
    if len(result) > max_len:
        # 保留扩展名, 截断中间
        keep_ext = ext if len(ext) < max_len - 5 else ext[:3]
        result = result[:max_len - len(keep_ext)] + keep_ext
    return result


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
                scatter_mode=True, primary_dest=""):
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
        self.primary_dest = primary_dest  # 主绑定盘符(空则自动选)
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

            # Build dest list; primary_dest first (bind SD card to one drive),
            # then others by free space descending as fallback
            all_dests = []
            for dl, dr in self.dest_roots:
                try:
                    _, free = get_disk_usage(dr)
                    all_dests.append((dl, dr, free))
                except Exception:
                    pass
            # Primary dest goes first so this SD card writes to its bound drive
            # until full, then falls back to others
            if self.primary_dest:
                all_dests.sort(key=lambda x: (0 if x[0] == self.primary_dest else 1, x[2]))
            else:
                all_dests.sort(key=lambda x: x[2])
            if all_dests:
                pdesc = f" (绑定 {self.primary_dest})" if self.primary_dest else ""
                self.log_message.emit(
                    f"[{letter}] 目标盘{pdesc}: "
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

                # Collect newly added dests and re-sort so primary_dest leads
                with self._dest_lock:
                    if self._new_dests:
                        for dl, dr in self._new_dests:
                            _, free = get_disk_usage(dr)
                            all_dests.append((dl, dr, free))
                            self.log_message.emit(
                                f"[{self.sd_info.letter}] 新增目标盘 {dl}: (剩余 {format_size(free)})"
                            )
                        self._new_dests.clear()
                        # Re-sort: primary_dest first, then by free desc
                        if self.primary_dest:
                            all_dests.sort(key=lambda x: (0 if x[0] == self.primary_dest else 1, x[2]))
                        else:
                            all_dests.sort(key=lambda x: x[2])

                fpath, fsize = files[file_idx]
                chosen = None

                # ── 选盘逻辑 ──
                # 整体文件模式: 只用主盘(primary_dest), 放不下就跳过
                # 打散模式: 先写主盘, 满了溢出到其他盘
                # 先过滤掉已断开的目标盘 (get_disk_usage 失败=设备掉线)
                alive_dests = []
                disconnected = []
                for dl, dr, _ in all_dests:
                    try:
                        total, free = get_disk_usage(dr)
                        # total=0 means drive not ready / doesn't exist
                        if total == 0:
                            disconnected.append(dl)
                        else:
                            alive_dests.append((dl, dr, free))
                    except Exception:
                        disconnected.append(dl)
                if disconnected:
                    for dl in disconnected:
                        self.log_message.emit(
                            f"[{letter}] 目标盘 {dl} 已断开, 从列表移除"
                        )
                    all_dests = alive_dests
                    if not all_dests:
                        self.log_message.emit(
                            f"[{letter}] [严重] 所有目标盘已断开! 已传输 {format_size(copied)}"
                        )
                        self.status_changed.emit(letter, "设备断开")
                        self.transfer_failed.emit(letter, "所有目标盘断开")
                        return
                    # Re-sort
                    if self.primary_dest:
                        all_dests.sort(key=lambda x: (0 if x[0] == self.primary_dest else 1, x[2]))
                    else:
                        all_dests.sort(key=lambda x: x[2])
                else:
                    all_dests = alive_dests
                # 现在用 alive dests 的缓存 free 值, 不再重复调 get_disk_usage
                if not self.scatter_mode and self.primary_dest:
                    # 整体文件: 只检查主盘
                    for i in range(len(all_dests)):
                        dl, dr, free = all_dests[i]
                        if dl != self.primary_dest:
                            continue
                        if free >= fsize + 1024 * 1024:
                            chosen = i
                        break
                else:
                    # 打散: 先主盘, 满了再其他盘
                    for i in range(len(all_dests)):
                        dl, dr, free = all_dests[i]
                        if free >= fsize + 1024 * 1024:
                            chosen = i
                            break

                if chosen is None:
                    if not self.scatter_mode:
                        # 整体文件模式: 主盘放不下, 跳过此文件
                        pfree_str = "未知"
                        for dl, dr, _free in all_dests:
                            if dl == self.primary_dest:
                                pfree_str = format_size(_free)
                                break
                        self.log_message.emit(
                            f"[{letter}] 跳过(主盘{self.primary_dest}放不下): "
                            f"{os.path.basename(fpath)} ({format_size(fsize)}, "
                            f"主盘剩余{pfree_str})"
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
                # 检查目标路径长度, 如果太长则截断
                base_dest_len = len(current_dest_path)
                rel_len = len(rel)
                if base_dest_len + 1 + rel_len > MAX_PATH_LEN:
                    # 需要截断: 保留扩展名和关键信息
                    available = MAX_PATH_LEN - base_dest_len - 1
                    if available < 20:
                        available = 20
                    rel = _truncate_path(rel, available)
                    self.log_message.emit(
                        f"[{letter}] 路径过长, 已截断: {os.path.basename(fpath)}"
                    )
                dst_path = os.path.join(current_dest_path, rel)
                fname = os.path.basename(fpath)
                try:
                    os.makedirs(_long_path(os.path.dirname(dst_path)), exist_ok=True)
                    copied = self._copy_file(fpath, dst_path, copied, total_size, fname, fsize)
                    copied_files.add(fpath)
                    file_idx += 1
                except Exception as e:
                    errors.append((fpath, str(e)))
                    if _is_device_error(e):
                        # 目标盘断开, 立即停止传输
                        self.log_message.emit(
                            f"[{letter}] [严重] 目标盘 {dest_letter} 已断开! "
                            f"已传输 {format_size(copied)}"
                        )
                        self.status_changed.emit(letter, "设备断开")
                        self.transfer_failed.emit(letter, f"目标盘 {dest_letter} 断开")
                        return
                    elif _is_path_too_long(e):
                        self.log_message.emit(
                            f"[{letter}] 路径过长, 跳过: {fname} "
                            f"(路径 {len(dst_path)} 字符)"
                        )
                    else:
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
        # 使用长路径前缀绕过 Windows MAX_PATH 260 限制
        src_lp = _long_path(src)
        dst_lp = _long_path(dst)
        try:
            fsrc = open(src_lp, 'rb')
        except OSError as e:
            raise OSError(f"无法读取源文件: {fname} - {e}")
        try:
            fdst = open(dst_lp, 'wb')
        except OSError as e:
            fsrc.close()
            raise OSError(f"无法创建目标文件: {fname} - {e}")
        try:
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
            fdst.flush()
            os.fsync(fdst.fileno())
        except OSError:
            # 写入失败(设备断开等), 删除不完整的目标文件
            try:
                fdst.close()
            except Exception:
                pass
            try:
                fsrc.close()
            except Exception:
                pass
            try:
                os.remove(dst_lp)
            except Exception:
                pass
            raise
        finally:
            try:
                fdst.close()
            except Exception:
                pass
            try:
                fsrc.close()
            except Exception:
                pass
        try:
            shutil.copystat(src_lp, dst_lp)
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
