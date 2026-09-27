import hashlib
import json
from pathlib import Path
from jsonschema import Draft202012Validator
from .validators.report import BoundaryError, Issue, pointer

DATA = Path(__file__).parent / 'data'
VALIDATOR_VERSION = '1.0.0'
PROFILE_FILES = {
    ('PODCAST_HIGH_RETENTION_V1', '1.0.0'): 'profile.json',
    ('PODCAST_HIGH_RETENTION_FAST_SPEECH_V1', '1.0.0'): 'profile-fast-speech.json',
}


def load_contract():
    lock = json.loads((DATA / 'contract-lock.json').read_bytes())
    documents = {}
    for name, expected in lock.items():
        raw = (DATA / name).read_bytes()
        actual = hashlib.sha256(raw).hexdigest()
        if actual != expected:
            raise BoundaryError(Issue('CONTRACT_CHANGED', '/contract/' + name,
                                      'Bundled contract differs from its lock.', expected, actual))
        documents[name] = json.loads(raw)
    Draft202012Validator.check_schema(documents['schema.json'])
    return documents['schema.json'], documents['profile.json'], lock


def validate_schema(plan, schema):
    return [Issue('SCHEMA_INVALID', pointer(e.absolute_path), e.message)
            for e in sorted(Draft202012Validator(schema).iter_errors(plan),
                            key=lambda e: (pointer(e.absolute_path), e.message))]


def load_profile(profile_id, version):
    """Return only a profile declared in the locked contract bundle."""
    _, _, lock = load_contract()
    filename = PROFILE_FILES.get((profile_id, version))
    if filename is None:
        raise BoundaryError(Issue('PROFILE_UNKNOWN', '/profile', 'Plan references an unknown locked profile.'))
    return json.loads((DATA / filename).read_bytes()), lock[filename]
