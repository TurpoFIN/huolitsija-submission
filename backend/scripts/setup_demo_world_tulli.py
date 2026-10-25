"""Create private synthetic TEST signing configuration, never authority credentials.

Usage: python scripts/setup_demo_world_tulli.py --output-dir /home/user/work/<new-dir>
       --authority-url http://127.0.0.1:<backend-port>/api/demo/world/authority/tulli
Writes only inside the new directory. Prints no configuration or key values.
"""
import sys
sys.dont_write_bytecode = True
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
from urllib.parse import urlparse

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def private_file(path, data):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as f:
        f.write(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--authority-url', required=True)
    args = parser.parse_args()
    directory = Path(args.output_dir).resolve()
    root = Path('/home/user/work').resolve()
    if root not in directory.parents or directory.exists() or not directory.parent.is_dir():
        parser.error('output-dir must be a new directory with an existing parent under /home/user/work')
    endpoint = urlparse(args.authority_url)
    if endpoint.scheme not in ('http','https') or endpoint.hostname not in ('127.0.0.1','localhost','::1') \
            or endpoint.path != '/api/demo/world/authority/tulli' or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        parser.error('authority-url must be the local world-authority route on loopback')
    # A read-only collision check; this identifier must never belong to a real party.
    for _ in range(5):
        digits = '399' + str(secrets.randbelow(10000)).zfill(4)
        remainder = sum(int(x)*w for x,w in zip(digits,(7,9,10,5,8,4,2))) % 11
        if remainder == 1:
            continue
        business_id = digits + '-' + str(0 if remainder == 0 else 11-remainder)
        url = 'https://avoindata.prh.fi/opendata-ytj-api/v3/companies?businessId=' + business_id
        response = httpx.get(url, timeout=25, follow_redirects=False)
        if response.status_code not in (200,404):
            raise RuntimeError('YTJ collision check unavailable; no configuration written')
        if response.status_code == 404 or not (response.json().get('companies') or []):
            break
    else:
        raise RuntimeError('No unused fictional identifier allocated; no configuration written')
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Huolitsija private world TEST')])
    today = datetime.now(timezone.utc)
    cert = x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(
        x509.random_serial_number()).not_valid_before(today-timedelta(days=1)).not_valid_after(today+timedelta(days=365)).sign(key,hashes.SHA256())
    directory.mkdir(mode=0o700)
    private_file(directory/'key.pem',key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    private_file(directory/'cert.pem',cert.public_bytes(serialization.Encoding.PEM))
    identity = 'FI' + business_id
    settings = {'DEMO_WORLD_SANDBOX':'true','TULLI_MODE':'test','TULLI_IMPORT_MODE':'test','TULLI_EXPORT_MODE':'test',
        'TULLI_DECLARANT_ID':identity,'TULLI_BUILDER_ID':identity,'TULLI_INTERMEDIARY_ID':identity,
        'TULLI_REFERENCE_PREFIX':''.join(secrets.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ') for _ in range(5)),
        'TULLI_CLIENT_CERT':str(directory/'cert.pem'),'TULLI_CLIENT_KEY':str(directory/'key.pem'),
        'TULLI_CUSTOMER_APPROVED':'true','TULLI_ENDPOINT':args.authority_url}
    private_file(directory/'runtime.env',('\n'.join(k+'='+shlex.quote(v) for k,v in settings.items())+'\n').encode())
    (directory/'SETUP.json').write_text(json.dumps({'synthetic_private_test_only':True,'real_authority_credentials':False,
        'network_authority_endpoint':'loopback only','verified_identifier_absent':{'url':url,'http_status':response.status_code,
            'body_sha256':hashlib.sha256(response.content).hexdigest(),'at':today.isoformat()}},indent=2))


if __name__ == '__main__':
    main()
