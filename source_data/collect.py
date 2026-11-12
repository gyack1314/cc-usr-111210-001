"""从清单采集 DESADV，输出保留来源的可见记录或隔离记录。"""
import argparse
import json
from pathlib import Path

from .ingest import ingest, jsonable
from .io import asset, load


def collect(path):
    config, digest = load(path)
    root = Path(path).parent
    raw, source_digest = asset(root, config['source'])
    result = ingest(config, raw, path=config['source']['path'], store={})
    for record in result['records']:
        record['review_required_before_receipt'] = True
    payload = {
        'manifest_sha256': digest,
        'source_sha256': source_digest,
        'status': result['status'],
        'counts': result['counts'],
        'records': result['records'],
        'conflicts': result['conflicts'],
    }
    return payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('file')
    arguments = parser.parse_args()
    payload = collect(arguments.file)
    print(json.dumps(jsonable(payload), ensure_ascii=False, indent=2,
                     allow_nan=False))


if __name__ == '__main__':
    main()
