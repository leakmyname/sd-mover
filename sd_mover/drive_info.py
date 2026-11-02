"""驱动器信息数据类与扫描功能"""

from dataclasses import dataclass
from typing import List

from .winapi import (
    get_logical_drives, get_drive_type, get_volume_info,
   get_disk_usage, get_hardware_serial, DRIVE_REMOVABLE, DRIVE_CDROM
)
from .constants import SD_CARD_MIN_GB, SD_CARD_MAX_GB, MIN_EXT_DRIVE_GB


@dataclass
class DriveInfo:
    letter: str
    label: str
    serial: int
    total: int
    free: int
    drive_type: int
    fs: str
    hw_serial: str = ""
    reader_serial: str = ""

    @property
    def root(self):
        return f"{self.letter}:\\"

    @property
    def is_removable(self):
        return self.drive_type == DRIVE_REMOVABLE

    @property
    def size_gb(self):
        return self.total / (1024 ** 3)

    @property
    def free_gb(self):
        return self.free / (1024 ** 3)

    @property
    def is_sd_card(self):
        """判断是否为SD卡: 按标签或按可移动+容量范围"""
        label_upper = self.label.upper()
        if 'SD' in label_upper or 'SDXC' in label_upper or 'SDHC' in label_upper:
            return True
        if self.is_removable and SD_CARD_MIN_GB <= self.size_gb <= SD_CARD_MAX_GB:
            return True
        return False

    @property
    def is_external_drive(self):
        """判断是否为目标外部硬盘(700G+), 排除系统盘"""
        if self.letter in ('C',):
            return False
        return self.size_gb >= MIN_EXT_DRIVE_GB


def scan_drives() -> List[DriveInfo]:
    """扫描所有可用驱动器，返回 DriveInfo 列表"""
    drives = []
    for letter in get_logical_drives():
        root = f"{letter}:\\"
        dtype = get_drive_type(root)
        if dtype == DRIVE_CDROM:
            continue
        label, serial, fs = get_volume_info(root)
        if label is None:
            continue
        total, free = get_disk_usage(root)
        if total == 0:
            continue
        drives.append(DriveInfo(
            letter=letter, label=label, serial=serial,
            total=total, free=free, drive_type=dtype, fs=fs or '',
            hw_serial="",
            reader_serial=""
        ))
    return drives
