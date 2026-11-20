import io, csv, json, zipfile, hashlib
from app.errors import BusinessError

def serialize(result):
    if not result.get('manifest') or not result.get('views'):
        raise BusinessError('INCOMPLETE_RUN','批次尚未完成')
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json',json.dumps({**result['manifest'],'revision':result.get('revision',1),'status':result.get('status','待复核')},ensure_ascii=False,indent=2))
        archive.writestr('evidence.json',json.dumps({'counts':result['counts'],'views':result['views'],'quarantine':result['quarantine']},ensure_ascii=False,indent=2))
    return buffer.getvalue()

def serialize_delivery(delivery):
    # 受控证据批次只含授权原值与受限字段指纹；构建期的泄漏自检保证未授权原值不在其中。
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json',json.dumps({k:delivery.get(k) for k in ('delivery_id','run_id','policy_version','digest','seal','status','revision')},ensure_ascii=False,indent=2))
        archive.writestr('delivery.json',json.dumps(delivery,ensure_ascii=False,indent=2))
    return buffer.getvalue()
