"""Windows API 辅助函数 (通过 ctypes)"""

import ctypes
import json
import subprocess
import threading

# Windows API 常量
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FILE_SHARE_READ = 1
FILE_SHARE_WRITE = 2
FSCTL_LOCK_VOLUME = 0x00090018
FSCTL_DISMOUNT_VOLUME = 0x00090020
IOCTL_STORAGE_EJECT_MEDIA = 0x002D4808
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_CDROM = 5

_kernel32 = ctypes.windll.kernel32
_kernel32.CreateFileW.restype = ctypes.c_void_p

# 隐藏子进程(PowerShell)窗口, 避免 STARTUPINFO 闪现
_CREATE_NO_WINDOW = 0x08000000

# ── 序列号缓存 (批量查询, 避免频繁开 PowerShell) ──
_serial_cache = {}        # letter -> {"usb_serial", "disk_serial", "reader_serial"}
_cache_drive_key = None   # 上次查询时的盘符集合, 用于判断是否需要刷新
_refresh_lock = threading.Lock()
_refresh_thread = None    # 当前后台刷新线程
_refresh_callback = None  # 刷新完成回调
_refresh_done_flag = False  # 刷新完成标志(供UI线程轮询)
_refresh_done_drives = None  # 刷新完成时的盘符集合


def get_logical_drives():
    """获取所有可用驱动器盘符列表"""
    mask = _kernel32.GetLogicalDrives()
    return [chr(ord('A') + i) for i in range(26) if mask & (1 << i)]


def get_drive_type(root_path):
    """获取驱动器类型"""
    return _kernel32.GetDriveTypeW(root_path)


def get_volume_info(root_path):
    """获取卷标、序列号、文件系统类型。返回 (label, serial, fs) 或 (None, 0, None)"""
    label_buf = ctypes.create_unicode_buffer(1024)
    serial = ctypes.c_ulong(0)
    max_comp = ctypes.c_ulong(0)
    fs_flags = ctypes.c_ulong(0)
    fs_buf = ctypes.create_unicode_buffer(1024)
    success = _kernel32.GetVolumeInformationW(
        root_path, label_buf, 1024,
        ctypes.byref(serial), ctypes.byref(max_comp),
        ctypes.byref(fs_flags), fs_buf, 1024
    )
    if not success:
        return None, 0, None
    return label_buf.value, serial.value, fs_buf.value


def get_disk_usage(root_path):
    """获取磁盘总空间和剩余空间(字节)"""
    free_bytes = ctypes.c_ulonglong(0)
    total_bytes = ctypes.c_ulonglong(0)
    available = ctypes.c_ulonglong(0)
    _kernel32.GetDiskFreeSpaceExW(
        root_path,
        ctypes.byref(available),
        ctypes.byref(total_bytes),
        ctypes.byref(free_bytes)
    )
    return total_bytes.value, free_bytes.value


def format_size(size_bytes):
    """格式化字节大小为可读字符串"""
    size = float(size_bytes)
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def eject_drive(drive_letter):
    """弹出驱动器，返回是否成功"""
    try:
        volume_path = f"\\\\.\\{drive_letter}:"
        handle = _kernel32.CreateFileW(
            volume_path,
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None, OPEN_EXISTING, 0, None
        )
        if handle == INVALID_HANDLE_VALUE or handle is None:
            return _eject_shell(drive_letter)

        bytes_returned = ctypes.c_ulong(0)
        _kernel32.DeviceIoControl(
            handle, FSCTL_LOCK_VOLUME,
            None, 0, None, 0, ctypes.byref(bytes_returned), None
        )
        _kernel32.DeviceIoControl(
            handle, FSCTL_DISMOUNT_VOLUME,
            None, 0, None, 0, ctypes.byref(bytes_returned), None
        )
        result = _kernel32.DeviceIoControl(
            handle, IOCTL_STORAGE_EJECT_MEDIA,
            None, 0, None, 0, ctypes.byref(bytes_returned), None
        )
        _kernel32.CloseHandle(handle)

        if not result:
            return _eject_shell(drive_letter)
        return True
    except Exception:
        return _eject_shell(drive_letter)


def _eject_shell(drive_letter):
    """通过 PowerShell Shell.Application 弹出(备用方案)"""
    try:
        ps = (
            "$shell = New-Object -ComObject Shell.Application; "
            f"$d = $shell.Namespace(17).ParseName('{drive_letter}:'); "
            "if ($d) { $d.InvokeVerb('Eject') }"
        )
        subprocess.run(
            ['powershell', '-NoProfile', '-Command', ps],
            capture_output=True, timeout=15,
            creationflags=_CREATE_NO_WINDOW
        )
        return True
    except Exception:
        return False


