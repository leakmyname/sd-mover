"""主窗口 - PyQt5 GUI (多硬盘版)"""

import os
import sys
import re
import json
from datetime import datetime
from typing import Dict, Set, Optional

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QPushButton, QProgressBar, QTableWidget,
    QTableWidgetItem, QHeaderView, QCheckBox, QLineEdit, QGroupBox,
    QSystemTrayIcon, QMessageBox, QTextEdit, QSpinBox, QTabWidget,
    QAbstractItemView, QStyle
)
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QFont, QColor, QTextCursor

from .constants import (
    POLL_INTERVAL_MS, STABILIZE_CHECKS, WARNING_THRESHOLD_GB,
    DEFAULT_PREFIX, DEFAULT_AUTO_EJECT, DEFAULT_AUTO_DELETE,
    DEFAULT_START_DELAY_SEC
)
from .drive_info import DriveInfo, scan_drives
from .transfer_worker import TransferWorker
from .winapi import (
    format_size, get_disk_usage,
    refresh_serial_cache, get_cached_usb_serial, get_cached_reader_serial
)
from .log_manager import LogManager


def format_eta(seconds):
    """格式化预估时间"""
    if seconds < 1:
        return "即将完成"
    if seconds < 60:
        return f"{int(seconds)}秒"
    if seconds < 3600:
        return f"{int(seconds / 60)}分钟"
    h = seconds / 3600
    return f"{h:.1f}小时"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SD卡自动转存工具 (多硬盘版)")
        self.setMinimumSize(950, 720)
        self.resize(1050, 780)
        self.ext_drives = {}
        self.sd_cards = {}
        self.workers = {}
        self.processing = set()
        self.stable_count = {}
        self.ego_counter = 0
        self.session_transferred = 0
        self.session_sd_count = 0
        self.pending_queue = []
        self.waiting_set = set()
        self.delay_timers = {}       # letter -> QTimer (插入延迟)
        self.delay_remaining = {}    # letter -> int (剩余秒数)
        self.delay_tick = {}         # letter -> QTimer (每秒更新显示)
        self._last_drive_set = None  # 用于判断是否需要刷新序列号缓存
        self.config = self._load_config()
        self.log_mgr = LogManager(self._log_dir())
        self.log_mgr.increment_session()
        self._build_ui()
        self._setup_tray()
        self._poll_timer = QTimer()
        self._poll_timer.timeout.connect(self._poll_drives)
        self._poll_timer.start(POLL_INTERVAL_MS)
        self._blink_timer = QTimer()
        self._blink_timer.timeout.connect(self._blink_warning)
        self._blink_state = False
        self._poll_drives()

    def _app_dir(self):
        if getattr(sys, 'frozen', False):
            return os.path.dirname(sys.executable)
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _config_path(self):
        return os.path.join(self._app_dir(), 'sd_mover_config.json')

    def _log_dir(self):
        return os.path.join(self._app_dir(), 'logs')

    def _load_config(self):
        try:
            with open(self._config_path(), 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_config(self):
        self.config['prefix'] = self.prefix_edit.text()
        self.config['min_ext_gb'] = self.min_size_spin.value()
        self.config['auto_eject'] = self.auto_eject_cb.isChecked()
        self.config['auto_delete'] = self.auto_delete_cb.isChecked()
        self.config['start_delay_sec'] = self.delay_spin.value()
        self.config['test_mode'] = self.test_mode_cb.isChecked()
        try:
            with open(self._config_path(), 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
            self._log("设置已保存")
        except Exception as e:
            self._log(f"保存设置失败: {e}")

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(8)
        main_layout.setContentsMargins(12, 12, 12, 12)

        # ── 标签页 ──
        self.tab_widget = QTabWidget()
        main_layout.addWidget(self.tab_widget)

        # 主页
        main_tab = QWidget()
        layout = QVBoxLayout(main_tab)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 12)
        title = QLabel("SD卡自动转存工具 (多硬盘版)")
        title.setStyleSheet("font-size: 18px; font-weight: bold; color: #333;")
        layout.addWidget(title)
        self.warning_banner = QLabel()
        self.warning_banner.setAlignment(Qt.AlignCenter)
        self.warning_banner.setStyleSheet(
            "background-color: #ff4444; color: white; font-size: 16px;"
            " font-weight: bold; padding: 10px; border-radius: 4px;"
        )
        self.warning_banner.hide()
        layout.addWidget(self.warning_banner)
        stats = self.log_mgr.load_session_stats()
        self.stats_label = QLabel(
            f"累计: {stats['total_sd_cards']}张SD卡 | "
            f"{format_size(stats['total_bytes'])} | "
            f"{stats.get('total_files', 0)}个文件 | "
            f"{len(stats.get('drives_used', []))}个目标盘"
        )
        self.stats_label.setStyleSheet(
            "background-color: #e8e8e8; padding: 6px; border-radius: 4px;"
            " font-size: 13px; color: #555;"
        )
        layout.addWidget(self.stats_label)
        ext_group = QGroupBox("目标硬盘 (外部存储) - 支持多盘, 一个满了自动切换")
        ext_layout = QVBoxLayout(ext_group)
        self.ext_table = QTableWidget(0, 7)
        self.ext_table.setHorizontalHeaderLabels(["盘符","标签","容量","剩余","使用率","硬盘盒SN","标签"])
        h = self.ext_table.horizontalHeader()
        h.setSectionResizeMode(5, QHeaderView.Stretch)
        self.ext_table.setColumnWidth(0, 60)
        self.ext_table.setColumnWidth(1, 120)
        self.ext_table.setColumnWidth(2, 100)
        self.ext_table.setColumnWidth(3, 100)
        self.ext_table.setColumnWidth(4, 80)
        self.ext_table.setColumnWidth(6, 90)
        self.ext_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.ext_table.setSelectionMode(QAbstractItemView.NoSelection)
        ext_layout.addWidget(self.ext_table)
        layout.addWidget(ext_group)
        sd_group = QGroupBox("SD卡列表")
        sd_layout = QVBoxLayout(sd_group)
        self.sd_table = QTableWidget(0, 7)
        self.sd_table.setHorizontalHeaderLabels(["盘符","标签","容量","状态","进度","信息","操作"])
        h = self.sd_table.horizontalHeader()
        h.setSectionResizeMode(5, QHeaderView.Stretch)
        h.setSectionResizeMode(4, QHeaderView.Fixed)
        h.setSectionResizeMode(6, QHeaderView.Fixed)
        self.sd_table.setColumnWidth(0, 50)
        self.sd_table.setColumnWidth(1, 80)
        self.sd_table.setColumnWidth(2, 80)
        self.sd_table.setColumnWidth(3, 80)
        self.sd_table.setColumnWidth(4, 180)
        self.sd_table.setColumnWidth(6, 80)
        self.sd_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.sd_table.setSelectionMode(QAbstractItemView.NoSelection)
        sd_layout.addWidget(self.sd_table)
        layout.addWidget(sd_group)
        log_group = QGroupBox("日志")
        log_layout = QVBoxLayout(log_group)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(140)
        self.log_text.setStyleSheet("font-family: Consolas, monospace; font-size: 12px;")
        log_layout.addWidget(self.log_text)
        log_btn_row = QHBoxLayout()
        log_btn_row.addStretch()
        open_log_btn = QPushButton("打开日志文件夹")
        open_log_btn.clicked.connect(self._open_log_dir)
        log_btn_row.addWidget(open_log_btn)
        log_layout.addLayout(log_btn_row)
        layout.addWidget(log_group)
        s_group = QGroupBox("设置")
        s_layout = QHBoxLayout(s_group)
        s_layout.addWidget(QLabel("命名前缀:"))
        self.prefix_edit = QLineEdit(self.config.get('prefix', DEFAULT_PREFIX))
        self.prefix_edit.setMaximumWidth(80)
        s_layout.addWidget(self.prefix_edit)
        s_layout.addWidget(QLabel("最小硬盘(GB):"))
        self.min_size_spin = QSpinBox()
        self.min_size_spin.setRange(100, 10000)
        self.min_size_spin.setValue(self.config.get('min_ext_gb', 700))
        s_layout.addWidget(self.min_size_spin)
        self.auto_eject_cb = QCheckBox("自动弹出")
        self.auto_eject_cb.setChecked(self.config.get('auto_eject', DEFAULT_AUTO_EJECT))
        s_layout.addWidget(self.auto_eject_cb)
        self.auto_delete_cb = QCheckBox("自动清空")
        self.auto_delete_cb.setChecked(self.config.get('auto_delete', DEFAULT_AUTO_DELETE))
        s_layout.addWidget(self.auto_delete_cb)
        s_layout.addWidget(QLabel("插入延迟(秒):"))
        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(0, 600)
        self.delay_spin.setValue(self.config.get('start_delay_sec', DEFAULT_START_DELAY_SEC))
        s_layout.addWidget(self.delay_spin)
        self.test_mode_cb = QCheckBox("测试模式(不删不弹)")
        self.test_mode_cb.setChecked(self.config.get('test_mode', False))
        s_layout.addWidget(self.test_mode_cb)
        s_layout.addStretch()
        save_btn = QPushButton("保存设置")
        save_btn.clicked.connect(self._save_config)
        s_layout.addWidget(save_btn)
        refresh_btn = QPushButton("手动刷新")
        refresh_btn.clicked.connect(self._poll_drives)
        s_layout.addWidget(refresh_btn)
        layout.addWidget(s_group)
        self.tab_widget.addTab(main_tab, "主界面")

        # ── 序列号管理页 ──
        serial_tab = QWidget()
        sl = QVBoxLayout(serial_tab)
        sl.setContentsMargins(12, 12, 12, 12)
        sl.addWidget(QLabel("序列号标签管理 (自动分配, 可手动修改)"))
        # SD 读卡器表
        sd_reader_group = QGroupBox("SD读卡器序列号")
        sd_reader_layout = QVBoxLayout(sd_reader_group)
        self.sd_reader_table = QTableWidget(0, 3)
        self.sd_reader_table.setHorizontalHeaderLabels(["序列号", "标签", "首次出现"])
        rh = self.sd_reader_table.horizontalHeader()
        rh.setSectionResizeMode(0, QHeaderView.Stretch)
        self.sd_reader_table.setColumnWidth(1, 120)
        self.sd_reader_table.setColumnWidth(2, 160)
        sd_reader_layout.addWidget(self.sd_reader_table)
        sl.addWidget(sd_reader_group)
        # USB 硬盘盒表
        enclosure_group = QGroupBox("USB硬盘盒序列号")
        enclosure_layout = QVBoxLayout(enclosure_group)
        self.enclosure_table = QTableWidget(0, 3)
        self.enclosure_table.setHorizontalHeaderLabels(["序列号", "标签", "首次出现"])
        eh = self.enclosure_table.horizontalHeader()
        eh.setSectionResizeMode(0, QHeaderView.Stretch)
        self.enclosure_table.setColumnWidth(1, 120)
        self.enclosure_table.setColumnWidth(2, 160)
        enclosure_layout.addWidget(self.enclosure_table)
        sl.addWidget(enclosure_group)
        # 保存按钮
        serial_btn_row = QHBoxLayout()
        serial_btn_row.addStretch()
        save_serial_btn = QPushButton("保存标签")
        save_serial_btn.clicked.connect(self._save_serial_labels)
        serial_btn_row.addWidget(save_serial_btn)
        sl.addLayout(serial_btn_row)
        self.tab_widget.addTab(serial_tab, "序列号管理")

        self._load_serial_tables()

    # ── 序列号标签管理 ──

    def _load_serial_tables(self):
        """从 config 加载序列号标签到管理页表格"""
        sd_labels = self.config.get('sd_reader_labels', {})
        enc_labels = self.config.get('enclosure_labels', {})
        sd_seen = self.config.get('sd_reader_seen', {})
        enc_seen = self.config.get('enclosure_seen', {})
        self.sd_reader_table.setRowCount(0)
        for sn, label in sd_labels.items():
            row = self.sd_reader_table.rowCount()
            self.sd_reader_table.insertRow(row)
            self.sd_reader_table.setItem(row, 0, QTableWidgetItem(sn))
            self.sd_reader_table.setItem(row, 1, QTableWidgetItem(label))
            self.sd_reader_table.setItem(row, 2, QTableWidgetItem(sd_seen.get(sn, "")))
        self.enclosure_table.setRowCount(0)
        for sn, label in enc_labels.items():
            row = self.enclosure_table.rowCount()
            self.enclosure_table.insertRow(row)
            self.enclosure_table.setItem(row, 0, QTableWidgetItem(sn))
            self.enclosure_table.setItem(row, 1, QTableWidgetItem(label))
            self.enclosure_table.setItem(row, 2, QTableWidgetItem(enc_seen.get(sn, "")))

    def _save_serial_labels(self):
        """将管理页表格编辑保存到 config"""
        sd_labels = {}
        for r in range(self.sd_reader_table.rowCount()):
            sn_item = self.sd_reader_table.item(r, 0)
            label_item = self.sd_reader_table.item(r, 1)
            if sn_item and sn_item.text().strip():
                sd_labels[sn_item.text().strip()] = (label_item.text().strip() if label_item else "")
        enc_labels = {}
        for r in range(self.enclosure_table.rowCount()):
            sn_item = self.enclosure_table.item(r, 0)
            label_item = self.enclosure_table.item(r, 1)
            if sn_item and sn_item.text().strip():
                enc_labels[sn_item.text().strip()] = (label_item.text().strip() if label_item else "")
        self.config['sd_reader_labels'] = sd_labels
        self.config['enclosure_labels'] = enc_labels
        try:
            with open(self._config_path(), 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
            self._log("序列号标签已保存")
        except Exception as e:
            self._log(f"保存标签失败: {e}")

    def _register_serial(self, sn, is_reader):
        """检测到新序列号时自动分配标签"""
        if not sn:
            return
        if is_reader:
            labels = self.config.get('sd_reader_labels', {})
            seen = self.config.get('sd_reader_seen', {})
            if sn not in labels:
                n = len(labels) + 1
                labels[sn] = f"SD卡{n}"
                seen[sn] = datetime.now().strftime('%Y-%m-%d %H:%M')
                self.config['sd_reader_labels'] = labels
                self.config['sd_reader_seen'] = seen
                self._log(f"新SD读卡器: {sn} -> {labels[sn]}")
                self._load_serial_tables()
        else:
            labels = self.config.get('enclosure_labels', {})
            seen = self.config.get('enclosure_seen', {})
            if sn not in labels:
                n = len(labels) + 1
                labels[sn] = f"硬盘盒{n:02d}"
                seen[sn] = datetime.now().strftime('%Y-%m-%d %H:%M')
                self.config['enclosure_labels'] = labels
                self.config['enclosure_seen'] = seen
                self._log(f"新硬盘盒: {sn} -> {labels[sn]}")
                self._load_serial_tables()

    def _get_enclosure_label(self, sn):
        return self.config.get('enclosure_labels', {}).get(sn, "")

    def _get_reader_label(self, sn):
        return self.config.get('sd_reader_labels', {}).get(sn, "")

    def _setup_tray(self):
        self.tray = QSystemTrayIcon(self)
        try:
            icon = self.style().standardIcon(QStyle.SP_MediaPlay)
        except Exception:
            icon = QIcon()
        self.tray.setIcon(icon)
        self.tray.setToolTip("SD卡自动转存工具")
        self.tray.show()

    def _open_log_dir(self):
        os.startfile(self._log_dir())

    def _log(self, msg):
        ts = datetime.now().strftime('%H:%M:%S')
        self.log_text.append(f"[{ts}] {msg}")
        self.log_text.moveCursor(QTextCursor.End)
        try:
            with open(os.path.join(self._log_dir(), 'app.log'), 'a', encoding='utf-8') as f:
                f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
        except Exception:
            pass

    def _update_stats_label(self):
        stats = self.log_mgr.load_session_stats()
        self.stats_label.setText(
            f"累计: {stats['total_sd_cards']}张SD卡 | "
            f"{format_size(stats['total_bytes'])} | "
            f"{stats.get('total_files', 0)}个文件 | "
            f"{len(stats.get('drives_used', []))}个目标盘"
        )

    def _poll_drives(self):
        try:
            drives = scan_drives()
        except Exception as e:
            self._log(f"扫描驱动器失败: {e}")
            return
        # 批量刷新序列号缓存 (只在盘符集合变化时才真正查询, 避免频繁开 PowerShell)
        all_letters = [d.letter for d in drives]
        drive_key = frozenset(all_letters)
        if drive_key != self._last_drive_set:
            self._last_drive_set = drive_key
            refresh_serial_cache(all_letters)
        # 从缓存填充序列号
        for d in drives:
            d.hw_serial = get_cached_usb_serial(d.letter)
            d.reader_serial = get_cached_reader_serial(d.letter)
            # 注册新序列号到管理页
            if d.hw_serial:
                self._register_serial(d.hw_serial, is_reader=False)
            if d.reader_serial:
                self._register_serial(d.reader_serial, is_reader=True)
        min_gb = self.min_size_spin.value()
        ext_candidates = [d for d in drives if d.size_gb >= min_gb and d.letter != 'C']
        ext_candidates.sort(key=lambda d: (not d.is_removable, -d.total))
        new_letters = set(d.letter for d in ext_candidates)
        for d in ext_candidates:
            if d.letter not in self.ext_drives:
                self.ext_drives[d.letter] = d
                sn = d.hw_serial or "未知"
                enc_label = self._get_enclosure_label(sn) if sn else ""
                self._log(f"检测到目标硬盘: {d.letter}: ({d.label}, {format_size(d.total)}, 硬盘盒SN:{sn}, {enc_label})")
                self.tray.showMessage("目标硬盘已连接", f"{d.letter}: {d.label}")
                QTimer.singleShot(1000, self._try_start_queued)
        for letter in set(self.ext_drives) - new_letters:
            self._log(f"目标硬盘 {letter}: 已断开")
            del self.ext_drives[letter]
        self._update_ext_panel()
        if not self.ext_drives:
            self.statusBar().showMessage("等待目标硬盘连接...")
            return
        ext_letters = set(self.ext_drives.keys())
        current = set()
        for d in drives:
            if not d.is_sd_card or d.letter in ext_letters:
                continue
            current.add(d.letter)
            if d.letter not in self.sd_cards:
                self.sd_cards[d.letter] = d
                self.stable_count[d.letter] = 0
                self._add_sd_row(d)
                reader_sn = d.reader_serial or "未知"
                reader_label = self._get_reader_label(reader_sn) if d.reader_serial else ""
                self._update_sd_status(d.letter, "检测中...")
                self._log(f"检测到SD卡: {d.letter}: ({d.label}, {format_size(d.total)}, 读卡器SN:{reader_sn}, {reader_label})")
            elif d.letter in self.stable_count:
                old = self.sd_cards[d.letter]
                if old.free == d.free:
                    self.stable_count[d.letter] += 1
                    if self.stable_count[d.letter] >= STABILIZE_CHECKS:
                        del self.stable_count[d.letter]
                        if d.letter not in self.processing:
                            self._start_with_delay(d)
                else:
                    self.stable_count[d.letter] = 0
            self.sd_cards[d.letter] = d
        for letter in set(self.sd_cards) - current:
            if letter not in self.workers:
                del self.sd_cards[letter]
                self.stable_count.pop(letter, None)
                self.waiting_set.discard(letter)
                self._cancel_delay(letter)
                self.pending_queue = [s for s in self.pending_queue if s.letter != letter]
                self._remove_sd_row(letter)
                self._log(f"SD卡 {letter}: 已移除")
        self._update_status_bar()

    def _start_with_delay(self, d):
        """插入延迟后再开始传输 (可配置, 默认60秒)"""
        delay = self.delay_spin.value()
        if delay <= 0:
            self._check_and_start(d)
            return
        if d.letter in self.delay_timers:
            return  # 已在延迟中
        self.delay_remaining[d.letter] = delay
        self._update_sd_status(d.letter, f"等待{delay}秒")
        self._log(f"[{d.letter}] 插入延迟 {delay} 秒后开始传输")
        tick = QTimer()
        tick.timeout.connect(lambda l=d.letter: self._delay_tick_cb(l))
        tick.start(1000)
        self.delay_tick[d.letter] = tick

    def _delay_tick_cb(self, letter):
        """每秒更新延迟倒计时"""
        if letter not in self.delay_remaining:
            self._cancel_delay(letter)
            return
        self.delay_remaining[letter] -= 1
        remaining = self.delay_remaining[letter]
        if remaining <= 0:
            self._cancel_delay(letter)
            d = self.sd_cards.get(letter)
            if d and letter not in self.processing:
                self._check_and_start(d)
        else:
            self._update_sd_status(letter, f"等待{remaining}秒")

    def _cancel_delay(self, letter):
        """取消延迟计时器"""
        if letter in self.delay_timers:
            self.delay_timers[letter].stop()
            del self.delay_timers[letter]
        if letter in self.delay_tick:
            self.delay_tick[letter].stop()
            del self.delay_tick[letter]
        self.delay_remaining.pop(letter, None)

    def _check_and_start(self, d):
        """空间检查策略: 空间紧张时优先保证一张卡先移动完"""
        data_size = d.total - d.free
        total_free = sum(dr.free for dr in self.ext_drives.values())
        active_bytes = sum(
            (w.sd_info.total - w.sd_info.free)
            for w in self.workers.values()
        )
        available_for_this = total_free - active_bytes
        if available_for_this <= 0:
            self._queue_sd(d)
            return
        if data_size > available_for_this:
            self._queue_sd(d)
            return
        self._start_transfer(d)

    def _queue_sd(self, d):
        """将SD卡加入等待队列, 空间紧张时小卡优先"""
        if d.letter in self.waiting_set:
            return
        self.waiting_set.add(d.letter)
        self.pending_queue.append(d)
        self.pending_queue.sort(key=lambda x: x.total - x.free)
        self._update_sd_status(d.letter, "排队中")
        self._log(
            f"[{d.letter}] 空间紧张, 已排队等待 "
            f"(数据 {format_size(d.total - d.free)})"
        )

    def _try_start_queued(self):
        """尝试启动队列中能放下的SD卡"""
        if not self.pending_queue:
            return
        total_free = sum(dr.free for dr in self.ext_drives.values())
        active_bytes = sum(
            (w.sd_info.total - w.sd_info.free)
            for w in self.workers.values()
        )
        available = total_free - active_bytes
        started = []
        for d in self.pending_queue:
            data_size = d.total - d.free
            if data_size <= available:
                available -= data_size
                started.append(d)
                self.waiting_set.discard(d.letter)
                self._start_transfer(d)
        for d in started:
            self.pending_queue.remove(d)
        if started:
            self._log(f"队列启动了 {len(started)} 张SD卡, 剩余 {len(self.pending_queue)} 张排队")

    def _update_ext_panel(self):
        self.ext_table.setRowCount(0)
        any_full = False
        full_info = []
        for letter in sorted(self.ext_drives.keys()):
            d = self.ext_drives[letter]
            total, free = get_disk_usage(d.root)
            d.total = total
            d.free = free
            row = self.ext_table.rowCount()
            self.ext_table.insertRow(row)
            self.ext_table.setItem(row, 0, QTableWidgetItem(f"{letter}:"))
            self.ext_table.setItem(row, 1, QTableWidgetItem(d.label))
            self.ext_table.setItem(row, 2, QTableWidgetItem(format_size(d.total)))
            free_item = QTableWidgetItem(format_size(d.free))
            pct = int((1 - free / total) * 100) if total > 0 else 0
            if free < WARNING_THRESHOLD_GB * 1024 ** 3:
                free_item.setForeground(QColor(255, 0, 0))
                any_full = True
                full_info.append((letter, d.hw_serial or "未知"))
            self.ext_table.setItem(row, 3, free_item)
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(pct)
            bar.setFormat(f"{pct}%")
            self.ext_table.setCellWidget(row, 4, bar)
            serial_item = QTableWidgetItem(d.hw_serial or "查询中...")
            if any_full and free < WARNING_THRESHOLD_GB * 1024 ** 3:
                serial_item.setForeground(QColor(255, 0, 0))
            self.ext_table.setItem(row, 5, serial_item)
            enc_label = self._get_enclosure_label(d.hw_serial) if d.hw_serial else ""
            self.ext_table.setItem(row, 6, QTableWidgetItem(enc_label))
        if any_full:
            parts = []
            for letter, sn in full_info:
                enc_label = self._get_enclosure_label(sn) if sn and sn != "未知" else ""
                parts.append(f"{letter}: [{enc_label} SN:{sn}]")
            self.warning_banner.setText(
                f"!! 警告: 目标盘 {' '.join(parts)} 空间不足! 请更换硬盘 !!"
            )
            self.warning_banner.show()
            if not self._blink_timer.isActive():
                self._blink_timer.start(500)
            # 托盘通知也带上序列号
            sn_text = "; ".join(f"{l} SN:{s}" for l, s in full_info)
            self.tray.showMessage(
                "硬盘已满", f"序列号: {sn_text}",
                QSystemTrayIcon.Warning, 8000
            )
        else:
            self.warning_banner.hide()
            if self._blink_timer.isActive():
                self._blink_timer.stop()

    def _blink_warning(self):
        self._blink_state = not self._blink_state
        if self._blink_state:
            self.warning_banner.setStyleSheet("background-color: #ff0000; color: white; font-size: 16px; font-weight: bold; padding: 10px; border-radius: 4px;")
        else:
            self.warning_banner.setStyleSheet("background-color: #cc0000; color: yellow; font-size: 16px; font-weight: bold; padding: 10px; border-radius: 4px;")

    def _update_status_bar(self):
        n = len(self.workers)
        transferred = format_size(self.session_transferred)
        nd = len(self.ext_drives)
        nq = len(self.pending_queue)
        parts = []
        if n > 0:
            parts.append(f"传输中: {n}个SD卡")
        else:
            parts.append("监控中...")
        parts.append(f"{nd}个目标盘")
        parts.append(f"本次: {transferred}")
        if nq > 0:
            parts.append(f"排队: {nq}张")
        self.statusBar().showMessage(" | ".join(parts))

    def _next_ego(self):
        max_num = 0
        for d in self.ext_drives.values():
            try:
                for item in os.listdir(d.root):
                    m = re.match(r'\D+(\d+)', item, re.IGNORECASE)
                    if m:
                        num = int(m.group(1))
                        if num > max_num:
                            max_num = num
            except OSError:
                pass
        nxt = max(max_num + 1, self.ego_counter + 1)
        self.ego_counter = nxt
        return nxt

    def _start_transfer(self, d):
        self.processing.add(d.letter)
        ego = self._next_ego()
        prefix = self.prefix_edit.text() or DEFAULT_PREFIX
        dest_roots = []
        for letter in sorted(self.ext_drives.keys()):
            dr = self.ext_drives[letter]
            _, free = get_disk_usage(dr.root)
            dest_roots.append((letter, dr.root, free))
        dest_roots.sort(key=lambda x: x[2])  # 升序: 先填快满的盘
        dest_roots = [(l, r) for l, r, _ in dest_roots]
        worker = TransferWorker(
            sd_info=d, dest_roots=dest_roots,
            ego_num=ego, prefix=prefix,
            auto_eject=self.auto_eject_cb.isChecked(),
            auto_delete=self.auto_delete_cb.isChecked(),
            reader_serial=d.reader_serial or "",
            test_mode=self.test_mode_cb.isChecked()
        )
        worker.progress_updated.connect(self._on_progress)
        worker.status_changed.connect(self._update_sd_status)
        worker.transfer_completed.connect(self._on_completed)
        worker.transfer_failed.connect(self._on_failed)
        worker.log_message.connect(self._log)
        worker.dest_changed.connect(self._on_dest_changed)
        worker.need_space.connect(self._on_need_space)
        self.workers[d.letter] = worker
        worker.start()
        self._log(f"[{d.letter}] 分配编号: {prefix}{ego:02d}")
        self._set_sd_button(d.letter, "取消")

    def _on_dest_changed(self, letter, dest_letter):
        row = self._find_row(letter)
        if row >= 0:
            self.sd_table.item(row, 5).setText(f"写入 -> {dest_letter}:")

    def _on_need_space(self, letter, remaining_files, remaining_bytes):
        """所有目标盘满了, 弹窗让用户插入新盘或忽略"""
        worker = self.workers.get(letter)
        if not worker:
            return
        full_sn = []
        for dl, dd in self.ext_drives.items():
            _, df = get_disk_usage(dd.root)
            if df < WARNING_THRESHOLD_GB * 1024 ** 3:
                sn = get_cached_usb_serial(dl) or dd.hw_serial or '未知'
                enc_label = self._get_enclosure_label(sn) if sn and sn != '未知' else ""
                full_sn.append(f"{dl}: [{enc_label} SN:{sn}]")
        sn_line = "\n".join(full_sn) if full_sn else ""
        msg = (
            f"目标硬盘空间不足!\n\n"
            f"SD卡 {letter}: 还剩 {remaining_files} 个文件 "
            f"({format_size(remaining_bytes)}) 未传输。\n\n"
            + (f"已满硬盘序列号:\n{sn_line}\n\n" if sn_line else "")
            + f"请插入新的目标硬盘后点\"继续\",\n"
            f"或点\"忽略\"放弃未传输部分(已传输的会从SD卡删除)。"
        )
        box = QMessageBox(self)
        box.setWindowTitle("硬盘空间不足")
        box.setIcon(QMessageBox.Warning)
        box.setText(msg)
        cont_btn = box.addButton("继续", QMessageBox.AcceptRole)
        ign_btn = box.addButton("忽略", QMessageBox.RejectRole)
        box.setDefaultButton(cont_btn)
        self.raise_()
        self.activateWindow()
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized | Qt.WindowActive)
        reply = box.exec_()
        if reply == QMessageBox.RejectRole:
            worker.give_up()
            self._log(f"[{letter}] 用户选择忽略, 放弃未传输部分")
        else:
            self._poll_drives()
            existing = set()
            for dl, dr in worker.dest_roots:
                existing.add(dl)
            added = 0
            min_gb = self.min_size_spin.value()
            for ed in self.ext_drives.values():
                if ed.letter in existing:
                    continue
                if ed.size_gb < min_gb:
                    continue
                worker.add_dest(ed.letter, ed.root)
                existing.add(ed.letter)
                added += 1
                self._log(f"[{letter}] 新增目标盘 {ed.letter}: ({ed.label})")
            if added == 0:
                QMessageBox.information(
                    self, "未检测到新硬盘",
                    "未检测到新的目标硬盘, 请插入后再点继续。"
                )
                self._on_need_space(letter, remaining_files, remaining_bytes)

    def _on_progress(self, letter, copied, total, fname, speed, file_copied, file_total):
        row = self._find_row(letter)
        if row < 0:
            return
        pct = int(copied / total * 100) if total > 0 else 0
        bar = self.sd_table.cellWidget(row, 4)
        if bar:
            bar.setValue(pct)
            bar.setFormat(f"{pct}%")
        # 总进度 ETA
        eta_str = ""
        if speed > 0 and total > 0:
            remaining_bytes = total - copied
            remaining_seconds = remaining_bytes / (speed * 1024 * 1024)
            eta_str = f" | 剩余: {format_eta(remaining_seconds)}"
        # 单文件进度
        file_pct_str = ""
        if file_total > 0 and file_copied >= 0:
            file_pct = int(file_copied / file_total * 100)
            file_pct_str = f" | 文件: {file_pct}%"
        info = f"{format_size(copied)}/{format_size(total)} | {speed:.1f} MB/s{eta_str}{file_pct_str}"
        if fname:
            info = f"{fname}\n{info}"
        self.sd_table.item(row, 5).setText(info)

    def _on_completed(self, letter, dest_letter, dest_path, bytes_tx, file_count):
        self.session_transferred += bytes_tx
        self.session_sd_count += 1
        self._log(f"[{letter}] [OK] 传输完成: {format_size(bytes_tx)} -> {dest_letter}: {dest_path}")
        row = self._find_row(letter)
        if row >= 0:
            bar = self.sd_table.cellWidget(row, 4)
            if bar:
                bar.setValue(100)
                bar.setFormat("100%")
            self._update_sd_status(letter, "已完成")
        sd = self.sd_cards.get(letter)
        sd_label = sd.label if sd else ""
        sd_size = format_size(sd.total) if sd else ""
        dest_label = ""
        if dest_letter and dest_letter in self.ext_drives:
            dest_label = self.ext_drives[dest_letter].label
        folder_name = os.path.basename(dest_path) if dest_path else ""
        self.log_mgr.add_transfer(
            sd_letter=letter, sd_label=sd_label, sd_size=sd_size,
            dest_letter=dest_letter, dest_label=dest_label,
            folder=folder_name, bytes_tx=bytes_tx,
            file_count=file_count, status="完成"
        )
        self._update_stats_label()
        self.tray.showMessage("传输完成", f"SD卡 {letter}: 已传输 {format_size(bytes_tx)}", QSystemTrayIcon.Information, 5000)
        QApplication.beep()
        self.processing.discard(letter)
        self.workers.pop(letter, None)
        self._update_status_bar()
        QTimer.singleShot(500, self._try_start_queued)

    def _on_failed(self, letter, error):
        # 取消/忽略的信号通过failed发过来
        if error == "CANCELLED":
            self._update_sd_status(letter, "已取消")
            self._log(f"[{letter}] 传输已取消")
            self._set_sd_button(letter, "开始")
        elif error == "GAVE_UP":
            self._update_sd_status(letter, "已忽略")
            self._log(f"[{letter}] 已忽略未传输部分(已传输的已从SD卡删除)")
            self._set_sd_button(letter, "开始")
        else:
            self._log(f"[{letter}] [ERR] 传输失败: {error}")
            self._update_sd_status(letter, "失败")
            self.tray.showMessage("传输失败", f"SD卡 {letter}: {error}", QSystemTrayIcon.Critical, 10000)
            self._set_sd_button(letter, "重试")
        self.processing.discard(letter)
        self.workers.pop(letter, None)
        self._update_status_bar()
        QTimer.singleShot(500, self._try_start_queued)

    def _on_action_btn(self, letter):
        """操作按钮: 取消/开始/重试 三态"""
        btn_text = ""
        row = self._find_row(letter)
        if row >= 0:
            btn = self.sd_table.cellWidget(row, 6)
            if btn:
                btn_text = btn.text()
        if btn_text == "取消":
            worker = self.workers.get(letter)
            if worker:
                worker.cancel()
                self._log(f"[{letter}] 用户取消传输")
                self._update_sd_status(letter, "取消中...")
        elif btn_text in ("开始", "重试"):
            d = self.sd_cards.get(letter)
            if d:
                self.waiting_set.discard(letter)
                self.pending_queue = [s for s in self.pending_queue if s.letter != letter]
                self._check_and_start(d)
                self._log(f"[{letter}] 重新开始传输")

    def _add_sd_row(self, d):
        row = self.sd_table.rowCount()
        self.sd_table.insertRow(row)
        self.sd_table.setItem(row, 0, QTableWidgetItem(f"{d.letter}:"))
        self.sd_table.setItem(row, 1, QTableWidgetItem(d.label))
        self.sd_table.setItem(row, 2, QTableWidgetItem(format_size(d.total)))
        self.sd_table.setItem(row, 3, QTableWidgetItem("等待中"))
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setFormat("0%")
        self.sd_table.setCellWidget(row, 4, bar)
        self.sd_table.setItem(row, 5, QTableWidgetItem(""))
        btn = QPushButton("取消")
        btn.setEnabled(False)
        btn.clicked.connect(lambda checked, l=d.letter: self._on_action_btn(l))
        self.sd_table.setCellWidget(row, 6, btn)

    def _set_sd_button(self, letter, text):
        row = self._find_row(letter)
        if row >= 0:
            btn = self.sd_table.cellWidget(row, 6)
            if btn:
                btn.setText(text)
                btn.setEnabled(True)

    def _remove_sd_row(self, letter):
        row = self._find_row(letter)
        if row >= 0:
            self.sd_table.removeRow(row)

    def _find_row(self, letter):
        for r in range(self.sd_table.rowCount()):
            item = self.sd_table.item(r, 0)
            if item and item.text() == f"{letter}:":
                return r
        return -1

    def _update_sd_status(self, letter, status):
        row = self._find_row(letter)
        if row < 0:
            return
        item = self.sd_table.item(row, 3)
        if not item:
            return
        item.setText(status)
        colors = {
            "已完成": QColor(0, 128, 0),
            "失败": QColor(255, 0, 0),
            "空间不足": QColor(255, 0, 0),
            "传输中": QColor(0, 0, 200),
            "已弹出": QColor(0, 128, 0),
            "弹出失败": QColor(255, 128, 0),
            "取消中...": QColor(255, 128, 0),
            "已取消": QColor(128, 128, 128),
        }
        item.setForeground(colors.get(status, QColor(0, 0, 0)))
        if status == "传输中":
            self._set_sd_button(letter, "取消")
        elif status in ("已完成", "已弹出"):
            row2 = self._find_row(letter)
            if row2 >= 0:
                btn = self.sd_table.cellWidget(row2, 6)
                if btn:
                    btn.setEnabled(False)

    def closeEvent(self, event):
        if self.workers:
            reply = QMessageBox.question(
                self, '确认退出',
                f'有 {len(self.workers)} 个传输正在进行, 确定退出吗?',
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )
            if reply == QMessageBox.No:
                event.ignore()
                return
        self._save_config()
        for w in self.workers.values():
            w.cancel()
            w.wait(3000)
        self._poll_timer.stop()
        self._blink_timer.stop()
        event.accept()
