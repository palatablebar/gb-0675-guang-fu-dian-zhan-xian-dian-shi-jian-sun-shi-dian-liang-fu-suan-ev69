from app.coerce import instant
from app.errors import BusinessError

# 引擎现行字段口径。口径随规则版本演进；策略口径与此不一致即判不兼容，须升版本重判。
ENGINE_CALIBER = {
    'power': 'kW@600s-left', 'irradiance': 'W/m2@sample', 'status': 'enum:健康|故障',
    'at': 'UTC@iso8601', 'unit': 'enum:kW|MW', 'baseline_kw': 'kW@median-normalized',
    'loss_kwh': 'kWh@0.0001', 'actual_kwh': 'kWh@0.0001', 'baseline_kwh': 'kWh@0.0001',
    'reference_ids': 'id-list@stable', 'sample_id': 'id@stable',
}
# 基线与损失的依据字段：受限即令派生结果失去依据，片段转待查。
BASIS_FIELDS = {'power', 'irradiance', 'status', 'at', 'baseline_kw', 'loss_kwh'}
# 参考逆变器侧参与基线拟合的字段。
REFERENCE_BASIS_FIELDS = ('power', 'irradiance', 'status', 'at')

POLICIES = {
    'full-review-v1': {
        'version': 'full-review-v1', 'label': '完整复核策略',
        'note': '全部字段授权，遥测原样交出。',
        'caliber': dict(ENGINE_CALIBER), 'rules': [],
    },
    'audit-min-v1': {
        'version': 'audit-min-v1', 'label': '审计最小暴露',
        'note': '西区目标机组辐照不授权；东区10:10起对照依据编号不交出，以指纹代核。',
        'caliber': dict(ENGINE_CALIBER),
        'rules': [
            {'field': 'irradiance', 'devices': ['INV-B'], 'decision': 'deny', 'reason': '西区辐照未授权交付'},
            {'field': 'reference_ids', 'devices': ['INV-A'], 'since': '2026-10-15T10:10:00+08:00',
             'decision': 'deny', 'reason': '该时段对照依据涉第三方法人'},
        ],
    },
    'legacy-caliber-v1': {
        'version': 'legacy-caliber-v1', 'label': '旧口径策略',
        'note': '损失口径停留在0.001kWh，与现行0.0001kWh不兼容，仅用于切换失败演练。',
        'caliber': {**ENGINE_CALIBER, 'loss_kwh': 'kWh@0.001'}, 'rules': [],
    },
}
DEFAULT_POLICY = 'audit-min-v1'

def get_policy(version):
    policy = POLICIES.get(version)
    if not policy:
        raise BusinessError('POLICY_NOT_FOUND', '策略版本不存在', 404)
    return policy

def validate_policy(policy):
    # 口径重判：策略声明的口径须与引擎现行口径一致，否则拒绝交付或切换。
    for field, caliber in policy.get('caliber', {}).items():
        if field not in ENGINE_CALIBER:
            raise BusinessError('POLICY_UNKNOWN_FIELD', f'策略口径字段{field}不在现行口径表内')
        if ENGINE_CALIBER[field] != caliber:
            raise BusinessError('POLICY_INCOMPATIBLE',
                                f'字段{field}口径已变更：策略为{caliber}，现行为{ENGINE_CALIBER[field]}，须重判兼容性')
    for rule in policy.get('rules', []):
        if rule.get('field') not in ENGINE_CALIBER:
            raise BusinessError('POLICY_UNKNOWN_FIELD', f"策略字段{rule.get('field')}不在现行口径表内")
        if rule.get('decision', 'deny') != 'deny':
            raise BusinessError('BAD_POLICY_RULE', '仅支持deny裁决')
        for bound in ('since', 'until'):
            if rule.get(bound):
                instant(rule[bound])
    return policy

def denied_rule(policy, field, device, at=None):
    moment = instant(at) if at else None
    for rule in policy.get('rules', []):
        if rule.get('field') != field or rule.get('decision', 'deny') != 'deny':
            continue
        devices = rule.get('devices') or ['all']
        if 'all' not in devices and device not in devices:
            continue
        since = instant(rule['since']) if rule.get('since') else None
        until = instant(rule['until']) if rule.get('until') else None
        if since or until:
            if moment is None:
                continue
            if since and moment < since:
                continue
            if until and moment >= until:
                continue
        return rule
    return None
