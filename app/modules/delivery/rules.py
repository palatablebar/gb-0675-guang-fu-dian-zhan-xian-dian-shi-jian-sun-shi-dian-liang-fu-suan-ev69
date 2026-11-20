import hashlib, json
from decimal import Decimal
from app.coerce import number
from app.errors import BusinessError
from app.manifest import digest as source_digest
from .policy import (BASIS_FIELDS, REFERENCE_BASIS_FIELDS, POLICIES, DEFAULT_POLICY,
                     validate_policy, denied_rule)

# 片段交付字段 → 规范字段名。策略按规范名裁决；结构字段标识片段本身，始终交付。
FIELD_MAP = {
    'actual_kw': 'power', 'irradiance': 'irradiance', 'status': 'status', 'sample_at': 'at',
    'baseline_kw': 'baseline_kw', 'loss_kwh': 'loss_kwh', 'actual_kwh': 'actual_kwh',
    'baseline_kwh': 'baseline_kwh', 'reference_ids': 'reference_ids', 'sample_id': 'sample_id',
}
STRUCTURAL = ('device', 'start', 'end', 'seconds', 'zone', 'event_ids', 'code')
# 待查片段不交付的派生字段：依据受限，基线与损失失去依据。
WITHHELD_ON_PENDING = ('baseline_kw', 'loss_kwh', 'actual_kwh', 'baseline_kwh')

def fingerprint(policy_version, run_id, key, field, value):
    # 确定性指纹：审计方持原始批次可重算核对，取数方无法反推原值。
    return hashlib.sha256(f'{policy_version}|{run_id}|{key}|{field}|{value}'.encode()).hexdigest()

def _classify(row, records_by_id, policy):
    key = f"{row['device']}|{row['start']}|{row['end']}"
    denied = {}
    for delivered, canonical in FIELD_MAP.items():
        value = row.get(delivered)
        if value in (None, '', []):
            continue
        rule = denied_rule(policy, canonical, row['device'], row['start'])
        if rule:
            denied[canonical] = rule
    ref_denied = {}
    for ref_id in row.get('reference_ids') or []:
        ref = records_by_id.get(ref_id)
        if not ref:
            continue
        for canonical in REFERENCE_BASIS_FIELDS:
            rule = denied_rule(policy, canonical, ref['device'], ref['at'])
            if rule:
                ref_denied[f"{canonical}@{ref['device']}"] = rule
    basis_hits = {f: r for f, r in denied.items() if f in BASIS_FIELDS}
    klass = '待查' if basis_hits or ref_denied else ('受限' if denied else '可用')
    return key, klass, denied, basis_hits, ref_denied

def _guard_no_leak(fragments, guarded):
    # 泄漏自检：受限原值不得以任何形式进入已交付字段，违反即阻断整批交付。
    by_key = {f['key']: f for f in fragments}
    delivered_of = {c: d for d, c in FIELD_MAP.items()}
    for key, canonical, value in guarded:
        frag = by_key[key]
        delivered = delivered_of[canonical]
        cell = frag['redacted'].get(delivered)
        ok = delivered not in frag['fields'] and cell and set(cell) == {'fingerprint', 'reason', 'field'}
        if not ok or json.dumps(value, ensure_ascii=False, sort_keys=True) in json.dumps(cell, ensure_ascii=False, sort_keys=True):
            raise BusinessError('DELIVERY_LEAK', '交付包含未授权原值，已阻断', 500)

def build_delivery(state, policy, run_id=None):
    validate_policy(policy)
    run_id = run_id or state.get('manifest', {}).get('run_id')
    if not run_id:
        raise BusinessError('RUN_CONTEXT_MISSING', '缺少批次上下文')
    records_by_id = {r['id']: r for r in state['records']}
    fragments, guarded = [], []
    for row in state['energy_rows']:
        key, klass, denied, basis_hits, ref_denied = _classify(row, records_by_id, policy)
        fields = {k: row.get(k) for k in STRUCTURAL}
        redacted = {}
        for delivered, canonical in FIELD_MAP.items():
            value = row.get(delivered)
            rule = denied.get(canonical)
            if rule and value not in (None, '', []):
                redacted[delivered] = {
                    'fingerprint': fingerprint(policy['version'], run_id, key, canonical,
                                               json.dumps(value, ensure_ascii=False, sort_keys=True)),
                    'reason': rule.get('reason', ''), 'field': canonical}
                guarded.append((key, canonical, value))
            elif klass == '待查' and delivered in WITHHELD_ON_PENDING:
                fields[delivered] = None
            else:
                fields[delivered] = value
        reasons = sorted({r.get('reason', '') for r in list(basis_hits.values()) + list(ref_denied.values()) if r.get('reason')})
        fragments.append({'key': key, 'class': klass, 'fields': fields, 'redacted': redacted,
                          'basis_missing': sorted(set(basis_hits) | set(ref_denied)), 'pending_reasons': reasons})
    _guard_no_leak(fragments, guarded)
    quant = lambda v: v.quantize(Decimal('.0001'))
    delivered = [f for f in fragments if f['class'] != '待查' and f['fields'].get('loss_kwh') is not None]
    loss = sum((number(f['fields']['loss_kwh']) for f in delivered), Decimal(0))
    totals = {
        'available': sum(f['class'] == '可用' for f in fragments),
        'restricted': sum(f['class'] == '受限' for f in fragments),
        'pending': sum(f['class'] == '待查' for f in fragments),
        'delivered_loss_kwh': str(quant(loss)),
        'delivered_seconds': sum(f['fields']['seconds'] for f in delivered),
        'pending_seconds': sum(f['fields']['seconds'] for f in fragments if f['class'] == '待查'),
        'unknown_seconds': sum(f['fields']['seconds'] for f in fragments
                               if f['class'] != '待查' and f['fields'].get('loss_kwh') is None),
        'union_seconds': state['balance']['union_seconds'],
    }
    totals['time_accounted'] = (totals['delivered_seconds'] + totals['pending_seconds']
                                + totals['unknown_seconds'] == totals['union_seconds'])
    seal = hashlib.sha256(json.dumps(fragments, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {
        'delivery_id': f"{run_id}:{policy['version']}", 'run_id': run_id,
        'policy_version': policy['version'], 'policy_label': policy.get('label', ''),
        'caliber': dict(policy['caliber']), 'fragments': fragments, 'totals': totals, 'seal': seal,
        'digest': hashlib.sha256(f"{run_id}|{policy['version']}|{seal}".encode()).hexdigest(),
    }

def apply(state):
    run_id = source_digest(state['raw_bytes'], state['context'])[:16]
    delivery = build_delivery(state, POLICIES[DEFAULT_POLICY], run_id=run_id)
    state['delivery'] = delivery
    t = delivery['totals']
    state['views']['delivery'] = {
        'title': '受控证据批次',
        'rows': [{**f['fields'], 'class': f['class']} for f in delivery['fragments']],
        'metrics': {'可用': t['available'], '受限': t['restricted'], '待查': t['pending'],
                    '交付损失_kWh': t['delivered_loss_kwh']},
    }
