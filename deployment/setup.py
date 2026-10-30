"""Windows 首次安装：校验私有 Release 后恢复本机运行资源。"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import venv
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def run(*args):
    subprocess.run([str(x) for x in args], cwd=ROOT, check=True)


def main():
    if os.name != 'nt' or sys.version_info[:2] != (3, 11) or sys.maxsize < 2**32:
        raise RuntimeError('Use Windows 64-bit Python 3.11: https://www.python.org/downloads/windows/')
    if not shutil.which('gh'):
        raise RuntimeError('Install https://cli.github.com/ and run: gh auth login')
    if not (ROOT / 'data').exists() and shutil.disk_usage(ROOT).free < 6 * 1024**3:
        raise RuntimeError('At least 6 GB free disk space is required; 10 GB is recommended')
    run('gh', 'auth', 'status')
    pin = json.loads((ROOT / 'deployment/resources.json').read_text(encoding='utf-8'))
    if sha(ROOT / 'config.json') != pin['config_sha256']:
        raise RuntimeError('Restore config.json to match this resource release before setup')
    python = ROOT / '.venv/Scripts/python.exe'
    print('Creating/checking Python environment and installing dependencies...', flush=True)
    if not python.exists():
        venv.create(ROOT / '.venv', with_pip=True)
    run(python, '-X', 'utf8', '-m', 'pip', 'install', '--no-compile', '-r', 'requirements-lock.txt')
    cache = ROOT / '.install-cache'
    cache.mkdir(exist_ok=True)
    restoring = cache / 'restore-started.json'
    marker = ROOT / 'data/round1_20261023/installation.json'
    if marker.exists() and json.loads(marker.read_text(encoding='utf-8')).get('root') == str(ROOT):
        run(python, '-X', 'utf8', 'deployment/restore_runtime.py', '--check-only')
        print('READY: run start_v1.cmd')
        return
    resumable = restoring.exists() and json.loads(restoring.read_text(encoding='utf-8')).get('root') == str(ROOT)
    if (ROOT / 'data/round1_20261023/index/current.json').exists() and not (ROOT / 'runtime-files.json').exists() and not resumable:
        raise RuntimeError('Existing custom data found; install into a clean source directory')
    # 下载/解压中断时保持门禁关闭，不能沿用随源码保留的历史成功记录。
    state = ROOT / 'reports/round1_20261023/run_status.json'
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({'phase': 'checking', 'scope': 'portable installation'}), encoding='utf-8')
    for asset in pin['assets']:
        print('Downloading/verifying ' + asset['name'] + '...', flush=True)
        archive = cache / asset['name']
        if not archive.exists() or sha(archive) != asset['sha256']:
            run('gh', 'release', 'download', pin['tag'], '--repo', pin['repository'],
                '--pattern', asset['name'], '--dir', cache, '--clobber')
        if archive.stat().st_size != asset['bytes'] or sha(archive) != asset['sha256']:
            raise RuntimeError('Resource checksum mismatch: ' + asset['name'])
        restoring.write_text(json.dumps({'root': str(ROOT)}), encoding='utf-8')
        # 仅允许发行包中的资源目录，拒绝路径穿越和源码覆盖。
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                p = PurePosixPath(info.filename)
                if (p.is_absolute() or '..' in p.parts or '\\' in info.filename
                        or not p.parts or p.parts[0] not in
                        {'data', 'reports', 'doris-website-master', 'third_party', 'runtime-files.json'}):
                    raise RuntimeError('Unsafe archive entry: ' + info.filename)
                if not (ROOT / info.filename).resolve().is_relative_to(ROOT):
                    raise RuntimeError('Archive path escapes project')
            print('Extracting model, documents and index; please wait...', flush=True)
            z.extractall(ROOT)
    if not (ROOT / '.env').exists():
        shutil.copyfile(ROOT / '.env.example', ROOT / '.env')
    run(python, '-X', 'utf8', 'deployment/restore_runtime.py')
    print('READY: configure .env, run start_v1.cmd, open http://127.0.0.1:8765')


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as exc:
        print('SETUP FAILED: ' + str(exc), file=sys.stderr)
        sys.exit(1)
