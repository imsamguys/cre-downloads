import json
from .report import BoundaryError, Issue


def parse_json(raw, *, max_bytes=262144, max_depth=32):
    """Single strict UTF-8 document; bound nesting before recursive JSON decoding."""
    def reject(code, message):
        raise BoundaryError(Issue(code, '', message))
    if not isinstance(raw, (str, bytes)):
        reject('JSON_TYPE', 'Pass JSON text or UTF-8 bytes, not a mutable object.')
    try:
        data = raw.encode('utf-8') if isinstance(raw, str) else raw
        if len(data) > max_bytes:
            reject('JSON_SIZE', f'JSON exceeds {max_bytes} bytes.')
        text = data.decode('utf-8')
        depth = 0
        quoted = escaped = False
        for char in text:
            if quoted:
                if escaped:
                    escaped = False
                elif char == '\\':
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in '[{':
                depth += 1
                if depth > max_depth:
                    reject('JSON_DEPTH', f'JSON nesting exceeds {max_depth}.')
            elif char in ']}':
                depth -= 1

        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    reject('JSON_DUPLICATE_KEY', f'Duplicate key: {key}')
                result[key] = value
            return result

        def number(value):
            reject('JSON_NUMBER', 'This contract permits integer numbers only.')
        result = json.loads(text, object_pairs_hook=pairs, parse_float=number, parse_constant=number)
        # Reject lone surrogates accepted by Python's permissive JSON decoder.
        json.dumps(result, ensure_ascii=False).encode('utf-8')
        if not isinstance(result, dict):
            reject('JSON_ROOT', 'Root must be one object.')
        return result
    except BoundaryError:
        raise
    except (ValueError, UnicodeError, RecursionError) as exc:
        reject('JSON_PARSE', str(exc))
