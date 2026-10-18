"""Mock / replay model provider (no API key) and the advisor that wraps it.

Where the model is used: when a new goal is being committed, the advisor asks the model which of the
*deterministically eligible* goals to pursue. That is the one judgement call in the loop that a model could
legitimately improve on. Its answer is advisory only:
  * it can only pick from the eligible set the rules already computed (it can never make a viewer eligible),
  * its confidence and free-text reason are recorded but never used as evidence,
  * any timeout, HTTP 503, malformed JSON, schema violation or unsupported claim falls back to the rule choice.

The mock replays replay/model_faults.jsonl exactly on the named triggers and reproduces the model_provider outage
window from control_events.jsonl. Latency is simulated deterministically and added to the decision time.
"""
import hashlib
import json
import re
from datetime import timedelta

CLIENT_TIMEOUT_MS = 1500
CIRCUIT_OPEN_MS = 30000
# Claims about data sources the agent does not have. A model citing them is hallucinating evidence.
UNSUPPORTED_SOURCES = re.compile(r'\b(billing|payment (system|gateway)|crm|bank|credit card|confirmed in)\b', re.I)


def _h01(*parts):
    return int(hashlib.sha256(':'.join(map(str, parts)).encode()).hexdigest()[:12], 16) / 16 ** 12


class ModelTimeout(Exception):
    pass


class ModelUnavailable(Exception):
    pass


class MockModelProvider:
    def __init__(self, faults, registry=None):
        self.faults = faults or {}
        self.registry = registry
        self.calls = 0

    def complete(self, task, payload, trigger_event_id, at):
        """Returns (raw_text, latency_ms). Raises ModelTimeout / ModelUnavailable."""
        self.calls += 1
        if self.registry and self.registry.state('model_provider', at) == 'dependency_unavailable':
            raise ModelUnavailable('HTTP 503 (model_provider outage window)')
        fault = self.faults.get(trigger_event_id)
        if fault:
            if fault.get('fault') == 'timeout':
                raise ModelTimeout('provider did not answer within %d ms (fault asks for %ss)' % (
                    CLIENT_TIMEOUT_MS, fault.get('seconds')))
            out = fault.get('model_output')
            return (out if isinstance(out, str) else json.dumps(out)), int(150 + 250 * _h01('lat', trigger_event_id))
        latency = int(120 + 280 * _h01('lat', trigger_event_id))
        if task == 'choose_goal':
            gid = payload['eligible'][0]['goal_id']
            return json.dumps({'goal_id': gid, 'eligible': True, 'confidence': 0.7,
                               'reason': 'Highest-priority eligible goal given the evidence provided.'}), latency
        raise ValueError('unknown task %s' % task)


class ModelAdvisor:
    def __init__(self, provider, max_calls=8):
        self.provider = provider
        self.max_calls = max_calls
        self.circuit_open_until = None

    def choose_goal(self, eligible, trigger_event_id, at, budget):
        """Returns dict(goal_id, fallback_used, model_calls, latency_ms, model_status, notes)."""
        rule_choice = eligible[0]['goal_id']
        res = {'goal_id': rule_choice, 'fallback_used': False, 'model_calls': 0, 'latency_ms': 0,
               'model_status': 'ok', 'notes': []}
        if budget.model_calls >= self.max_calls:
            res.update(fallback_used=True, model_status='budget_exhausted')
            return res
        if self.circuit_open_until and at < self.circuit_open_until:
            res.update(fallback_used=True, model_status='circuit_open')
            res['notes'].append('model circuit open after recent 503s; deterministic rules used')
            return res
        budget.model_calls += 1
        res['model_calls'] = 1
        payload = {'eligible': [{'goal_id': c['goal_id'], 'evidence': c['evidence']} for c in eligible]}
        try:
            raw, latency = self.provider.complete('choose_goal', payload, trigger_event_id, at)
            res['latency_ms'] = latency
        except ModelTimeout as ex:
            res.update(fallback_used=True, model_status='timeout', latency_ms=CLIENT_TIMEOUT_MS)
            res['notes'].append(str(ex))
            return res
        except ModelUnavailable as ex:
            self.circuit_open_until = at + timedelta(milliseconds=CIRCUIT_OPEN_MS)
            res.update(fallback_used=True, model_status='unavailable', latency_ms=60)
            res['notes'].append(str(ex))
            return res
        verdict, parsed, why = self.validate(raw, eligible)
        if verdict != 'ok':
            res.update(fallback_used=True, model_status=verdict)
            res['notes'].append(why)
            return res
        res['goal_id'] = parsed['goal_id']
        res['model_confidence'] = parsed['confidence']
        return res

    @staticmethod
    def validate(raw, eligible):
        try:
            out = json.loads(raw)
        except (TypeError, ValueError) as ex:
            return 'malformed_output', None, 'model output is not valid JSON (%s); rules used' % ex.__class__.__name__
        if not isinstance(out, dict) or not {'goal_id', 'eligible', 'confidence'} <= set(out):
            return 'schema_violation', None, 'model output missing required keys; rules used'
        allowed = {c['goal_id'] for c in eligible}
        if out['goal_id'] not in allowed or out['eligible'] is not True:
            return ('unsupported_claim', None,
                    'model claimed %s is eligible but the rules (evidence) say eligible goals are %s; claim rejected'
                    % (out['goal_id'], sorted(allowed)))
        if UNSUPPORTED_SOURCES.search(str(out.get('reason', ''))):
            return 'unsupported_claim', None, 'model cited a data source the agent does not have; rejected'
        c = out['confidence']
        if not isinstance(c, (int, float)) or not 0 <= c <= 1:
            return 'schema_violation', None, 'confidence out of range; rules used'
        return 'ok', out, None
