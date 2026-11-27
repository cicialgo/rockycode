// Only Node built-ins: this check does not import or execute dependencies.
import {readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
const raw=readFileSync(new URL('../package-lock.json',import.meta.url));
const lock=JSON.parse(raw);
const manifest=JSON.parse(readFileSync(new URL('../package.json',import.meta.url),'utf8'));
const baseline=JSON.parse(readFileSync(new URL('./dependency-baseline.json',import.meta.url),'utf8'));
const incidents=JSON.parse(readFileSync(new URL('./known-malicious-versions.json',import.meta.url),'utf8')).packages;
const errors=[];
if(createHash('sha256').update(raw).digest('hex')!==baseline.lockSha256) errors.push('Lockfile changed: review added/updated dependencies and update the review baseline deliberately.');
for(const [name,version] of Object.entries({...manifest.dependencies,...manifest.devDependencies})) {
 if(!/^\d+\.\d+\.\d+$/.test(version)) errors.push(`Direct dependency must be pinned: ${name}`);
 if(lock.packages[`node_modules/${name}`]?.version!==version) errors.push(`Manifest/lock mismatch: ${name}`);
}
const entries=Object.entries(lock.packages).filter(([path])=>path);
for(const [path,pkg] of entries) {
 const name=path.split('node_modules/').at(-1);
 let url; try {url=new URL(pkg.resolved);} catch {}
 if(!url||url.protocol!=='https:'||url.hostname!=='registry.npmjs.org'||url.username||url.password||url.port) errors.push(`Unapproved dependency source: ${path}`);
 if(pkg.link||!pkg.integrity?.startsWith('sha512-')) errors.push(`Missing registry integrity or local link: ${path}`);
 if(incidents[name]?.some(version=>version==='*'||version===pkg.version)) errors.push(`KNOWN MALICIOUS: ${name}@${pkg.version}`);
 if(pkg.hasInstallScript&&baseline.reviewedInstallScriptPackages[name]!==pkg.version) errors.push(`Unreviewed install script package: ${name}@${pkg.version}`);
}
if(errors.length) throw new Error(errors.join('\n'));
console.log(`Dependency policy passed: ${entries.length} locked entries; official registry + SHA-512 + pinned versions; selected historical IOC check passed. Install scripts remain disabled.`);
