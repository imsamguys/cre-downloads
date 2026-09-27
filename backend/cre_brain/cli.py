import argparse
import json
from pathlib import Path
from .assets.probe import probe_media
from .assets.registry import normalization_issues
from .boundary import accept_plan
from .compiler.snapshot import file_hash
from .validators.parse import parse_json
from .validators.report import BoundaryError


def emit(value):
    print(json.dumps(value, indent=2, ensure_ascii=False))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Offline CRE acceptance and read-only media inspection.')
    sub = parser.add_subparsers(dest='command', required=True)
    inspect = sub.add_parser('inspect-media', help='Inspect media against the podcast normalization contract.')
    inspect.add_argument('path', type=Path)
    inspect.add_argument('--kind', choices=['video', 'audio', 'image'], default='video')
    accept = sub.add_parser('accept', help='Validate an entire plan; never renders.')
    accept.add_argument('plan', type=Path)
    accept.add_argument('--registry', required=True, type=Path)
    accept.add_argument('--analysis', required=True, type=Path)
    accept.add_argument('--capabilities', required=True, type=Path)
    accept.add_argument('--snapshot', type=Path, help='New output path; written only for accepted_for_review, never overwritten.')
    args = parser.parse_args(argv)
    try:
        if args.command == 'inspect-media':
            path = args.path.resolve(strict=True)
            before = file_hash(path)
            metadata = probe_media(path, args.kind)
            if file_hash(path) != before:
                raise ValueError('Media changed during inspection.')
            issues = normalization_issues(metadata, '/media')
            emit({'status': 'requires_normalization' if issues else 'media_format_checked',
                  'render_authorized': False, 'path': str(path), 'sha256': before,
                  'metadata': metadata, 'issues': [i.as_dict() for i in issues],
                  'remaining': ['trusted word alignment', 'hash-bound loudness evidence',
                                'asset-use approval', 'renderer and font capability binding']})
            return 2 if issues else 0
        result = accept_plan(args.plan.read_bytes(),
                             parse_json(args.registry.read_bytes(), max_bytes=4*1024*1024),
                             parse_json(args.analysis.read_bytes(), max_bytes=4*1024*1024),
                             parse_json(args.capabilities.read_bytes(), max_bytes=4*1024*1024))
        if result.snapshot and args.snapshot:
            # Exclusive creation prevents replacing an earlier snapshot or input by mistake.
            with args.snapshot.open('xb') as stream:
                stream.write(result.snapshot.json_bytes)
        emit(result.report())
        return 0 if result.accepted else 2
    except (BoundaryError, OSError, ValueError) as exc:
        emit({'status': 'rejected', 'render_authorized': False,
              'issues': [exc.issue.as_dict()] if isinstance(exc, BoundaryError) else
                        [{'code': 'INPUT_IO_ERROR', 'path': '', 'message': str(exc)}]})
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
