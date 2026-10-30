"""沿用Uvicorn标准logging及格式，运行日志按北京时间自然日追加。"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path

from .common import ROOT

LOG_TIMEZONE = timezone(timedelta(hours=8))


class DailyFileHandler(logging.FileHandler):
    """按记录产生日期切换，空闲跨日无需定时线程；历史文件只追加。"""
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.day = datetime.now(LOG_TIMEZONE).date()
        super().__init__(self.directory / f'server-{self.day.isoformat()}.log',
                         mode='a', encoding='utf-8', delay=True)

    def emit(self, record):
        # Handler.handle持有锁：并发emit的关闭、换名、追加是同一临界区。
        try:
            day = datetime.fromtimestamp(record.created, LOG_TIMEZONE).date()
            if day != self.day:
                if self.stream is not None:
                    self.stream.close()
                    self.stream = None
                self.baseFilename = str(self.directory / f'server-{day.isoformat()}.log')
                self.day = day
            super().emit(record)
        except Exception:
            self.handleError(record)


def server_log_config(directory=None):
    from uvicorn.config import LOGGING_CONFIG
    cfg = deepcopy(LOGGING_CONFIG)
    # 控制台处理器原样保留；文件使用相同文本格式，仅关闭终端颜色转义。
    for name in ('default', 'access'):
        formatter = 'file_' + name
        cfg['formatters'][formatter] = {**cfg['formatters'][name], 'use_colors': False}
        cfg['handlers'][formatter] = {
            '()': DailyFileHandler, 'directory': str(directory or ROOT / 'logs/runtime'),
            'formatter': formatter}
        logger = 'uvicorn' if name == 'default' else 'uvicorn.access'
        cfg['loggers'][logger]['handlers'].append(formatter)
    return cfg
