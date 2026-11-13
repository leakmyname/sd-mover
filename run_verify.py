# -*- coding: utf-8 -*-
"""在 Linux 上跑 verify_*.py: 先打桩 ctypes.windll, 再执行目标脚本。"""
import sys, runpy, os
sys.path.insert(0, '/tmp')
import winshim  # noqa
target = sys.argv[1]
sys.argv = [target] + sys.argv[2:]
runpy.run_path(target, run_name='__main__')
