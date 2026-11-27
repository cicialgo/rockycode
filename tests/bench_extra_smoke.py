"""Isolated offline bench dependency acceptance probe; no model, Docker, or remote dataset."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from tempfile import TemporaryDirectory

def run(code, *args, env=None):
    return subprocess.run([sys.executable, "-c", code, *map(str, args)],
                          capture_output=True, text=True, timeout=90, env=env, check=True)

def parallel(code, argv, env=None):
    children = [subprocess.Popen([sys.executable, "-c", code, *map(str, a)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
                for a in argv]
    results = []
    try:
        for p in children:
            out, err = p.communicate(timeout=90)
            assert p.returncode == 0, (p.returncode, out, err)
            results.append(out.strip())
    finally:
        for p in children:
            if p.poll() is None:
                p.kill()
                p.wait()
    return results

with TemporaryDirectory(prefix="rocky-bench-accept-") as tmp:
    root = Path(tmp)
    env = dict(os.environ, HF_DATASETS_OFFLINE="1", HF_HUB_OFFLINE="1",
               HF_HOME=str(root/"hf"), VIRTUALENV_OVERRIDE_APP_DATA=str(root/"appdata"),
               VIRTUALENV_NO_PERIODIC_UPDATE="1")
    # Real process contention, including a blocked acquisition and release.
    from filelock import FileLock, Timeout
    target = root/"counter"
    target.write_text("0")
    lock = FileLock(str(target)+".lock")
    with lock:
        out = run("""
import sys
from filelock import FileLock, Timeout
try:
    with FileLock(sys.argv[1], timeout=0.15): raise AssertionError("overlapping holder")
except Timeout:
    print("timeout-correct")
""", str(target)+".lock").stdout.strip()
        assert out == "timeout-correct"
    for cls in ["FileLock", "SoftFileLock"]:
        target.write_text("0")
        code = """
import sys,time,filelock
from pathlib import Path
p=Path(sys.argv[1]); lock=getattr(filelock, sys.argv[2])(str(p)+'.'+sys.argv[2]+'.lock', timeout=20)
for _ in range(30):
    with lock:
        n=int(p.read_text()); time.sleep(0.001); p.write_text(str(n+1))
"""
        parallel(code, [(target, cls)]*4, env)
        assert target.read_text()=="120", cls
    # Harmless same-user check of preservation of an existing linked file.
    victim=root/"sentinel"; victim.write_text("keep-me"); victim.chmod(0o640)
    linked=root/"linked.lock"; os.link(victim,linked)
    try:
        with FileLock(linked, mode=0o666, timeout=0.15):
            pass
    except (OSError, Timeout):
        pass
    assert victim.read_text()=="keep-me"
    assert victim.stat().st_mode & 0o777 == 0o640
    print("PASS lock timeout, FileLock and SoftFileLock 4x30 contention, linked-file preservation")

    # Multiple contenders reclaim a marker left by a killed owner (filelock #899).
    # This exercises the lifecycle but does not deterministically force every upstream race.
    for attempt in range(12):
        stale = root/f"dead-{attempt}.lock"
        holder = subprocess.Popen([sys.executable, "-c", """
import sys,time
from filelock import SoftFileLock
lock=SoftFileLock(sys.argv[1]); lock.acquire(); print('held',flush=True)
time.sleep(30)
""", str(stale)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        try:
            assert holder.stdout.readline().strip() == "held"
        finally:
            holder.kill()
            holder.wait(timeout=5)
        assert stale.exists()
        counter=root/f"dead-{attempt}.counter"; counter.write_text("0")
        code = """
import sys,time,os
from pathlib import Path
from filelock import SoftFileLock
lock=SoftFileLock(sys.argv[1], timeout=10); counter=Path(sys.argv[2])
sentinel=str(counter)+'.exclusive'
while time.monotonic()<float(sys.argv[3]): time.sleep(0.001)
for _ in range(5):
    with lock:
        fd=os.open(sentinel,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        try:
            n=int(counter.read_text()); time.sleep(0.003); counter.write_text(str(n+1))
        finally:
            os.close(fd); os.unlink(sentinel)
"""
        parallel(code,[(stale,counter,time.monotonic()+0.4)]*3,env)
        assert counter.read_text()=="15"
    print("PASS dead SoftFileLock holder recovery: 12 rounds, 3 contenders, no overlapping critical section")

    # Real datasets JSON builder cache, concurrent first reads and warm reuse.
    source=root/"rows.jsonl"
    source.write_text("".join(json.dumps({"instance_id":str(i),"value":i})+"\n" for i in range(12)))
    code = """
import sys,json
from datasets import load_dataset
d=load_dataset('json', data_files=sys.argv[1], split='train', cache_dir=sys.argv[2])
assert len(d)==12 and sum(d['value'])==66
print(json.dumps({'fingerprint':d._fingerprint,'files':[x['filename'] for x in d.cache_files]}))
"""
    values=[json.loads(s) for s in parallel(code,[(source,root/"dataset-cache")]*4,env)]
    assert len({v["fingerprint"] for v in values})==1
    files={str(p):p.stat().st_mtime_ns for p in (root/"dataset-cache").rglob("*.arrow")}
    assert files
    warm=json.loads(run(code,source,root/"dataset-cache",env=env).stdout)
    assert warm["fingerprint"]==values[0]["fingerprint"]
    assert files=={str(p):p.stat().st_mtime_ns for p in (root/"dataset-cache").rglob("*.arrow")}
    print("PASS datasets 4 concurrent cold readers and warm cache reuse")

    # Exercise virtualenv + python-discovery + distlib using shared seed/cache.
    code = """
import subprocess,sys,pathlib,json
dest=pathlib.Path(sys.argv[1])
subprocess.run([sys.executable,'-m','virtualenv','--no-download','--no-periodic-update',
               '--app-data',sys.argv[2], '--python',sys.executable,str(dest)],
               check=True,capture_output=True,text=True,timeout=60)
p=dest/'bin'/'python'
r=subprocess.run([str(p),'-c','import sys,json; print(json.dumps([sys.prefix, sys.version_info[:2]]))'],
                 check=True,capture_output=True,text=True,timeout=10)
assert pathlib.Path(json.loads(r.stdout)[0])==dest
subprocess.run([str(p),'-m','pip','--version'],check=True,capture_output=True,text=True,timeout=10)
print('created')
"""
    assert parallel(code,[(root/f"venv-{i}",root/"appdata") for i in range(3)],env)==["created"]*3
    assert run(code,root/"venv-warm",root/"appdata",env=env).stdout.strip()=="created"
    assert list((root/"appdata").rglob("*"))
    print("PASS virtualenv 3 concurrent seeded environments and warm cache; discovery and pip work")
print("BENCH_ACCEPTANCE_PASS")
