"""应用常量配置"""

# 硬盘识别
MIN_EXT_DRIVE_GB = 700    # 目标硬盘最小容量(GB)
SD_CARD_MIN_GB = 1        # SD卡容量范围下限(GB)
SD_CARD_MAX_GB = 600        # SD卡容量范围上限(GB) 覆盖512GB SDXC

# 传输参数
CHUNK_SIZE = 8 * 1024 * 1024   # 8MB 分块拷贝
POLL_INTERVAL_MS = 2000        # 驱动器轮询间隔(ms)
STABILIZE_CHECKS = 2           # 稳定检测次数(防止相机还在写入)
WARNING_THRESHOLD_GB = 10     # 剩余空间警告阈值(GB)
MAX_PATH_LEN = 255             # Windows MAX_PATH 安全限制(留余量)

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
DEFAULT_START_DELAY_SEC = 2   # SD卡插入后延迟开始(秒)
DEFAULT_AUTO_EJECT_TEST = False  # 测试模式: 不真弹出/不真删除
DEFAULT_MAX_CONCURRENT_SD = 10  # 同时读取SD卡数
DEFAULT_MAX_CONCURRENT_HD = 2  # 同时写入目标硬盘数
DEFAULT_SCATTER_MODE = True   # True=打散写入(跨盘拆分), False=整体文件(放不下跳过)

# 版本号: 日期+当日迭代, 例如 v11.12.3
APP_VERSION = "v11.12.6"
