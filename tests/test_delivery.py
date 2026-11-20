from fastapi.testclient import TestClient
from app import main, store
from app.engine import evaluate
from app.config import ROOT
from app.modules.delivery.policy import POLICIES, DEFAULT_POLICY, ENGINE_CALIBER
from app.modules.delivery.rules import build_delivery, fingerprint
import json, zipfile, io, pytest

def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path/'test.sqlite'))
    return TestClient(main.app)

def db(tmp_path):
    return str(tmp_path/'test.sqlite')

def make_run(c, dataset='clean'):
    return c.post('/api/runs', json={'dataset': dataset}).json()['manifest']['run_id']

def test_default_classification_clean():
    state = evaluate({'dataset': 'clean'})
    d = build_delivery(state, POLICIES[DEFAULT_POLICY], run_id=state['manifest']['run_id'])
    classes = [f['class'] for f in d['fragments']]
    assert classes.count('可用') == 1 and classes.count('受限') == 2 and classes.count('待查') == 2
    assert d['totals']['delivered_loss_kwh'] == '20.0000'
    assert d['totals']['pending_seconds'] == 1200
    assert d['totals']['time_accounted'] is True

def test_full_review_policy_delivers_everything():
    state = evaluate({'dataset': 'clean'})
    d = build_delivery(state, POLICIES['full-review-v1'], run_id=state['manifest']['run_id'])
    assert all(f['class'] == '可用' for f in d['fragments'])
    assert d['totals']['delivered_loss_kwh'] == state['balance']['loss_kwh'] == '26.6667'
    assert d['totals']['pending_seconds'] == 0

def test_pending_withholds_basis_and_loss():
    state = evaluate({'dataset': 'clean'})
    run_id = state['manifest']['run_id']
    d = build_delivery(state, POLICIES[DEFAULT_POLICY], run_id=run_id)
    pending = [f for f in d['fragments'] if f['class'] == '待查']
    assert {f['fields']['device'] for f in pending} == {'INV-B'}
    for f in pending:
        assert 'irradiance' not in f['fields']
        assert f['fields']['loss_kwh'] is None and f['fields']['baseline_kw'] is None
        assert f['basis_missing'] == ['irradiance']
        cell = f['redacted']['irradiance']
        expected = fingerprint(DEFAULT_POLICY, run_id, f['key'], 'irradiance', json.dumps('800'))
        assert cell['fingerprint'] == expected and cell['reason']

def test_restricted_keeps_loss_hides_anchor():
    state = evaluate({'dataset': 'clean'})
    d = build_delivery(state, POLICIES[DEFAULT_POLICY], run_id=state['manifest']['run_id'])
    restricted = [f for f in d['fragments'] if f['class'] == '受限']
    assert len(restricted) == 2
    for f in restricted:
        assert f['fields']['device'] == 'INV-A'
        assert f['fields']['loss_kwh'] is not None
        assert 'reference_ids' not in f['fields']
        assert f['redacted']['reference_ids']['fingerprint']

