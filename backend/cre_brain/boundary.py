from dataclasses import dataclass
from .assets.probe import probe_media
from .assets.registry import validate_assets
from .compiler.snapshot import Snapshot, digest
from .contract import VALIDATOR_VERSION, load_contract, load_profile, validate_schema
from .validators.capabilities import validate_capabilities
from .validators.layout import measure_layout
from .validators.parse import parse_json
from .validators.report import BoundaryError, Issue
from .validators.semantic import validate_semantics
from .validators.source import validate_source


@dataclass(frozen=True)
class AcceptanceResult:
    status: str
    issues: tuple
    snapshot: Snapshot | None = None
    plan_sha256: str | None = None
    execution_sha256: str | None = None
    evidence_sha256: str | None = None

    @property
    def accepted(self):
        return self.status == 'accepted_for_review'

    def report(self):
        return {'status': self.status, 'render_authorized': False,
                'validator_version': VALIDATOR_VERSION, 'plan_sha256': self.plan_sha256,
                'execution_sha256': self.execution_sha256, 'evidence_sha256': self.evidence_sha256,
                'snapshot_sha256': self.snapshot.sha256 if self.snapshot else None,
                'issues': [i.as_dict() for i in self.issues],
                'remaining_reviews': ['speech meaning and rhythm', 'face and gesture safety',
                                      'B-roll relevance', 'final rendered A/V and audio mix'] if self.accepted else []}


def accept_plan(plan_json, trusted_asset_registry, source_analysis, renderer_capabilities,
                *, probe=probe_media, layout=measure_layout):
    """Validate offline; return immutable review snapshot or atomic rejection.

    Registry, analysis, capabilities and injected backends MUST be application-owned.
    Never extract them from model output. Keyword backends are testing/integration seams.
    This function cannot grant final render/publish authorization.
    """
    plan_hash = None
    try:
        plan = parse_json(plan_json)
        plan_hash = digest(plan)
        schema, _, lock = load_contract()
        errors = validate_schema(plan, schema)
        if not errors:
            profile, profile_hash = load_profile(plan['profile']['id'], plan['profile']['version'])
        else:
            profile = None
        if not errors: errors = validate_semantics(plan, profile)
        if errors:
            return AcceptanceResult('rejected', tuple(errors), plan_sha256=plan_hash)
        # Independent checks deliberately report missing evidence together.
        cap_errors = validate_capabilities(plan, renderer_capabilities, lock)
        media_errors, resolved = validate_assets(plan, trusted_asset_registry, probe=probe)
        source_errors = validate_source(plan, source_analysis)
        errors = cap_errors + media_errors + source_errors
        layouts = []
        if not cap_errors:
            layout_errors, layouts = layout(plan, profile, renderer_capabilities)
            errors += layout_errors
        if errors:
            return AcceptanceResult('rejected', tuple(errors), plan_sha256=plan_hash)
        # Descriptive metadata, plan IDs and advisory reasoning do not affect pixels/audio.
        binding = {k: v for k, v in renderer_capabilities.items() if k != 'font'}
        binding['font'] = {k: v for k, v in renderer_capabilities['font'].items() if k != 'path'}
        execution = {'canonicalization': 'cre-json-v1', 'schema_version': plan['schema_version'],
                     'contract_hashes': lock,
                     'profile': {'id': plan['profile']['id'], 'version': plan['profile']['version']},
                     'source_asset_id': plan['clip']['source_asset_id'], 'language': plan['clip']['language'],
                     'render': plan['render'], 'renderer_binding': binding, 'text_layouts': layouts}
        execution_hash = digest(execution)
        evidence = {'source_analysis_sha256': digest(source_analysis),
                    'registry_sha256': digest(trusted_asset_registry),
                    'capabilities_sha256': digest(renderer_capabilities), 'validator_version': VALIDATOR_VERSION}
        evidence_hash = digest(evidence)
        timeline = []
        cursor = 0; removed = []
        for segment in plan['render']['segments']:
            if cursor < segment['source_in_frame']: removed.append([cursor, segment['source_in_frame']])
            cursor = segment['source_out_frame']
            timeline.append({**segment, 'output_end_frame': segment['output_start_frame'] + segment['source_out_frame'] - segment['source_in_frame']})
        source_duration = next(a['duration_frames'] for a in plan['render']['assets'] if a['id'] == plan['clip']['source_asset_id'])
        if cursor < source_duration: removed.append([cursor, source_duration])
        snapshot = Snapshot.from_data({'snapshot_version': '1.0.0', 'render_authorized': False,
                                       'execution_sha256': execution_hash, 'execution': execution,
                                       'evidence_sha256': evidence_hash, 'evidence': evidence,
                                       'resolved_assets': resolved, 'timeline': timeline,
                                       'removed_source_ranges': removed})
        return AcceptanceResult('accepted_for_review', (), snapshot, plan_hash, execution_hash, evidence_hash)
    except BoundaryError as exc:
        return AcceptanceResult('rejected', (exc.issue,), plan_sha256=plan_hash)
    except (ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
        return AcceptanceResult('rejected', (Issue('TRUSTED_INPUT_INVALID', '', str(exc)),), plan_sha256=plan_hash)
