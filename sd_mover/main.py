"""SD卡自动转存工具 - 主函数"""

import sys

from PyQt5.QtWidgets import QApplication
from PyQt5.QtGui import QFont

from sd_mover.main_window import MainWindow


def main():
    # 全局异常兜底: 未捕获异常不闪退, 记录到日志文件
    _orig_excepthook = sys.excepthook
    _log_file = None
    try:
        import os
        log_dir = os.path.join(os.path.dirname(sys.executable)
                               if getattr(sys, 'frozen', False) else '.', 'logs')
        os.makedirs(log_dir, exist_ok=True)
        _log_file = open(os.path.join(log_dir, 'crash.log'), 'a', encoding='utf-8')
    except Exception:
        pass

    def _hook(exc_type, exc_value, exc_tb):
        try:
            import traceback
            msg = ''.join(traceback.format_exception(exc_type, exc_value, exc_tb))
            if _log_file:
                _log_file.write(msg + '\n')
                _log_file.flush()
        except Exception:
            pass
        # GUI线程的异常也走原hook(打印到stderr), 但绝不退出进程
        try:
            _orig_excepthook(exc_type, exc_value, exc_tb)
        except Exception:
            pass

    sys.excepthook = _hook

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