# ── 序列号批量查询 (替代原来每次开 PowerShell) ──

_PS_SCRIPT_TEMPLATE = '''
$ErrorActionPreference='SilentlyContinue'
$result = @{}
$letters = '%s'.Split(',')
foreach ($letter in $letters) {
    $info = @{usb_serial=''; disk_serial=''; reader_serial=''}
    $part = Get-Partition -DriveLetter $letter 2>$null
    if ($part) {
        $disk = Get-Disk -Number $part.DiskNumber 2>$null
        if ($disk -and $disk.SerialNumber) {
            $info.disk_serial = ($disk.SerialNumber -replace '\\s','').Trim()
        }
        $diskNum = $part.DiskNumber
        $diskObj = Get-CimInstance Win32_DiskDrive | Where-Object {$_.Index -eq $diskNum}
        if ($diskObj -and $diskObj.PNPDeviceID) {
            $pnp = $diskObj.PNPDeviceID
            # Method 1: Get-PnpDevice parent tree traversal (best for USB enclosures)
            try {
                $dev = Get-PnpDevice -InstanceId $pnp 2>$null
                if ($dev) {
                    $current = $dev
                    for ($i = 0; $i -lt 20; $i++) {
                        $instId = $current.InstanceId
                        if ($instId -like 'USB\\*') {
                            $parts = $instId.Split('\\')
                            if ($parts.Length -ge 3 -and $parts[2] -ne '') {
                                $serial = $parts[2]
                                if ($serial -notmatch '^(root|hub|0&0|0000)$') {
                                    $info.usb_serial = $serial
                                    $info.reader_serial = $serial
                                    break
                                }
                            }
                        }
                        $parentId = $current.ParentDeviceId
                        if (-not $parentId -or $parentId -eq $instId) { break }
                        $current = Get-PnpDevice -InstanceId $parentId 2>$null
                        if (-not $current) { break }
                    }
                }
            } catch {}
            # Method 2: Direct PNP check
            if (-not $info.usb_serial -and $pnp -like 'USB\\*') {
                $parts = $pnp.Split('\\')
                if ($parts.Length -ge 3 -and $parts[2] -ne '') {
                    $info.usb_serial = $parts[2]
                    $info.reader_serial = $parts[2]
                }
            }
            # Method 3: Win32_USBControllerDevice fallback
            if (-not $info.usb_serial) {
                $usbDevs = Get-CimInstance Win32_USBControllerDevice
                foreach ($usbDev in $usbDevs) {
                    $dep = $usbDev.Dependent
                    $ant = $usbDev.Antecedent
                    if ($dep -and $dep -match $diskObj.DeviceID) {
                        $antPnp = ($ant -replace '.*DeviceID="([^"]*)".*','$1')
                        if ($antPnp -like 'USB\\*') {
                            $antParts = $antPnp.Split('\\')
                            if ($antParts.Length -ge 3 -and $antParts[2] -ne '') {
                                $info.usb_serial = $antParts[2]
                                $info.reader_serial = $antParts[2]
                            }
                        }
                    }
                }
            }
        }
    }
    $result[$letter] = $info
}
Write-Output ($result | ConvertTo-Json -Compress -Depth 3)
'''


def refresh_serial_cache(letters=None, callback=None):
    """批量查询所有盘符的 USB 序列号 — 只开一次 PowerShell

    letters: 要查询的盘符列表, None=自动获取全部
    callback: 刷新完成后的回调函数(无参数), 用于后台刷新通知UI

    结果存入 _serial_cache, key=盘符
    每个值: {"usb_serial", "disk_serial", "reader_serial"}
    """
    global _cache_drive_key, _refresh_thread, _refresh_callback
    if letters is None:
        letters = get_logical_drives()
    drive_key = frozenset(letters)
    with _refresh_lock:
        if drive_key == _cache_drive_key and _serial_cache:
            # 缓存有效, 立即回调(若有)
            if callback:
                callback()
            return
        # 如果已有后台线程在跑且盘符集合相同, 不重复启动
        if _refresh_thread and _refresh_thread.is_alive():
            # 记录回调, 等现有线程完成后触发
            if callback:
                _refresh_callback = callback
            return
        _cache_drive_key = drive_key
        if callback:
            _refresh_callback = callback
        _refresh_thread = threading.Thread(
            target=_do_refresh, args=(list(letters),), daemon=True
        )
        _refresh_thread.start()


