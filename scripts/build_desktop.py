"""Build a standalone Windows executable; release smoke testing is separate."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--version', default='0.1.0-preview.1')
    args = parser.parse_args()
    if platform.system() != 'Windows' or platform.machine().lower() not in {'amd64', 'x86_64'}:
        parser.error('Build the Windows x64 package on Windows x64')
    root = Path(__file__).resolve().parents[1]
    if subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain'], text=True).strip():
        raise SystemExit('Commit the reviewed source before producing a release')
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    name = 'Lokvetia-Core-' + args.version + '-win-x64'
    from agent_factory.studio_updates import version_key
    version_key(args.version)
    revision = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    build_info = out / 'lokvetia-build.json'
    build_info.write_text(json.dumps({'version':args.version,'source_commit':revision}),encoding='utf-8')
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onefile', '--windowed',
                    '--noupx', '--name', name, '--distpath', str(out), '--workpath', str(out / 'build'),
                    '--specpath', str(out / 'build'), '--paths', str(root / 'src'),
                    '--add-data', str(build_info)+';.',
                    '--collect-all', 'agent_factory', '--collect-all', 'temporalio',
                    '--collect-all', 'uvicorn', '--collect-all', 'pypdf',
                    '--copy-metadata', 'agent-factory-orchestrator',
                    '--recursive-copy-metadata', 'temporalio',
                    '--recursive-copy-metadata', 'fastapi', '--copy-metadata', 'uvicorn',
                    '--copy-metadata', 'pypdf', '--copy-metadata', 'python-multipart',
                    '--hidden-import', 'tkinter', '--hidden-import', 'tkinter.messagebox',
                    str(root / 'scripts/desktop_entry.py')], check=True)
    artifact = out / (name + '.exe')
    with artifact.open('rb') as file:
        digest = hashlib.file_digest(file, 'sha256').hexdigest()
    revision = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    manifest = dict(schema_version=1, version=args.version, source_commit=revision,
                    channel='preview', signed=False, tested=[],
                    artifacts=[dict(name=artifact.name, platform='windows-x64',
                                    size_bytes=artifact.stat().st_size, sha256=digest)])
    (out / 'desktop-release.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    (out / 'SHA256SUMS.txt').write_text(digest + '  ' + artifact.name + '\n', encoding='ascii')
    print(json.dumps(manifest))


if __name__ == '__main__':
    main()
