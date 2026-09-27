import hashlib
import json
from dataclasses import dataclass


def canonical_bytes(value):
    """CRE canonical JSON v1: sorted keys, UTF-8, no whitespace, integers only.

    No Unicode normalization or array reordering. Deliberately not RFC 8785.
    """
    def walk(item):
        if isinstance(item, dict):
            if not all(isinstance(k, str) for k in item):
                raise ValueError('Canonical keys must be strings.')
            for child in item.values(): walk(child)
        elif isinstance(item, list):
            for child in item: walk(child)
        elif item is not None and type(item) not in (str, int, bool):
            raise ValueError('Canonical JSON supports only integers, strings, booleans and null.')
    walk(value)
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class Snapshot:
    """Bytes are immutable; as_dict returns a fresh copy on every call."""
    json_bytes: bytes
    sha256: str

    @classmethod
    def from_data(cls, value):
        raw = canonical_bytes(value)
        return cls(raw, hashlib.sha256(raw).hexdigest())

    def as_dict(self):
        return json.loads(self.json_bytes)
