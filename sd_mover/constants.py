"""应用常量配置"""

# 硬盘识别
MIN_EXT_DRIVE_GB = 700      # 目标硬盘最小容量(GB)
SD_CARD_MIN_GB = 100        # SD卡容量范围下限(GB)
SD_CARD_MAX_GB = 400        # SD卡容量范围上限(GB)

# 传输参数
CHUNK_SIZE = 8 * 1024 * 1024   # 8MB 分块拷贝
POLL_INTERVAL_MS = 2000        # 驱动器轮询间隔(ms)
STABILIZE_CHECKS = 2           # 稳定检测次数(防止相机还在写入)
WARNING_THRESHOLD_GB = 10     # 剩余空间警告阈值(GB)

# 跳过的系统文件/目录(小写)
SKIP_ITEMS = {
    'system volume information',
    '$recycle.bin',
    '.trashes',
    '.fseventsd',
    '.spotlight-v100',
    'desktop.ini',
    'thumbs.db',
    '.ds_store',
}

# 默认配置
DEFAULT_PREFIX = 'ego'
DEFAULT_AUTO_EJECT = True
DEFAULT_AUTO_DELETE = True
DEFAULT_START_DELAY_SEC = 60   # SD卡插入后延迟开始(秒)
DEFAULT_AUTO_EJECT_TEST = False  # 测试模式: 不真弹出/不真删除
