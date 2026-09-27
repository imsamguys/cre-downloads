from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Issue:
    code: str
    path: str
    message: str
    expected: object = None
    actual: object = None

    def as_dict(self):
        return asdict(self)


class BoundaryError(ValueError):
    def __init__(self, issue):
        self.issue = issue
        super().__init__(issue.message)


def pointer(parts):
    return ''.join('/' + str(p).replace('~', '~0').replace('/', '~1') for p in parts)
