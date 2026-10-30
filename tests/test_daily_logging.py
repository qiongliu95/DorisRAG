import io
import logging
import logging.config
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from doris_rag.runtime_logging import DailyFileHandler, LOG_TIMEZONE, server_log_config


def record(text, stamp):
    value = logging.LogRecord('uvicorn.error',logging.INFO,'',1,text,(),None)
    value.created = datetime.fromisoformat(stamp).replace(tzinfo=LOG_TIMEZONE).timestamp()
    return value


class DailyLoggingTests(unittest.TestCase):
    def test_midnight_idle_year_boundary_and_reopen_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler=DailyFileHandler(tmp);handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
            values=[('中文日志一','2026-12-31T23:59:59'),('跨日日志二','2027-01-01T00:00:00'),
                    ('空闲后日志三','2027-01-03T09:00:00'),('迟到记录','2026-12-31T23:59:58')]
            for text,stamp in values: handler.handle(record(text,stamp))
            handler.close()
            again=DailyFileHandler(tmp);again.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
            again.handle(record('重启后追加','2027-01-03T10:00:00'));again.close()
            actual={p.name:p.read_text(encoding='utf-8').splitlines() for p in Path(tmp).glob('*.log')}
            self.assertEqual(actual,{'server-2026-12-31.log':['INFO: 中文日志一','INFO: 迟到记录'],
                'server-2027-01-01.log':['INFO: 跨日日志二'],
                'server-2027-01-03.log':['INFO: 空闲后日志三','INFO: 重启后追加']})

    def test_concurrent_records_not_lost_or_duplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler=DailyFileHandler(tmp);handler.setFormatter(logging.Formatter('%(message)s'))
            def emit(n):handler.handle(record(str(n),'2026-10-28T12:00:00' if n%2 else '2026-10-29T00:00:00'))
            with ThreadPoolExecutor(8) as pool:list(pool.map(emit,range(200)))
            handler.close()
            rows=[s for p in Path(tmp).glob('*.log') for s in p.read_text(encoding='utf-8').splitlines()]
            self.assertEqual(len(rows),200);self.assertEqual(set(rows),set(map(str,range(200))))

    def test_uvicorn_access_error_format_and_no_duplicate_setup(self):
        from uvicorn.config import LOGGING_CONFIG
        self.addCleanup(logging.config.dictConfig, LOGGING_CONFIG)
        with tempfile.TemporaryDirectory() as tmp,patch('sys.stderr',new_callable=io.StringIO),patch('sys.stdout',new_callable=io.StringIO):
            for i in range(2):
                logging.config.dictConfig(server_log_config(tmp))
                logging.getLogger('uvicorn.error').info('服务消息%s',i)
                logging.getLogger('uvicorn.access').info('%s - "%s %s HTTP/%s" %d','127.0.0.1:123','GET','/health','1.1',200)
            for name in ('uvicorn','uvicorn.access'):
                for handler in logging.getLogger(name).handlers:handler.flush();handler.close()
            rows=next(Path(tmp).glob('*.log')).read_text(encoding='utf-8').splitlines()
            self.assertEqual(len(rows),4)
            self.assertEqual(rows[0],'INFO:     服务消息0')
            self.assertEqual(rows[1],'INFO:     127.0.0.1:123 - "GET /health HTTP/1.1" 200 OK')
            self.assertNotIn('\x1b','\n'.join(rows))