def _do_refresh(letters):
    """后台线程: 实际执行 PowerShell 查询"""
    global _refresh_callback, _refresh_done_flag, _refresh_done_drives
    try:
        if not letters:
            with _refresh_lock:
                _serial_cache.clear()
            return
        letters_str = ",".join(letters)
        ps_script = _PS_SCRIPT_TEMPLATE % letters_str
        result = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command', ps_script],
            capture_output=True, timeout=60, text=True,
            creationflags=_CREATE_NO_WINDOW
        )
        raw = result.stdout.strip()
        new_cache = {}
        if raw:
            data = json.loads(raw)
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, dict):
                        new_cache[k] = {
                            "usb_serial": v.get("usb_serial", "") or "",
                            "disk_serial": v.get("disk_serial", "") or "",
                            "reader_serial": v.get("reader_serial", "") or "",
                        }
                    else:
                        new_cache[k] = {"usb_serial": "", "disk_serial": "", "reader_serial": ""}
        with _refresh_lock:
            _serial_cache.clear()
            _serial_cache.update(new_cache)
            _refresh_done_flag = True
            _refresh_done_drives = frozenset(letters)
    except Exception:
        # 保持旧缓存, 触发回调
        with _refresh_lock:
            _refresh_done_flag = True
            _refresh_done_drives = frozenset(letters)


def get_cached_usb_serial(letter):
    """从缓存获取 USB 硬盘盒序列号 (优先 usb_serial, 回退 disk_serial)"""
    info = _serial_cache.get(letter, {})
    return info.get("usb_serial", "") or info.get("disk_serial", "")


def get_cached_reader_serial(letter):
    """从缓存获取 SD 读卡器 USB 序列号"""
    info = _serial_cache.get(letter, {})
    return info.get("reader_serial", "")


def get_hardware_serial(drive_letter):
    """获取 USB 硬盘盒序列号 (兼容旧调用, 同步刷新)"""
    # 对于兼容调用, 做一次同步刷新(不带回调)
    with _refresh_lock:
        drive_key = frozenset([drive_letter])
        if drive_key != _cache_drive_key or not _serial_cache:
            pass  # 落到下面的同步刷新
        else:
            return get_cached_usb_serial(drive_letter)
    # 同步刷新(用于兼容旧接口)
    global _refresh_thread
    if _refresh_thread and _refresh_thread.is_alive():
        _refresh_thread.join(timeout=60)
    # 直接同步执行
    _do_refresh_sync([drive_letter])
    return get_cached_usb_serial(drive_letter)


def _do_refresh_sync(letters):
    """同步版刷新(兼容旧接口用)"""
    global _cache_drive_key
    if not letters:
        _serial_cache.clear()
        return
    letters_str = ",".join(letters)
    ps_script = _PS_SCRIPT_TEMPLATE % letters_str
    try:
        result = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command', ps_script],
            capture_output=True, timeout=60, text=True,
            creationflags=_CREATE_NO_WINDOW
        )
        raw = result.stdout.strip()
        new_cache = {}
        if raw:
            data = json.loads(raw)
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, dict):
                        new_cache[k] = {
                            "usb_serial": v.get("usb_serial", "") or "",
                            "disk_serial": v.get("disk_serial", "") or "",
                            "reader_serial": v.get("reader_serial", "") or "",
                        }
                    else:
                        new_cache[k] = {"usb_serial": "", "disk_serial": "", "reader_serial": ""}
        with _refresh_lock:
            _serial_cache.clear()
            _serial_cache.update(new_cache)
            _cache_drive_key = frozenset(letters)
    except Exception:
        pass


def check_single_instance(name="SDMover_SingleInstance_Mutex"):
    """检查是否已有实例运行, 返回 True=可以运行, False=已有实例"""
    try:
        _kernel32.CreateMutexW(None, False, name)
        last_error = ctypes.GetLastError()
        if last_error == 183:  # ERROR_ALREADY_EXISTS
            return False
        return True
    except Exception:
        return True
def pop_refresh_done():
    """UI线程轮询: 检查后台刷新是否完成, 返回 True 表示已完成(并清除标志)"""
    global _refresh_done_flag, _refresh_done_drives
    with _refresh_lock:
        if _refresh_done_flag:
            _refresh_done_flag = False
            drives = _refresh_done_drives
            _refresh_done_drives = None
            return drives
        return None
