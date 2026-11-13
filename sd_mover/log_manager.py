"""日志持久化管理 - 简洁易读的传输记录 + 每日汇总"""

import os
import json
from datetime import datetime


def format_size(size_bytes):
    size = float(size_bytes)
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


class LogManager:
    """管理持久化日志: 简洁文本记录 + 每日汇总"""

    def __init__(self, log_dir):
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)
        self.record_path = os.path.join(log_dir, '传输记录.txt')
        self.daily_path = os.path.join(log_dir, '每日汇总.txt')
        self.stats_path = os.path.join(log_dir, 'session_stats.json')

    def log_transfer(self, sd_letter, sd_label, sd_size,
                     dest_letter, dest_label, folder,
                     bytes_tx, file_count, status, note=''):
        """写入简洁的传输记录 - 一行一条, 一眼能看懂"""
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        size_str = format_size(bytes_tx)
        # 部分完成时把跳过的字节数也写进记录, 对账时能看出差了多少
        skip_str = ""
        if status and '部分' in status:
            skip_str = f", 跳过{format_size(note)}" if note else ", 有跳过"
        line = (
            f"{ts} | SD卡 {sd_letter}: ({sd_label}) {sd_size} | "
            f"-> {dest_letter}: ({dest_label}) | "
            f"{folder} | {size_str} | {file_count}个文件 | [{status}{skip_str}]\n"
        )
        with open(self.record_path, 'a', encoding='utf-8') as f:
            f.write(line)
        self._rebuild_daily_summary()

    def _rebuild_daily_summary(self):
        """从传输记录重新生成每日汇总"""
        if not os.path.exists(self.record_path):
            return
        daily_data = {}
        with open(self.record_path, 'r', encoding='utf-8') as f:
            for line in f:
                parts = line.strip().split('|')
                if len(parts) < 7:
                    continue
                date_str = parts[0].strip()[:10]
                size_str = parts[4].strip()
                status = parts[6].strip()
                # 只统计真正搬完的卡。带"部分"的是没搬完的, 不能计入对账。
                if '完成' not in status or '部分' in status:
                    continue
                size_bytes = self._parse_size(size_str)
                if date_str not in daily_data:
                    daily_data[date_str] = [0, 0]
                daily_data[date_str][0] += 1
                daily_data[date_str][1] += size_bytes
        with open(self.daily_path, 'w', encoding='utf-8') as f:
            f.write("===== 每日传输汇总 =====\n\n")
            for date_str in sorted(daily_data.keys()):
                count, total = daily_data[date_str]
                f.write(f"{date_str} | {count}张SD卡 | {format_size(total)}\n")
            total_count = sum(v[0] for v in daily_data.values())
            total_bytes = sum(v[1] for v in daily_data.values())
            f.write(f"\n总计: {total_count}张SD卡 | {format_size(total_bytes)}\n")

    def _parse_size(self, size_str):
        try:
            parts = size_str.strip().split()
            num = float(parts[0])
            unit = parts[1] if len(parts) > 1 else 'B'
            multipliers = {'B': 1, 'KB': 1024, 'MB': 1024**2,
                          'GB': 1024**3, 'TB': 1024**4}
            return int(num * multipliers.get(unit, 1))
        except Exception:
            return 0

    def load_session_stats(self):
        try:
            with open(self.stats_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {
                'total_sessions': 0,
                'total_sd_cards': 0,
                'total_bytes': 0,
                'total_files': 0,
                'drives_used': [],
            }

    def save_session_stats(self, stats):
        with open(self.stats_path, 'w', encoding='utf-8') as f:
            json.dump(stats, f, indent=2, ensure_ascii=False)

    def add_transfer(self, sd_letter, sd_label, sd_size,
                     dest_letter, dest_label, folder,
                     bytes_tx, file_count, status, note='', skipped_bytes=0):
        self.log_transfer(
            sd_letter, sd_label, sd_size,
            dest_letter, dest_label, folder,
            bytes_tx, file_count, status, note
        )
        stats = self.load_session_stats()
        # 对账口径: 只有"完整搬完一块卡"才计数。
        # 部分完成(有放不下的文件被跳过)不计入张数, 也不计入总量,
        # 否则每日汇总会虚增, 和实际搬过去的量对不上。
        full_ok = ('完成' in status) and ('部分' not in status)
        if full_ok:
            stats['total_sd_cards'] += 1
            stats['total_bytes'] += bytes_tx
            stats['total_files'] += file_count
            if dest_letter and dest_letter not in stats.get('drives_used', []):
                stats.setdefault('drives_used', []).append(dest_letter)
        else:
            stats['partial_count'] = stats.get('partial_count', 0) + 1
            stats['partial_bytes_skipped'] = stats.get(
                'partial_bytes_skipped', 0) + int(skipped_bytes or 0)
        self.save_session_stats(stats)
        return stats

    def increment_session(self):
        stats = self.load_session_stats()
        stats['total_sessions'] += 1
        self.save_session_stats(stats)
        return stats
