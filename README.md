# SD卡自动转存工具

Windows 桌面应用，自动将多张 SD 卡数据并行转存到外部移动硬盘。

## 功能

- 自动识别 700G+ 外部硬盘作为目标存储
- 自动识别 SD 卡（按 SDXC/SDHC 标签或可移动+容量范围）
- 多张 SD 卡同时传输，各自独立进度条
- 传输完成后自动清空 SD 卡并弹出
- 目标硬盘空间不足时弹出闪烁红色警告横幅
- 文件夹命名格式：`ego01_20261028_153000`（前缀+编号_日期时间）
- 系统托盘通知 + 蜂鸣提醒
- 设置持久化（前缀、最小硬盘容量、自动弹出/清空开关）

## 项目结构

```
sd_mover/
  __init__.py          # 包标识
  constants.py        # 常量配置（容量阈值、轮询间隔等）
  winapi.py           # Windows API 封装（驱动器扫描、弹出）
  drive_info.py       # DriveInfo 数据类 + 扫描逻辑
  transfer_worker.py  # 传输线程（分块拷贝、进度、清空、弹出）
  main_window.py      # PyQt5 主窗口 GUI
  main.py             # 入口函数
run.py                 # 直接运行入口
build_exe.bat          # Windows 打包脚本
requirements.txt       # 依赖

tests/                  # 逻辑验证脚本(不碰真实存储设备)
  run_all.py           # python tests/run_all.py 一次跑完
  verify_plan.py       # 并发选盘/预占/排队(max_hd 硬上限)
  verify_acct.py       # 对账只统计完整搬完的卡 + 每日汇总
  verify_mixed.py      # 256G/512G 混插 + 续传续用同一目录
  verify_scatter.py    # 打散溢出不产生半个文件 + 取消清理
  verify_conc2.py      # 多卡铺开到多块盘 + 进度/ETA/按钮列
```

## 打包

在 Windows 上运行：

```cmd
build_exe.bat
```

或手动执行：

```cmd
pyinstaller --onefile --name sd_mover --windowed --noconfirm --clean --add-data sd_mover;sd_mover run.py
```

输出：`dist\sd_mover.exe`（约 30MB，单文件）

测试脚本用假盘符和系统临时目录跑，不会读写任何真实 SD 卡或移动硬盘。

## 使用方法

1. 先连接 700G+ 移动硬盘
2. 运行 `sd_mover.exe`
3. 插入 SD 卡，工具自动检测并开始传输
4. 传输完成后 SD 卡自动清空并弹出
5. 可同时插入多张 SD 卡，并行传输

## 设置说明

- **命名前缀**：默认 `ego`，生成 `ego01`、`ego02`...
- **最小硬盘(GB)**：目标硬盘最低容量阈值，默认 700
- **自动弹出**：传输完成后自动弹出 SD 卡
- **自动清空**：传输完成后自动删除 SD 卡上的源文件
