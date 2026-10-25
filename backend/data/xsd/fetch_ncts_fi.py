"""Fetch the Finnish Customs national transit schema bundle, with provenance."""
import argparse, hashlib, io, json, zipfile
from datetime import datetime, timezone
from pathlib import Path
import requests
BASE='https://finnishcustoms-suomentulli.github.io/MIGs/Transit/common/'

def fetch(out, version):
    url=BASE+version+'/schemas.zip'
    r=requests.get(url,timeout=45);r.raise_for_status()
    out=Path(out);out.mkdir(parents=True,exist_ok=False)
    hashes=[]
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        for name in z.namelist():
            if not name.endswith('.xsd'):continue
            p=out/Path(name).name;b=z.read(name);p.write_bytes(b)
            hashes.append(hashlib.sha256(b).hexdigest()+'  '+p.name)
    (out/'SHA256SUMS').write_text('\n'.join(hashes)+'\n')
    (out/'SOURCE.json').write_text(json.dumps({'url':url,'sha256':hashlib.sha256(r.content).hexdigest(),
        'retrieved_at':datetime.now(timezone.utc).isoformat(),'version':version},indent=2)+'\n')
    print(len(hashes),'schemas',url)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',required=True);p.add_argument('--version',required=True)
    a=p.parse_args();fetch(a.out,a.version)
