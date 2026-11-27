"""Prepare arm-blinded repository conversations for human/assistant review.

No model calls. Keep the separate key closed until judgments are written.
The packet contains full tool evidence; the compact index is not a substitute
for checking claims against that evidence and the immutable initial source.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path

from bench.prompt_ablation import episode_path, write_json


def prepare(run: Path, output: Path):
    manifest=json.loads((run/'manifest.json').read_text())
    cases=json.loads((run/'repo-cases.json').read_text())
    fixtures=json.loads((run/'repo-fixtures.json').read_text())
    rows=json.loads((run/'results.json').read_text()) if (run/'results.json').exists() else []
    plan=manifest['plan']
    shuffled=list(plan)
    # Different batches must not reuse IDs after an earlier key is decoded.
    review_name = manifest.get('review_identity', run.name)
    review_seed = int(hashlib.sha256((review_name + ':repo-review-v1').encode()).hexdigest(), 16)
    random.Random(review_seed).shuffle(shuffled)
    key={f'sample-{i:03d}':spec for i,spec in enumerate(shuffled,1)}
    output.mkdir(parents=True,exist_ok=False)
    write_json(output/'key.json',key)
    (output/'source_review.py').write_bytes(Path(__file__).read_bytes())
    write_json(output/'initial-source.json',fixtures)
    index=[]
    for bid,spec in key.items():
        row=next((r for r in rows if all(r[k]==v for k,v in spec.items())),None)
        if row is None:
            index.append({'id':bid,'status':'not_started'})
            continue
        i=next(i for i,p in enumerate(plan,1) if p==spec)
        dest=episode_path(run,i,spec)
        checkpoint=json.loads((dest/'checkpoint.json').read_text())
        final_history=[m for m in checkpoint['history'] if m['role']!='system']
        events=[]
        complete_evidence=True
        for raw_path in row.get('trajectories', [row['trajectory']]):
            path=Path(raw_path)
            if not path.is_file():
                complete_evidence=False
                continue
            events.extend(json.loads(line) for line in path.read_text().splitlines())
        # The final checkpoint may have compacted away early tool receipts.
        # Recover every appended message from the immutable trajectory, without
        # meta/system/reasoning records that could reveal the candidate prompt.
        history=[e['data'] for e in events if e['kind']=='message' and e['data']['role']!='system']
        if not history:
            history=final_history
            complete_evidence=False
        turns=row['turns']+([row['partial_turn']] if row.get('partial_turn') else [])
        final_path=dest/f'files-turn-{len(turns)}.json'
        files=json.loads(final_path.read_text()) if final_path.exists() else {}
        initial=fixtures[spec['case']]
        modified={k:files.get(k) for k in sorted(initial.keys()|files.keys()) if initial.get(k)!=files.get(k)}
        packet={'id':bid,'case':spec['case'],'rubric':cases[spec['case']]['rubric'],
            'status':row['status'],'turns':turns,'history_with_tool_evidence':history,
            'final_request_history':final_history,'complete_trajectory_evidence':complete_evidence,
            'compactions':sum(e['kind']=='compaction' for e in events),
            'final_changes':modified,'acceptance':row['acceptance'],'mechanical_pass':row['mechanical_pass']}
        write_json(output/(bid+'.json'),packet)
        index.append({'id':bid,'case':spec['case'],'status':row['status'],
            'mechanical_pass':row['mechanical_pass'],'answer_chars':sum(len(t['answer']) for t in turns)})
    write_json(output/'index.json',index)
    write_json(output/'review-notes-template.json',{'reviewer':'not independent human review',
        'instruction':'Record concrete claims, contradictory/supporting evidence, scope, tests, uncertainty. Do not infer semantic accuracy from mechanical_pass. Save judgments before decoding key.json.',
        'samples':[]})
    print(json.dumps({'prepared':len(rows),'planned':len(plan),'model_calls':0}))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('run',type=Path)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    prepare(args.run.resolve(),args.output.resolve())


if __name__=='__main__':
    main()
