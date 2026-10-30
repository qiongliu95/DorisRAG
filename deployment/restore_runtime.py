"""只迁移来源路径；保留向量、文本、chunk ID，并重新执行真实构建验收。"""
import json
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deployment.setup import ROOT, sha
SOURCE_ROOT = ROOT / json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))['source_root']


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def relocate(value):
    if isinstance(value, list):
        for row in value:
            relocate(row)
    elif isinstance(value, dict):
        if 'original_path' in value:
            # 不做全局字符串替换，文档正文、SQL 和历史语义字段保持原值。
            path = (SOURCE_ROOT / value['repo_path']).resolve()
            if not path.is_relative_to(ROOT / 'doris-website-master') or not path.is_file():
                raise ValueError('Invalid document path: ' + value['repo_path'])
            value['original_path'] = str(path)
        for child in value.values():
            if isinstance(child, (dict, list)):
                relocate(child)


def main():
    cfg = read(ROOT / 'config.json')
    data = ROOT / cfg['data_dir']
    report = ROOT / 'reports/round1_20261023'
    marker = data / 'installation.json'
    write(report / 'run_status.json', {'phase': 'checking', 'scope': 'portable installation'})
    if '--check-only' not in sys.argv:
        pin = read(ROOT / 'deployment/resources.json')
        if sha(ROOT / 'config.json') != pin['config_sha256']:
            raise ValueError('Config checksum mismatch')
        files = read(ROOT / 'runtime-files.json')
        print('Checking every extracted resource...', flush=True)
        for row in files:
            p = (ROOT / row['path']).resolve()
            if not p.is_relative_to(ROOT) or sha(p) != row['sha256']:
                raise ValueError('Resource mismatch: ' + row['path'])
        old_id = read(data / 'index/current.json')['build_id']
        old = data / 'builds' / old_id
        manifest = read(old / 'manifest.json')
        for name in ('documents.jsonl', 'chunks.jsonl'):
            path = data / 'processed' / name
            temporary = path.with_suffix('.migrating')
            with path.open(encoding='utf-8') as src, temporary.open('w', encoding='utf-8', newline='\n') as dst:
                for line in src:
                    row = json.loads(line)
                    relocate(row)
                    dst.write(json.dumps(row, ensure_ascii=False) + '\n')
            temporary.replace(path)
        manifest['documents_sha256'] = sha(data / 'processed/documents.jsonl')
        manifest['chunks_sha256'] = sha(data / 'processed/chunks.jsonl')
        build_id = manifest['chunks_sha256'][:12] + '-' + manifest['config_sha256'][:10]
        directory = data / 'builds' / build_id
        directory.mkdir(exist_ok=True)
        for name in ('vectors.npy', 'faiss.index'):
            shutil.copyfile(old / name, directory / name)
        rows = read(old / 'chunk_metadata.json')
        relocate(rows)
        write(directory / 'chunk_metadata.json', rows)
        del rows
        manifest['metadata_sha256'] = sha(directory / 'chunk_metadata.json')
        # 原清洗/切分验证只做路径迁移，明确记录其来源，不能宣称重新进行质量验收。
        verification = read(data / 'manifests/verification.json')
        verification.update({k: manifest[k] for k in ('documents_sha256', 'chunks_sha256')})
        verification['relocation'] = {'from_build': old_id, 'changes': 'original_path only',
                                      'source_integrity_rechecked_by': 'tests/check_knowledge_build.py'}
        write(data / 'manifests/verification.json', verification)
        manifest['verification_sha256'] = sha(data / 'manifests/verification.json')
        manifest['build_id'] = build_id
        write(directory / 'manifest.json', manifest)
        shutil.copyfile(ROOT / 'config.json', directory / 'config_snapshot.json')
        write(data / 'index/current.json', {'build_id': build_id})
    else:
        if read(marker)['root'] != str(ROOT):
            raise ValueError('Installation moved; rerun setup from the resource archive')
        build_id = read(data / 'index/current.json')['build_id']
    # 验收包含全量源文件 SHA、向量顺序、模型指纹、三个版本真实检索与来源回读。
    subprocess.run([sys.executable, 'tests/check_knowledge_build.py'], cwd=ROOT, check=True)
    checks = read(report / 'build_checks.json')
    if checks.get('passed') is not True or checks.get('build_id') != build_id:
        raise ValueError('Build acceptance did not pass')
    write(marker, {'root': str(ROOT), 'build_id': build_id})
    write(report / 'run_status.json', {'phase': 'completed', 'build_id': build_id,
                                      'scope': 'portable installation integrity and retrieval; no LLM acceptance'})
    print('Runtime restoration and retrieval acceptance PASSED', flush=True)


if __name__ == '__main__':
    main()
