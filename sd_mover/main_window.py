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
    QAbstractItemView, QStyle, QRadioButton, QButtonGroup
)
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QFont, QColor, QTextCursor

from PyQt5.QtCore import QThread, pyqtSignal

from .constants import (
    POLL_INTERVAL_MS, STABILIZE_CHECKS, WARNING_THRESHOLD_GB,
    DEFAULT_PREFIX, DEFAULT_AUTO_EJECT, DEFAULT_AUTO_DELETE,
    DEFAULT_START_DELAY_SEC, DEFAULT_MAX_CONCURRENT_SD,
    DEFAULT_MAX_CONCURRENT_HD, DEFAULT_SCATTER_MODE
)
from .drive_info import DriveInfo, scan_drives
from .transfer_worker import TransferWorker
from .winapi import (
    format_size, get_disk_usage,
    refresh_serial_cache, get_cached_usb_serial, get_cached_reader_serial,
    pop_refresh_done
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
        self.delay_timers = {}
        self.delay_remaining = {}
        self.delay_tick = {}
        self._last_drive_set = None
        self._test_processed = set()  # SD cards already transferred in test mode
        self._completed_sd = set()  # SD letters already completed (avoid auto-restart loop)
        self._ready_to_start = set()
        self._serial_refreshing = False
        self._serial_refresh_timer = QTimer()
        self._serial_refresh_timer.timeout.connect(self._check_serial_refresh)
        self._serial_refresh_timer.start(2000)
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
            with open(self._config_path(), 'r', encoding='utf-8-sig') as f:
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
        self.config['max_concurrent_sd'] = self.max_concurrent_sd_spin.value()
        self.config['max_concurrent_hd'] = self.max_concurrent_hd_spin.value()
        self.config['scatter_mode'] = self.scatter_radio.isChecked()
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

        self.tab_widget = QTabWidget()
        main_layout.addWidget(self.tab_widget)

        # ── Tab 1: 主界面 ──
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

        # 目标硬盘表
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

        # SD卡列表
        sd_group = QGroupBox("SD卡列表")
        sd_layout = QVBoxLayout(sd_group)
        self.sd_table = QTableWidget(0, 8)
        self.sd_table.setHorizontalHeaderLabels(["盘符","标签","容量","状态","进度","速度","剩余时间","操作"])
        h = self.sd_table.horizontalHeader()
        h.setSectionResizeMode(6, QHeaderView.Stretch)
        h.setSectionResizeMode(4, QHeaderView.Fixed)
        h.setSectionResizeMode(7, QHeaderView.Fixed)
        self.sd_table.setColumnWidth(0, 50)
        self.sd_table.setColumnWidth(1, 80)
        self.sd_table.setColumnWidth(2, 80)
        self.sd_table.setColumnWidth(3, 80)
        self.sd_table.setColumnWidth(4, 180)
        self.sd_table.setColumnWidth(5, 90)
        self.sd_table.setColumnWidth(7, 80)
        self.sd_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.sd_table.setSelectionMode(QAbstractItemView.NoSelection)
        sd_layout.addWidget(self.sd_table)
        layout.addWidget(sd_group)

        # 日志
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
        self.tab_widget.addTab(main_tab, "主界面")

        # ── Tab 2: 设置 ──
        settings_tab = QWidget()
        sl = QVBoxLayout(settings_tab)
        sl.setContentsMargins(20, 20, 20, 20)
        sl.setSpacing(12)

        # 基本设置
        basic_group = QGroupBox("基本设置")
        basic_layout = QGridLayout(basic_group)
        basic_layout.setSpacing(8)

        basic_layout.addWidget(QLabel("命名前缀:"), 0, 0)
        self.prefix_edit = QLineEdit(self.config.get('prefix', DEFAULT_PREFIX))
        self.prefix_edit.setMaximumWidth(120)
        basic_layout.addWidget(self.prefix_edit, 0, 1)

        basic_layout.addWidget(QLabel("最小硬盘(GB):"), 0, 2)
        self.min_size_spin = QSpinBox()
        self.min_size_spin.setRange(1, 10000)
        self.min_size_spin.setValue(self.config.get('min_ext_gb', 700))
        basic_layout.addWidget(self.min_size_spin, 0, 3)

        basic_layout.addWidget(QLabel("插入延迟(秒):"), 1, 0)
        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(0, 600)
        self.delay_spin.setValue(self.config.get('start_delay_sec', DEFAULT_START_DELAY_SEC))
        basic_layout.addWidget(self.delay_spin, 1, 1)

        basic_layout.addWidget(QLabel("空间警告阈值(GB):"), 1, 2)
        self.warn_spin = QSpinBox()
        self.warn_spin.setRange(1, 500)
        self.warn_spin.setValue(self.config.get('warn_threshold_gb', WARNING_THRESHOLD_GB))
        basic_layout.addWidget(self.warn_spin, 1, 3)

        self.auto_eject_cb = QCheckBox("自动弹出SD卡")
        self.auto_eject_cb.setChecked(self.config.get('auto_eject', DEFAULT_AUTO_EJECT))
        basic_layout.addWidget(self.auto_eject_cb, 2, 0)

        self.auto_delete_cb = QCheckBox("自动清空SD卡")
        self.auto_delete_cb.setChecked(self.config.get('auto_delete', DEFAULT_AUTO_DELETE))
        basic_layout.addWidget(self.auto_delete_cb, 2, 1)

        self.test_mode_cb = QCheckBox("测试模式(不删不弹)")
        self.test_mode_cb.setChecked(self.config.get('test_mode', False))
        basic_layout.addWidget(self.test_mode_cb, 2, 2)

        sl.addWidget(basic_group)

        # 并发控制
        concurrency_group = QGroupBox("并发控制")
        conc_layout = QGridLayout(concurrency_group)
        conc_layout.setSpacing(8)

        conc_layout.addWidget(QLabel("同时读取SD卡数:"), 0, 0)
        self.max_concurrent_sd_spin = QSpinBox()
        self.max_concurrent_sd_spin.setRange(1, 50)
        self.max_concurrent_sd_spin.setValue(self.config.get('max_concurrent_sd', DEFAULT_MAX_CONCURRENT_SD))
        self.max_concurrent_sd_spin.setToolTip("同时进行传输的SD卡最大数量")
        conc_layout.addWidget(self.max_concurrent_sd_spin, 0, 1)
        conc_layout.addWidget(QLabel("(默认10, 插6块盘时可设2-3块同时读)"), 0, 2)

        conc_layout.addWidget(QLabel("同时写入硬盘数:"), 1, 0)
        self.max_concurrent_hd_spin = QSpinBox()
        self.max_concurrent_hd_spin.setRange(1, 50)
        self.max_concurrent_hd_spin.setValue(self.config.get('max_concurrent_hd', DEFAULT_MAX_CONCURRENT_HD))
        self.max_concurrent_hd_spin.setToolTip("同时写入的目标硬盘最大数量")
        conc_layout.addWidget(self.max_concurrent_hd_spin, 1, 1)
        conc_layout.addWidget(QLabel("(控制同时往几个硬盘写数据)"), 1, 2)

        sl.addWidget(concurrency_group)

        # 拷贝模式
        mode_group = QGroupBox("拷贝模式 (硬盘空间不足时如何处理)")
        mode_layout = QVBoxLayout(mode_group)
        self.scatter_radio = QRadioButton(
            "打散写入 — 文件跨盘拆分, 第一块写满后多的写在第二块"
        )
        self.scatter_radio.setChecked(self.config.get('scatter_mode', DEFAULT_SCATTER_MODE))
        self.scatter_radio.setToolTip(
            "打散模式: 一个文件可以写到A盘, 下一个文件写到B盘\n"
            "适合: 尽量利用所有空间, 不浪费"
        )
        mode_layout.addWidget(self.scatter_radio)

        self.whole_radio = QRadioButton(
            "整体文件 — 空间不够放整个文件的直接跳过"
        )
        self.whole_radio.setChecked(not self.config.get('scatter_mode', DEFAULT_SCATTER_MODE))
        self.whole_radio.setToolTip(
            "整体模式: 只往能放下整个文件的盘里写\n"
            "适合: 需要保持文件完整性, 剩余200G的盘跳过256G的卡"
        )
        mode_layout.addWidget(self.whole_radio)
        sl.addWidget(mode_group)

        # 保存按钮
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        save_btn = QPushButton("保存设置")
        save_btn.setMinimumWidth(120)
        save_btn.clicked.connect(self._save_config)
        btn_row.addWidget(save_btn)
        sl.addLayout(btn_row)
        sl.addStretch()

        self.tab_widget.addTab(settings_tab, "设置")

        # ── Tab 3: 序列号管理 ──
        serial_tab = QWidget()
        sl2 = QVBoxLayout(serial_tab)
        sl2.setContentsMargins(12, 12, 12, 12)
        sl2.addWidget(QLabel("序列号标签管理 (自动分配, 可手动修改)"))
        sd_reader_group = QGroupBox("SD读卡器序列号")
        sd_reader_layout = QVBoxLayout(sd_reader_group)
        self.sd_reader_table = QTableWidget(0, 3)
        self.sd_reader_table.setHorizontalHeaderLabels(["序列号", "标签", "首次出现"])
        rh = self.sd_reader_table.horizontalHeader()
        rh.setSectionResizeMode(0, QHeaderView.Stretch)
        self.sd_reader_table.setColumnWidth(1, 120)
        self.sd_reader_table.setColumnWidth(2, 160)
        sd_reader_layout.addWidget(self.sd_reader_table)
        sl2.addWidget(sd_reader_group)
        enclosure_group = QGroupBox("USB硬盘盒序列号")
        enclosure_layout = QVBoxLayout(enclosure_group)
        self.enclosure_table = QTableWidget(0, 3)
        self.enclosure_table.setHorizontalHeaderLabels(["序列号", "标签", "首次出现"])
        eh = self.enclosure_table.horizontalHeader()
        eh.setSectionResizeMode(0, QHeaderView.Stretch)
        self.enclosure_table.setColumnWidth(1, 120)
        self.enclosure_table.setColumnWidth(2, 160)
        enclosure_layout.addWidget(self.enclosure_table)
        sl2.addWidget(enclosure_group)
        serial_btn_row = QHBoxLayout()
        serial_btn_row.addStretch()
        save_serial_btn = QPushButton("保存标签")
        save_serial_btn.clicked.connect(self._save_serial_labels)
        serial_btn_row.addWidget(save_serial_btn)
        sl2.addLayout(serial_btn_row)
        self.tab_widget.addTab(serial_tab, "序列号管理")

        self._load_serial_tables()

    # ── 序列号标签管理 ──

    def _load_serial_tables(self):
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

    def _check_serial_refresh(self):
        done_drives = pop_refresh_done()
        if done_drives is not None:
            self._serial_refreshing = False
            self._log("序列号缓存刷新完成")
            for letter, d in self.ext_drives.items():
                d.hw_serial = get_cached_usb_serial(letter)
            for letter, d in self.sd_cards.items():
                d.reader_serial = get_cached_reader_serial(letter)
            for letter in list(self.ext_drives.keys()):
                sn = get_cached_usb_serial(letter)
                if sn:
                    self._register_serial(sn, is_reader=False)
            for letter in list(self.sd_cards.keys()):
                sn = get_cached_reader_serial(letter)
                if sn:
                    self._register_serial(sn, is_reader=True)
            self._update_ext_panel()
        for letter, d in self.ext_drives.items():
            d.hw_serial = get_cached_usb_serial(letter)
        for letter, d in self.sd_cards.items():
            d.reader_serial = get_cached_reader_serial(letter)
        for letter in list(self.ext_drives.keys()):
            sn = get_cached_usb_serial(letter)
            if sn:
                self._register_serial(sn, is_reader=False)
        for letter in list(self.sd_cards.keys()):
            sn = get_cached_reader_serial(letter)
            if sn:
                self._register_serial(sn, is_reader=True)
        self._update_ext_panel()

    def _poll_drives(self):
        try:
            min_gb = self.min_size_spin.value()
            drives = scan_drives(min_ext_gb=min_gb)
        except Exception as e:
            self._log(f"扫描驱动器失败: {e}")
            return
        all_letters = [d.letter for d in drives]
        drive_key = frozenset(all_letters)
        if drive_key != self._last_drive_set:
            self._last_drive_set = drive_key
            if not self._serial_refreshing:
                self._serial_refreshing = True
                refresh_serial_cache(all_letters)
        ext_new = {}
        sd_new = {}
        for d in drives:
            if d.is_sd_card:
                sn = get_cached_reader_serial(d.letter) or ""
                d.reader_serial = sn
                sd_new[d.letter] = d
            elif d.is_external_drive:
                sn = get_cached_usb_serial(d.letter) or ""
                d.hw_serial = sn
                ext_new[d.letter] = d
        old_ext = set(self.ext_drives.keys())
        old_sd = set(self.sd_cards.keys())
        self.ext_drives = ext_new
        self.sd_cards = sd_new
        for letter in ext_new:
            d = ext_new[letter]
            sn = d.hw_serial or get_cached_usb_serial(letter) or ""
            d.hw_serial = sn
            if sn:
                self._register_serial(sn, is_reader=False)
        for letter in sd_new:
            d = sd_new[letter]
            sn = d.reader_serial or get_cached_reader_serial(letter) or ""
            d.reader_serial = sn
            if sn:
                self._register_serial(sn, is_reader=True)
        removed_sd = old_sd - set(sd_new.keys())
        for letter in removed_sd:
            self._cancel_delay(letter)
            self.pending_queue = [s for s in self.pending_queue if s.letter != letter]
            self._remove_sd_row(letter)
            self._completed_sd.discard(letter)
        for letter in old_ext - set(ext_new.keys()):
            self._log(f"目标硬盘 {letter}: 已移除")
        self._update_ext_panel()
        for letter in sd_new:
            if letter not in old_sd:
                d = sd_new[letter]
                self._add_sd_row(d)
                # New SD card inserted: clear completed state
                self._completed_sd.discard(letter)
                if d.letter not in self.processing and d.letter not in self.waiting_set:
                    if d.letter not in self._ready_to_start:
                        self._start_with_delay(d)
            else:
                d = sd_new[letter]
                old_d = self.sd_cards.get(letter)
                if old_d:
                    d.reader_serial = old_d.reader_serial
        for letter in sd_new:
            d = sd_new[letter]
            if letter in self.processing:
                continue
            if letter in self.waiting_set:
                continue
            if letter in self._ready_to_start:
                continue
            if letter in self.delay_remaining:
                continue
            # Skip SD cards that already completed transfer (avoid auto-restart loop)
            if letter in self._completed_sd:
                continue
            _, sd_free = get_disk_usage(d.root)
            d.free = sd_free
            if sd_free < d.total * 0.99:
                self._start_with_delay(d)
        self._update_status_bar()

    def _start_with_delay(self, d):
        if self.test_mode_cb.isChecked() and d.letter in self._test_processed:
            self._update_sd_status(d.letter, "已传(测试)")
            return
        delay = self.delay_spin.value()
        if delay <= 0:
            self._check_and_start(d)
            return
        if d.letter in self.delay_timers:
            return
        self.delay_remaining[d.letter] = delay
        self._update_sd_status(d.letter, f"等待{delay}秒")
        self._log(f"[{d.letter}] 插入延迟 {delay} 秒后开始传输")
        tick = QTimer()
        tick.timeout.connect(lambda l=d.letter: self._delay_tick_cb(l))
        tick.start(1000)
        self.delay_tick[d.letter] = tick

    def _delay_tick_cb(self, letter):
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
        if letter in self.delay_timers:
            self.delay_timers[letter].stop()
            del self.delay_timers[letter]
        if letter in self.delay_tick:
            self.delay_tick[letter].stop()
            del self.delay_tick[letter]
        self.delay_remaining.pop(letter, None)

    def _check_and_start(self, d):
        self._ready_to_start.add(d.letter)
        self._plan_and_start()

    def _plan_and_start(self):
        """全局空间预算 + 并发限制"""
        ready = []
        for letter in list(self._ready_to_start):
            if letter in self.processing or letter in self.waiting_set:
                continue
            sd = self.sd_cards.get(letter)
            if sd:
                ready.append(sd)
        if not ready:
            return
        ready.sort(key=lambda x: x.total - x.free, reverse=True)
        total_free = sum(dr.free for dr in self.ext_drives.values())
        active_bytes = sum(
            (w.sd_info.total - w.sd_info.free)
            for w in self.workers.values()
        )
        available = max(0, total_free - active_bytes)
        max_sd = self.max_concurrent_sd_spin.value()
        started = len(self.processing)
        # 整体文件模式: 不做空间预算, 允许启动(worker内部跳过放不下的文件)
        # 打散模式: 需要空间预算, 不够则排队等待新盘
        whole_mode = self.whole_radio.isChecked()
        for card in ready:
            if started >= max_sd:
                self._ready_to_start.discard(card.letter)
                self._queue_sd(card)
                continue
            data_size = card.total - card.free
            if whole_mode or data_size <= available:
                if not whole_mode:
                    available -= data_size
                self._ready_to_start.discard(card.letter)
                self._start_transfer(card)
                started += 1
            else:
                self._ready_to_start.discard(card.letter)
                self._queue_sd(card)

    def _queue_sd(self, d):
        if d.letter in self.waiting_set:
            return
        self.waiting_set.add(d.letter)
        self.pending_queue.append(d)
        self.pending_queue.sort(key=lambda x: x.total - x.free)
        self._update_sd_status(d.letter, "排队中")
        self._log(
            f"[{d.letter}] 空间紧张或并发已满, 已排队等待 "
            f"(数据 {format_size(d.total - d.free)})"
        )

    def _try_start_queued(self):
        for d in self.pending_queue:
            self._ready_to_start.add(d.letter)
        self.pending_queue.clear()
        self.waiting_set.clear()
        self._plan_and_start()

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
            warn_gb = self.warn_spin.value()
            if free < warn_gb * 1024 ** 3:
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
            if any_full and free < warn_gb * 1024 ** 3:
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
        # Build full dest list (all external drives), sorted by free desc
        all_dests_full = []
        for letter in sorted(self.ext_drives.keys()):
            dr = self.ext_drives[letter]
            _, free = get_disk_usage(dr.root)
            all_dests_full.append((letter, dr.root, free))
        all_dests_full.sort(key=lambda x: x[2], reverse=True)
        # Limit to max_concurrent_hd drives (most free first)
        max_hd = self.max_concurrent_hd_spin.value()
        active_roots = all_dests_full[:max_hd]
        # Bind this SD card to a PRIMARY dest so it fills one drive before
        # switching. Round-robin among the active drives so multiple SD cards
        # bind different drives for true parallel writes.
        primary_dest = ""
        if active_roots:
            # Count how many workers are already bound to each drive
            bind_count = {}
            for l, r, _ in active_roots:
                bind_count[l] = 0
            for w in self.workers.values():
                if w.primary_dest in bind_count:
                    bind_count[w.primary_dest] += 1
            # Pick the active drive with the fewest bindings (ties broken by most free)
            best = min(active_roots, key=lambda x: (bind_count[x[0]], -x[2]))
            primary_dest = best[0]
        dest_roots = [(l, r) for l, r, _ in active_roots]
        scatter = self.scatter_radio.isChecked()
        worker = TransferWorker(
            sd_info=d, dest_roots=dest_roots,
            ego_num=ego, prefix=prefix,
            auto_eject=self.auto_eject_cb.isChecked(),
            auto_delete=self.auto_delete_cb.isChecked(),
            reader_serial=d.reader_serial or "",
            test_mode=self.test_mode_cb.isChecked(),
            scatter_mode=scatter,
            primary_dest=primary_dest
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
        mode_str = '打散' if scatter else '整体文件'
        self._log(f"[{d.letter}] 分配编号: {prefix}{ego:02d} (模式: {mode_str}, 主盘: {primary_dest or '自动'})")
        self._set_sd_button(d.letter, "取消")

    def _on_dest_changed(self, letter, dest_letter):
        row = self._find_row(letter)
        if row >= 0:
            # 速度列(5)和剩余时间列(6)会被进度更新覆盖, 这里不改
            pass

    def _on_need_space(self, letter, remaining_files, remaining_bytes):
        worker = self.workers.get(letter)
        if not worker:
            return
        full_sn = []
        for dl, dd in self.ext_drives.items():
            _, df = get_disk_usage(dd.root)
            warn_gb = self.warn_spin.value()
            if df < warn_gb * 1024 ** 3:
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
            max_hd = self.max_concurrent_hd_spin.value()
            new_dests = []
            for ed in self.ext_drives.values():
                if ed.letter in existing:
                    continue
                if ed.size_gb < min_gb:
                    continue
                new_dests.append((ed.letter, ed.root))
            new_dests.sort(key=lambda x: get_disk_usage(x[1])[1])
            for dl, dr in new_dests[:max_hd]:
                worker.add_dest(dl, dr)
                # Update primary_dest so worker binds the newly added drive
                worker.primary_dest = dl
                existing.add(dl)
                added += 1
                self._log(f"[{letter}] 新增目标盘 {dl}: (绑定)")
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
        def clamp_pct(v):
            return max(0, min(100, v))
        overall_raw = (copied / total * 100) if total > 0 else 0
        overall_int = int(clamp_pct(overall_raw))
        bar = self.sd_table.cellWidget(row, 4)
        if bar:
            bar.setValue(overall_int)
            bar.setFormat(f"{overall_int}%")
        # 速度列(5) - 始终显示
        speed_item = self.sd_table.item(row, 5)
        if speed_item:
            if speed > 0:
                speed_item.setText(f"{speed:.1f} MB/s")
            else:
                speed_item.setText("0.0 MB/s")
        # 剩余时间列(6) - 始终显示
        eta_item = self.sd_table.item(row, 6)
        if eta_item:
            if total > 0 and speed > 0:
                remaining_bytes = max(0, total - copied)
                remaining_seconds = remaining_bytes / (speed * 1024 * 1024)
                eta_item.setText(format_eta(remaining_seconds))
            elif total > 0 and copied > 0:
                eta_item.setText("计算中")
            else:
                eta_item.setText("--")

    def _on_completed(self, letter, dest_letter, dest_path, bytes_tx, file_count):
        self.session_transferred += bytes_tx
        self.session_sd_count += 1
        if self.test_mode_cb.isChecked():
            self._test_processed.add(letter)
        # Mark as completed so auto-poll won't restart it (prevents delete loop)
        self._completed_sd.add(letter)
        self._log(f"[{letter}] [OK] 传输完成: {format_size(bytes_tx)} -> {dest_letter}: {dest_path}")
        row = self._find_row(letter)
        if row >= 0:
            bar = self.sd_table.cellWidget(row, 4)
            if bar:
                bar.setValue(100)
                bar.setFormat("100%")
            speed_item = self.sd_table.item(row, 5)
            if speed_item:
                speed_item.setText("--")
            eta_item = self.sd_table.item(row, 6)
            if eta_item:
                eta_item.setText("已完成")
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
        # Mark completed to prevent auto-restart loop (user can manually restart)
        self._completed_sd.add(letter)
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
        btn_text = ""
        row = self._find_row(letter)
        if row >= 0:
            btn = self.sd_table.cellWidget(row, 7)
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
                self._test_processed.discard(letter)
                self._completed_sd.discard(letter)
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
        self.sd_table.setItem(row, 5, QTableWidgetItem("--"))
        self.sd_table.setItem(row, 6, QTableWidgetItem("--"))
        btn = QPushButton("取消")
        btn.setEnabled(False)
        btn.clicked.connect(lambda checked, l=d.letter: self._on_action_btn(l))
        self.sd_table.setCellWidget(row, 7, btn)

    def _set_sd_button(self, letter, text):
        row = self._find_row(letter)
        if row >= 0:
            btn = self.sd_table.cellWidget(row, 7)
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
            "已传(测试)": QColor(0, 128, 0),
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
        self._serial_refresh_timer.stop()
        event.accept()
