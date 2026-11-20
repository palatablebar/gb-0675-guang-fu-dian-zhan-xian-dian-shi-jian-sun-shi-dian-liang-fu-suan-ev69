import sqlite3, json
from pathlib import Path
from app.errors import BusinessError

def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(path,timeout=15)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '待复核')")
    con.execute("CREATE TABLE IF NOT EXISTS deliveries (run_id TEXT NOT NULL, policy_version TEXT NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '待复核', PRIMARY KEY (run_id, policy_version))")
    con.execute("CREATE TABLE IF NOT EXISTS policy_state (id INTEGER PRIMARY KEY CHECK(id=1), active TEXT NOT NULL)")
    return con

def save(path, result):
    run_id=result['manifest']['run_id']
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO runs(id,digest,result) VALUES(?,?,?)', (run_id,result['manifest']['sha256'],json.dumps(result,ensure_ascii=False)))
    return get(path,run_id)

def get(path, run_id):
    with connect(path) as con: row=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not row: raise BusinessError('NOT_FOUND','批次不存在',404)
    result=json.loads(row['result']);result.update(revision=row['revision'],status=row['status'])
    return result

def history(path):
    with connect(path) as con:
        return [dict(r) for r in con.execute('SELECT id,revision,status FROM runs ORDER BY rowid DESC LIMIT 100')]

def all_results(path):
    with connect(path) as con:
        return [json.loads(r['result']) for r in con.execute('SELECT result FROM runs ORDER BY rowid')]

def review(path,run_id,revision):
    with connect(path) as con:
        cur=con.execute('UPDATE runs SET revision=revision+1,status=? WHERE id=? AND revision=?',('已复核',run_id,revision))
        if cur.rowcount!=1:
            if not con.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():
                raise BusinessError('NOT_FOUND','批次不存在',404)
            raise BusinessError('STALE_VERSION','复核版本已更新，请刷新',409)
    return get(path,run_id)

def get_active_policy(path):
    with connect(path) as con:
        row=con.execute('SELECT active FROM policy_state WHERE id=1').fetchone()
    return row['active'] if row else None

def _insert_delivery(con, run_id, policy_version, payload):
    # 幂等：摘要一致复用既有行；同键摘要漂移说明策略内容被改写，拒绝覆盖（已复核结果尤其不可动）。
    cur=con.execute('INSERT OR IGNORE INTO deliveries(run_id,policy_version,digest,payload) VALUES(?,?,?,?)',
                    (run_id,policy_version,payload['digest'],json.dumps(payload,ensure_ascii=False)))
    if cur.rowcount==0:
        row=con.execute('SELECT digest FROM deliveries WHERE run_id=? AND policy_version=?',(run_id,policy_version)).fetchone()
        if row['digest']!=payload['digest']:
            raise BusinessError('DELIVERY_CONFLICT','同版本策略内容漂移，拒绝覆盖既有交付',409)

def save_delivery(path, run_id, policy_version, payload):
    with connect(path) as con:
        _insert_delivery(con, run_id, policy_version, payload)
    return get_delivery(path, run_id, policy_version)

def get_delivery(path, run_id, policy_version):
    with connect(path) as con:
        row=con.execute('SELECT * FROM deliveries WHERE run_id=? AND policy_version=?',(run_id,policy_version)).fetchone()
    if not row: return None
    payload=json.loads(row['payload']);payload.update(revision=row['revision'],status=row['status'])
    return payload

def deliveries_for(path, run_id):
    with connect(path) as con:
        return [dict(r) for r in con.execute('SELECT run_id,policy_version,digest,revision,status FROM deliveries WHERE run_id=? ORDER BY rowid',(run_id,))]

def review_delivery(path, run_id, policy_version, revision):
    with connect(path) as con:
        cur=con.execute('UPDATE deliveries SET revision=revision+1,status=? WHERE run_id=? AND policy_version=? AND revision=?',('已复核',run_id,policy_version,revision))
        if cur.rowcount!=1:
            if not con.execute('SELECT 1 FROM deliveries WHERE run_id=? AND policy_version=?',(run_id,policy_version)).fetchone():
                raise BusinessError('NOT_FOUND','交付批不存在',404)
            raise BusinessError('STALE_VERSION','复核版本已更新，请刷新',409)
    return get_delivery(path,run_id,policy_version)

def activate_policy(path, version, deliveries):
    # 单事务切换：任一交付写入失败即整体回滚，active保持旧版，既有交付不删不改。
    with connect(path) as con:
        con.execute('INSERT OR REPLACE INTO policy_state(id,active) VALUES(1,?)',(version,))
        for run_id,payload in deliveries:
            _insert_delivery(con,run_id,version,payload)
