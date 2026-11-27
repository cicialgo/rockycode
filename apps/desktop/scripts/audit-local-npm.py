"""Read-only local dependency/IOC audit. Standard library only; never executes packages.
The only writes are this project's ignored artifacts/security report directory.
--refresh downloads two explicitly named public CSV datasets, no executable code.
"""
import argparse
import base64
import collections
import csv
import hashlib
import io
import json
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'artifacts' / 'security'
SOURCES = {
 'shai-hulud-2.csv': 'https://raw.githubusercontent.com/DataDog/indicators-of-compromise/main/shai-hulud-2.0/consolidated_iocs.csv',
 'chaindrop.csv': 'https://raw.githubusercontent.com/DataDog/indicators-of-compromise/keyv-campaign/keyv-campaign/malicious-packages.csv',
}
parser = argparse.ArgumentParser()
parser.add_argument('--refresh', action='store_true')
parser.add_argument('--root', action='append')
args = parser.parse_args()
OUT.mkdir(parents=True, exist_ok=True)
bad = {k:set(v) for k,v in json.loads((ROOT/'scripts/known-malicious-versions.json').read_text())['packages'].items()}
sources=[]
for filename,url in SOURCES.items():
 path=OUT/filename
 if args.refresh:
  payload=urlopen(url,timeout=30).read(2_000_000)
  path.write_bytes(payload)
 if not path.exists():
  raise SystemExit('Missing public IOC snapshot. Run --refresh explicitly first.')
 rows=list(csv.DictReader(io.StringIO(path.read_text())))
 for row in rows:
  name=row.get('package_name') or row.get('package')
  versions=row.get('package_versions') or row.get('versions') or row.get('package_version')
  if name and versions:
   bad.setdefault(name,set()).update(re.findall(r'\d+\.\d+\.\d+(?:-[\w.-]+)?',versions))
 sources.append({'url':url,'rows':len(rows),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
print('IOC loaded:',len(bad),'package names;',sum(len(v) for v in bad.values()),'version rules',flush=True)
roots=args.root or [str(Path.home()),'/usr/local/lib/node_modules','/opt/homebrew/lib/node_modules']
roots=[p for p in roots if Path(p).exists()]
patterns=['package.json','package-lock.json','.package-lock.json','npm-shrinkwrap.json','pnpm-lock.yaml','yarn.lock','bun.lock','bun.lockb','Math_Symbol.js','math_init.js','setup.mjs','settings.json','tasks.json']
cmd=['rg','--files','--hidden','--no-ignore']
for pattern in patterns: cmd+=['-g',pattern]
for pattern in ['!**/.git/**','!**/Mail/**','!**/Messages/**','!**/*.photoslibrary/**','!**/artifacts/security/**','!**/Library/**','!**/Music/**','!**/Pictures/**','!**/Movies/**','!**/Zotero/**']:cmd+=['-g',pattern]
cmd+=roots
walk_timeout=False
try:
 walk=subprocess.run(cmd,capture_output=True,text=True,timeout=90)
 rawpaths=walk.stdout; walk_errors=walk.stderr.splitlines()
except subprocess.TimeoutExpired as e:
 walk_timeout=True
 rawpaths=(e.stdout or b'').decode(errors='replace') if isinstance(e.stdout,bytes) else (e.stdout or '')
 err=e.stderr or b'';walk_errors=(err.decode(errors='replace') if isinstance(err,bytes) else err).splitlines()
paths=sorted(set(rawpaths.splitlines()))
print('Inventory:',len(paths),'candidate files;',len(walk_errors),'walk errors; timed out:',walk_timeout,flush=True)
hits=[]; seen=set(); counts=collections.Counter(); debug_versions=collections.Counter(); problems=[]; binary_locks=[]
observed_pairs=set()

def record(name,version,path,kind,detail=None):
 if not isinstance(name,str) or not isinstance(version,str):return
 observed_pairs.add((name,version))
 if name=='debug':debug_versions[version]+=1
 if version not in bad.get(name,set()) and '*' not in bad.get(name,set()):return
 key=(name,version,str(path),kind,detail)
 if key in seen:return
 seen.add(key)
 hit={'package':name,'version':version,'path':str(path),'evidence':kind}
 if detail:hit['detail']=detail
 hits.append(hit)
 print('MATCH:',name+'@'+version,kind,str(path),flush=True)

def dependencies(tree,path,kind):
 for name,item in (tree or {}).items():
  if isinstance(item,dict):
   record(name,item.get('version'),path,kind)
   dependencies(item.get('dependencies'),path,kind)

pair=re.compile(r'(?<![A-Za-z0-9_.-])((?:@[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+)@([0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9_.-]+)?)')
hashes={
 '9fc2570b7cef51c1b8df116d144d11ff4096357be7d2c4c6367cfc2509cf1bcc':'ChainDrop payload',
 'fd3ca4007b225fdf8de7af4345a19179d5efa8c4bb9205f88cda806e5684b1eb':'ChainDrop loader',
 '927387d0cfac1118df4b383decc2ea6ba49c9d2f98b47098bcbcba1efc026e1f':'ChainDrop VS Code hook',
 '14eb4ce01dd4307759887ff819359b70d7d9ff709ecde039a5abc1aac325b128':'ChainDrop Claude hook',
}
for i,filename in enumerate(paths):
 path=Path(filename); name=path.name
 try:
  if name in ['settings.json','tasks.json'] and not any(x in path.parts for x in ['.vscode','.claude']):continue
  if path.is_symlink(): counts['symlinks_skipped']+=1;continue
  if path.stat().st_size>25_000_000:problems.append({'path':filename,'reason':'over 25 MB limit'});continue
  if name=='bun.lockb':binary_locks.append(filename);continue
  data=path.read_bytes(); counts[name]+=1
  if name in ['Math_Symbol.js','math_init.js','setup.mjs','settings.json','tasks.json']:
   digest=hashlib.sha256(data).hexdigest()
   if digest in hashes: hits.append({'path':filename,'evidence':'known_payload_sha256','detail':hashes[digest],'sha256':digest})
   continue
  text=data.decode('utf8')
  if name.endswith('.json'):
   obj=json.loads(text)
   if name=='package.json':
    record(obj.get('name'),obj.get('version'),path,'installed_manifest' if 'node_modules' in path.parts else 'source_manifest')
    for group in ['dependencies','devDependencies','optionalDependencies']:
     for dep,version in (obj.get(group) or {}).items():
      record(dep,version,path,'exact_manifest_reference')
   else:
    for package,item in (obj.get('packages') or {}).items():
     if package:
      record(item.get('name') or package.split('node_modules/')[-1],item.get('version'),path,'locked_version')
    dependencies(obj.get('dependencies'),path,'locked_version')
  elif name=='yarn.lock':
   names=[]
   for line in text.splitlines():
    if line and not line[0].isspace() and line.endswith(':'):
     names=re.findall(r'(?:^|[,\s\"])((?:@[^/\s]+/)?[^@\s\",]+)@',line)
    match=re.match(r'\s+version\s*:?\s+[\"\x27]?([^\s\"\x27]+)',line)
    if match:
     for dep in names:record(dep,match[1],path,'yarn_locked_version')
  else:
   for match in pair.finditer(text): record(match[1],match[2],path,'text_lock_reference')
   if name=='pnpm-lock.yaml':
    for match in re.finditer(r'^\s+[\"\x27]?/?((?:@[^/\s]+/)?[^/\s:\"\x27]+)/([0-9]+\.[0-9]+\.[0-9]+)[\"\x27]?:',text,re.M):
     record(match[1],match[2],path,'pnpm_legacy_lock_reference')
 except (OSError,UnicodeError,ValueError,TypeError,AttributeError) as e:
  problems.append({'path':filename,'reason':type(e).__name__})
 if i and i%15000==0:print('Parsed',i,'files',flush=True)

# Cache index records identify fetched tarballs. Registry metadata listing a
# bad historical version is NOT counted as an installed/downloaded package.
cache=Path.home()/'.npm'/'_cacache'
index=cache/'index-v5'
for path in index.rglob('*') if index.exists() else []:
 if not path.is_file():continue
 try:
  counts['npm_cache_index_files']+=1
  for line in path.read_text(errors='replace').splitlines():
   if '\t' not in line:continue
   item=json.loads(line.split('\t',1)[1]);key=item.get('key','')
   if not key.startswith('make-fetch-happen:request-cache:https://'):continue
   url=key.split('request-cache:',1)[1];parts=unquote(urlparse(url).path).strip('/').split('/-/')
   if len(parts)!=2:continue
   dep=parts[0];leaf=dep.split('/')[-1]
   match=re.fullmatch(re.escape(leaf)+r'-(.+)\.tgz',parts[1])
   if not match:continue
   version=match[1];integrity=item.get('integrity','');present=False
   try:
    algorithm,encoded=integrity.split('-',1);digest=base64.b64decode(encoded).hex()
    present=(cache/'content-v2'/algorithm/digest[:2]/digest[2:4]/digest[4:]).is_file()
   except (ValueError,TypeError):pass
   record(dep,version,path,'cached_tarball' if present else 'cache_index_history',str(item.get('time','')))
 except (OSError,ValueError):problems.append({'path':str(path),'reason':'cache index unreadable'})
# Only structured installation records; never copy complete log lines or tokens.
for path in (Path.home()/'.npm'/'_logs').glob('*debug*.log'):
 try:
  counts['npm_logs']+=1
  for number,line in enumerate(path.read_text(errors='replace').splitlines(),1):
   kind=None
   if re.search(r'\binfo run\s',line):kind='lifecycle_execution_log'
   elif re.search(r'\bsilly placeDep\s',line):kind='resolution_log'
   if kind:
    for match in pair.finditer(line):record(match[1],match[2],path,kind,'line '+str(number))
 except OSError:problems.append({'path':str(path),'reason':'log unreadable'})
report={'createdAt':time.strftime('%Y-%m-%dT%H:%M:%S%z'),'roots':roots,'sources':sources,'knownPackageNames':len(bad),'knownVersionRules':sum(len(v) for v in bad.values()),'candidateFiles':len(paths),'scannedFiles':paths,'counts':dict(counts),'debugVersions':dict(sorted(debug_versions.items())),'hits':hits,'inventoryTimedOut':walk_timeout,'walkErrors':walk_errors,'unreadable':problems,'unsupportedBinaryLocks':binary_locks,'limits':['Current files/cache/logs only. Deleted history and .git objects are not scanned.','Exact known-version matching; text locks require confirmation. Not a full malware or host compromise investigation.','No package code executed, no dependencies changed, no credentials read or uploaded.','Library, Music, Pictures, Movies, Zotero and .git are excluded; app.asar/archives and other users/volumes are not scanned. Symlinks and files over 25 MB skipped.']}
(OUT/'local-npm-audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
summary={k:report[k] for k in ['candidateFiles','counts','knownPackageNames','knownVersionRules','debugVersions','hits','inventoryTimedOut']}
summary.update(walkErrorCount=len(walk_errors),unreadableCount=len(problems),unsupportedBinaryLockCount=len(binary_locks),report=str(OUT/'local-npm-audit.json'))
print(json.dumps(summary,ensure_ascii=False,indent=2))
