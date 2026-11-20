from fastapi import APIRouter, Request, Body
from fastapi.responses import Response
from app.engine import evaluate
from app.errors import BusinessError
from app.web import templates
from app.config import META, MODULES
from app.archive import serialize_delivery
from app import store
from .policy import POLICIES, DEFAULT_POLICY, get_policy, validate_policy
from .rules import build_delivery

router = APIRouter()

def _db():
    from app.main import DB_PATH
    return DB_PATH

def _active(path):
    return store.get_active_policy(path) or DEFAULT_POLICY

def _policy_card(policy):
    try:
        validate_policy(policy)
        return {**policy, 'compatible': True, 'issue': ''}
    except BusinessError as e:
        return {**policy, 'compatible': False, 'issue': e.message}

def _ensure(path, state):
    version = _active(path)
    policy = validate_policy(get_policy(version))
    existing = store.get_delivery(path, state['manifest']['run_id'], version)
    if existing:
        return existing
    payload = build_delivery(state, policy, run_id=state['manifest']['run_id'])
    return store.save_delivery(path, state['manifest']['run_id'], version, payload)

@router.get('/delivery')
def page(request: Request):
    path = _db()
    run_id = request.query_params.get('run_id')
    if run_id:
        state = store.get(path, run_id)
    else:
        state = store.save(path, evaluate({'dataset': request.query_params.get('dataset', 'dirty')}))
    delivery = error = None
    try:
        delivery = _ensure(path, state)
    except BusinessError as e:
        error = e.message
    return templates.TemplateResponse(request=request, name='delivery.html', context={
        'meta': META, 'key': 'delivery', 'state': state,
        'view': state['views'].get('delivery', {}), 'delivery': delivery, 'error': error,
        'active': _active(path), 'policies': [_policy_card(p) for p in POLICIES.values()],
        'deliveries': store.deliveries_for(path, state['manifest']['run_id'])})

@router.post('/api/delivery')
def calculate(payload: dict = Body(default={})):
    return evaluate(payload, until=len(MODULES))

@router.post('/api/delivery/policy')
def switch(payload: dict = Body(...)):
    path = _db()
    version = payload.get('version')
    if not isinstance(version, str) or not version.strip():
        raise BusinessError('BAD_POLICY', '策略版本须为非空字符串')
    policy = validate_policy(get_policy(version.strip()))
    # 先纯函数构建全部交付，再单事务切换；任一环节失败即回滚，旧版与既有交付零丢失。
    deliveries = [(r['manifest']['run_id'], build_delivery(r, policy, run_id=r['manifest']['run_id']))
                  for r in store.all_results(path)]
    store.activate_policy(path, version.strip(), deliveries)
    return {'active': version.strip(), 'deliveries': len(deliveries)}

@router.post('/api/delivery/{run_id}/review')
def review(run_id: str, payload: dict = Body(...)):
    path = _db()
    revision = payload.get('revision')
    if not isinstance(revision, int) or revision < 1:
        raise BusinessError('BAD_REVISION', '版本号须为正整数')
    return store.review_delivery(path, run_id, _active(path), revision)

@router.get('/api/delivery/{run_id}/export')
def export(run_id: str):
    path = _db()
    delivery = store.get_delivery(path, run_id, _active(path)) or _ensure(path, store.get(path, run_id))
    return Response(serialize_delivery(delivery), media_type='application/zip',
                    headers={'Content-Disposition': f'attachment; filename="delivery-{run_id}.zip"'})
