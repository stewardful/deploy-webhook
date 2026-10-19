#!/usr/bin/env python3
"""Build an architecture-independent offline server-tool bundle and SHA256 file."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from install import BUNDLE_FILES, VERSION


def build(source, output):
    output.mkdir(parents=True, exist_ok=True)
    data = {}
    for name in BUNDLE_FILES:
        path = source/name
        if path.is_symlink() or not path.is_file(): raise ValueError('invalid package source')
        data[name] = path.read_bytes()
    data['manifest.json'] = (json.dumps({'schema_version': 1, 'version': VERSION,
        'files': {name: hashlib.sha256(value).hexdigest() for name, value in data.items()}}, sort_keys=True, indent=2)+'\n').encode()
    stem = 'deploy-delivery-'+VERSION
    archive = output/(stem+'.tar.gz')
    with archive.open('wb') as target, gzip.GzipFile(fileobj=target, mode='wb', mtime=0, filename='') as compressed:
        with tarfile.open(fileobj=compressed, mode='w|') as bundle:
            for name, content in sorted(data.items()):
                info = tarfile.TarInfo(stem+'/'+name)
                info.size = len(content); info.mode = 0o644
                bundle.addfile(info, io.BytesIO(content))
    checksum = archive.with_suffix('.gz.sha256')
    checksum.write_text(hashlib.sha256(archive.read_bytes()).hexdigest()+'  '+archive.name+'\n')
    return archive


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('dist'))
    args = parser.parse_args()
    print(build(Path(__file__).resolve().parent.parent, args.output))