def test_no_unauthorized_raw_leak(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    original = (ROOT/'sample_data/clean.csv').read_text()
    custom = original.replace('INV-B-00,TELEMETRY,INV-B,2026-10-15T10:00:00+08:00,30,kW,800',
                              'INV-B-00,TELEMETRY,INV-B,2026-10-15T10:00:00+08:00,30,kW,812.5')
    rid = c.post('/api/runs', json={'csv_text': custom}).json()['manifest']['run_id']
    assert c.get('/delivery?run_id='+rid).status_code == 200
    delivery = store.get_delivery(db(tmp_path), rid, DEFAULT_POLICY)
    blob = json.dumps(delivery, ensure_ascii=False)
    assert '812.5' not in blob
    z = zipfile.ZipFile(io.BytesIO(c.get('/api/delivery/'+rid+'/export').content))
    assert '812.5' not in z.read('delivery.json').decode()
    assert json.loads(z.read('manifest.json'))['digest'] == delivery['digest']

def test_caliber_mismatch_refused_and_state_unchanged(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = make_run(c)
    assert c.get('/delivery?run_id='+rid).status_code == 200
    before = store.deliveries_for(db(tmp_path), rid)
    r = c.post('/api/delivery/policy', json={'version': 'legacy-caliber-v1'})
    assert r.status_code == 422 and r.json()['code'] == 'POLICY_INCOMPATIBLE'
    assert (store.get_active_policy(db(tmp_path)) or DEFAULT_POLICY) == DEFAULT_POLICY
    assert store.deliveries_for(db(tmp_path), rid) == before

def test_unknown_policy_field_rejected(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    make_run(c)
    broken = {'version': 'broken-v1', 'label': '坏策略', 'note': '', 'caliber': dict(ENGINE_CALIBER),
              'rules': [{'field': 'nope', 'devices': ['all'], 'decision': 'deny'}]}
    monkeypatch.setitem(POLICIES, 'broken-v1', broken)
    r = c.post('/api/delivery/policy', json={'version': 'broken-v1'})
    assert r.status_code == 422 and r.json()['code'] == 'POLICY_UNKNOWN_FIELD'
    assert (store.get_active_policy(db(tmp_path)) or DEFAULT_POLICY) == DEFAULT_POLICY

def test_switch_transaction_rollback(tmp_path, monkeypatch):
    from app.errors import BusinessError
    c = client(tmp_path, monkeypatch)
    r1 = make_run(c, 'dirty')
    r2 = make_run(c, 'clean')
    real = store._insert_delivery
    calls = []
    def boom(con, run_id, version, payload):
        calls.append(run_id)
        if len(calls) == 2:
            raise BusinessError('SWITCH_FAILED', '注入的写入失败', 500)
        return real(con, run_id, version, payload)
    monkeypatch.setattr(store, '_insert_delivery', boom)
    r = c.post('/api/delivery/policy', json={'version': 'full-review-v1'})
    assert r.status_code == 500
    assert (store.get_active_policy(db(tmp_path)) or DEFAULT_POLICY) == DEFAULT_POLICY
    assert store.deliveries_for(db(tmp_path), r1) == [] and store.deliveries_for(db(tmp_path), r2) == []

def test_switch_success_preserves_old_deliveries(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = make_run(c)
    assert c.get('/delivery?run_id='+rid).status_code == 200
    old = store.get_delivery(db(tmp_path), rid, DEFAULT_POLICY)
    r = c.post('/api/delivery/policy', json={'version': 'full-review-v1'})
    assert r.status_code == 200 and r.json()['active'] == 'full-review-v1'
    versions = {d['policy_version'] for d in store.deliveries_for(db(tmp_path), rid)}
    assert versions == {DEFAULT_POLICY, 'full-review-v1'}
    kept = store.get_delivery(db(tmp_path), rid, DEFAULT_POLICY)
    assert kept['digest'] == old['digest']
    html = c.get('/delivery?run_id='+rid).text
    assert 'full-review-v1' in html and '可用片段' in html

def test_rerun_no_duplicate_no_overwrite_reviewed(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = make_run(c)
    assert c.get('/delivery?run_id='+rid).status_code == 200
    r = c.post('/api/delivery/'+rid+'/review', json={'revision': 1})
    assert r.status_code == 200 and r.json()['status'] == '已复核'
    digest = r.json()['digest']
    assert c.post('/api/runs/'+rid+'/rerun').status_code == 200
    assert c.get('/delivery?run_id='+rid).status_code == 200
    rows = store.deliveries_for(db(tmp_path), rid)
    assert len(rows) == 1 and rows[0]['status'] == '已复核' and rows[0]['revision'] == 2
    assert rows[0]['digest'] == digest

def test_delivery_review_stale_and_bad_revision(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = make_run(c)
    assert c.get('/delivery?run_id='+rid).status_code == 200
    url = '/api/delivery/'+rid+'/review'
    assert c.post(url, json={'revision': 0}).status_code == 422
    assert c.post(url, json={'revision': 1}).status_code == 200
    assert c.post(url, json={'revision': 1}).status_code == 409

def test_digest_conflict_guard(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = make_run(c)
    assert c.get('/delivery?run_id='+rid).status_code == 200
    state = store.get(db(tmp_path), rid)
    mutated = dict(POLICIES[DEFAULT_POLICY])
    mutated['rules'] = mutated['rules'] + [
        {'field': 'status', 'devices': ['INV-A'], 'decision': 'deny', 'reason': '漂移注入'}]
    drifted = build_delivery(state, mutated, run_id=rid)
    with pytest.raises(Exception) as e:
        store.save_delivery(db(tmp_path), rid, DEFAULT_POLICY, drifted)
    assert getattr(e.value, 'code', None) == 'DELIVERY_CONFLICT'
    kept = store.get_delivery(db(tmp_path), rid, DEFAULT_POLICY)
    assert kept['digest'] != drifted['digest']

def test_page_lists_three_groups(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = make_run(c)
    html = c.get('/delivery?run_id='+rid).text
    assert '可用片段' in html and '受限片段' in html and '待查片段' in html
    assert '指纹 ' in html and '口径表' in html

def test_module_entry_and_engine_default():
    state = evaluate()
    assert 'delivery' in state['views'] and 'delivery' in state
    c_state = evaluate({'dataset': 'clean'})
    assert c_state['views']['delivery']['metrics']['待查'] == 2

def test_export_zip_contents(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = make_run(c)
    assert c.get('/delivery?run_id='+rid).status_code == 200
    r = c.get('/api/delivery/'+rid+'/export')
    assert r.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert set(z.namelist()) == {'manifest.json', 'delivery.json'}
    delivery = json.loads(z.read('delivery.json'))
    assert delivery['run_id'] == rid and delivery['policy_version'] == DEFAULT_POLICY
    assert delivery['totals']['delivered_loss_kwh'] == '20.0000'
