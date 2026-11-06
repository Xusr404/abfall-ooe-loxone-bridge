"""Offline container release gate; never downloads addresses or calendars."""

import subprocess
import sys
import time
import uuid


def docker(*args, capture=False):
    return subprocess.run(["docker", *args], check=True, text=True,
                          stdout=subprocess.PIPE if capture else None).stdout


def main(image):
    name = "abfall-smoke-" + uuid.uuid4().hex[:12]
    volumes = [name + "-bridge", name + "-addresses"]
    mounts = ["-v", volumes[0] + ":/app/bridge/data",
              "-v", volumes[1] + ":/app/tools/helpers/data"]
    try:
        docker("create", "--name", name, "--network", "none", *mounts, image)
        docker("run", "--rm", "--network", "none", *mounts, "--entrypoint", "python", image, "-c", """
import json, os, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, '/app/bridge')
from core.common import normalized_name_key
assert os.getuid() != 0
today = datetime.now(ZoneInfo('Europe/Vienna')).date()
ids = dict(district_id='2', city_id='10', street_id='20', house_number_id='30')
names = dict(district_name='Synthetic', city_name='Test', street_name='Street', house_number_name='1')
entry = dict(fetched_at=datetime.now(timezone.utc).isoformat(), fetched_on=str(today),
             address_ids=ids, type_intervals={}, had_pickups=True,
             pickups=[dict(waste_type_id='t1', waste_type='Synthetic', date=str(today+timedelta(days=1)),
                           interval_id=None, interval='')])
Path('/app/bridge/data/loxone_bridge_cache.json').write_text(json.dumps(
    dict(version=3, entries={normalized_name_key(names): entry})), encoding='utf-8')
Path('/app/tools/helpers/data/address-data.json').write_text(
    json.dumps(dict(schema_version=1, districts=[])), encoding='utf-8')
""")
        docker("start", name)
        probe = """
import json, os
from urllib.request import urlopen
assert os.getuid() != 0
base = 'http://127.0.0.1:8765'
assert json.load(urlopen(base+'/health', timeout=3)) == {'status':'ok'}
data = json.load(urlopen(base+'/api/v1/pickups?district_name=Synthetic&city_name=Test&street_name=Street&house_number_name=1', timeout=3))
assert data['ok'] and not data['cache_stale'] and data['next_pickup']['days_until'] == 1
assert json.load(urlopen(base+'/setup/api/v1/address-data', timeout=3))['districts'] == []
"""
        for cycle in range(2):
            for attempt in range(30):
                try:
                    docker("exec", name, "python", "-c", probe, capture=True)
                    break
                except subprocess.CalledProcessError:
                    if attempt == 29:
                        docker("logs", name)
                        raise
                    time.sleep(1)
            if cycle == 0:
                docker("restart", name)
        print("Candidate passed offline startup, permissions, volumes, cache and restart checks")
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=False)
        for volume in volumes:
            subprocess.run(["docker", "volume", "rm", volume], check=False)


if __name__ == "__main__":
    main(sys.argv[1])
