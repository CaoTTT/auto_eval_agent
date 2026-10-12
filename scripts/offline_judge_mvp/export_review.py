#!/usr/bin/env python3
"""Run the artifact-tool workbook adapter from a temporary dependency context."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def main() -> None:
    p = argparse.ArgumentParser(description='导出专家审核 XLSX，需受支持的 artifact-tool 运行时')
    p.add_argument('--samples', required=True)
    p.add_argument('--drafts', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--source-name', default='原始测评集.xlsx')
    p.add_argument('--qa-dir')
    p.add_argument('--node', default=os.getenv('CODEX_PRIMARY_RUNTIME_NODE'))
    p.add_argument('--node-modules', default=os.getenv('CODEX_PRIMARY_RUNTIME_NODE_MODULES'))
    args = p.parse_args()
    source = Path(__file__).resolve().parent / 'export_review.mjs'
    node = args.node or shutil.which('node')
    modules = Path(args.node_modules).resolve() if args.node_modules else Path(__file__).resolve().parent / 'node_modules'
    if not node or not modules.is_dir() or not (modules / '@oai' / 'artifact-tool').exists():
        p.error('需要受支持的 Node/artifact-tool 运行时；请配置 --node 与 --node-modules，或在Work/Codex环境运行。Python制卡与审核导入可独立运行。')
    for path in [args.samples, args.drafts]:
        if not Path(path).is_file():
            p.error('输入文件不存在：' + str(path))
    with tempfile.TemporaryDirectory(prefix='judge-basis-export-') as working:
        directory = Path(working)
        script = directory / 'export_review.mjs'
        shutil.copy2(source, script)
        shutil.copy2(source.with_name('review_data.mjs'), directory / 'review_data.mjs')
        (directory / 'node_modules').symlink_to(modules, target_is_directory=True)
        command = [str(node), str(script), '--samples', str(Path(args.samples).resolve()),
                   '--drafts', str(Path(args.drafts).resolve()), '--out', str(Path(args.out).resolve()),
                   '--source-name', args.source_name]
        if args.qa_dir:
            command += ['--qa-dir', str(Path(args.qa_dir).resolve())]
        result = subprocess.run(command, cwd=directory, check=False)
        raise SystemExit(result.returncode)


if __name__ == '__main__':
    main()
