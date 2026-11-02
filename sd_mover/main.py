"""SD卡自动转存工具 - 主函数"""

import sys

from PyQt5.QtWidgets import QApplication
from PyQt5.QtGui import QFont

from sd_mover.main_window import MainWindow


def main():
    # 单实例检查
    try:
        from sd_mover.winapi import check_single_instance
        if not check_single_instance():
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, "SD卡自动转存工具已在运行, 请勿重复启动。", "提示", 0x30)
            sys.exit(0)
    except Exception:
        pass

    app = QApplication(sys.argv)
    app.setApplicationName("SD卡自动转存工具")
    font = QFont("Microsoft YaHei", 9)
    app.setFont(font)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
