#!/usr/bin/env python3
"""Reproduce a paired, target-level descriptive analysis from public frozen inputs.

No network, Foldseek searches, model fitting, pose inference or feature changes.
"""
from __future__ import annotations
import json, math, random, statistics
from collections import Counter, defaultdict
from hashlib import sha256
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPORT = HERE.parents[1] / 'docs/weekly-training-similarity-results.json'
SAMPLES = 5000
SEED = 20261004


def ranks(values):
    order = sorted(range(len(values)), key=lambda i: values[i])
    result = [0.] * len(values)
    start = 0
    while start < len(order):
        stop = start + 1
        while stop < len(order) and values[order[stop]] == values[order[start]]:
            stop += 1
        for i in order[start:stop]:
            result[i] = (start + stop + 1) / 2
        start = stop
    return result


def auc(labels, scores):
    n = sum(labels)
    if not n or n == len(labels):
        return None
    return (sum(r for r, y in zip(ranks(scores), labels) if y) - n*(n+1)/2) / (n*(len(labels)-n))


def rho(a, b):
    a, b = ranks(a), ranks(b)
    if len(set(a)) < 2 or len(set(b)) < 2:
        return None
    am, bm = statistics.mean(a), statistics.mean(b)
    return sum((x-am)*(y-bm) for x,y in zip(a,b)) / math.sqrt(sum((x-am)**2 for x in a)*sum((y-bm)**2 for y in b))


def percentile(values, probability):
    values = sorted(values)
    pos = probability * (len(values)-1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    return values[lo] + (values[hi]-values[lo]) * (pos-lo)


def numeric(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def valid(row, score, classification):
    return numeric(row.get(score)) and row.get(classification) in ('familiar', 'novel')


def estimate(rows, score_keys, pairs, selectors):
    out = {}
    for key in score_keys:
        for endpoint in ('oracle_correct', 'confidence_correct'):
            out[f'{key}.auc.{endpoint}'] = auc([r[endpoint] for r in rows], [r[key] for r in rows])
        out[f'{key}.spearman.negative_best_rmsd'] = rho([r[key] for r in rows], [-r['best_rmsd'] for r in rows])
    for left, right in pairs:
        for endpoint in ('oracle_correct', 'confidence_correct'):
            a, b = out[f'{left}.auc.{endpoint}'], out[f'{right}.auc.{endpoint}']
            out[f'{right}-minus-{left}.auc.{endpoint}'] = None if a is None or b is None else b-a
    for selector in selectors:
        out[f'{selector}.raw_pose_success'] = statistics.mean(r[selector+'_correct'] for r in rows)
        out[f'{selector}.selected_rmsd_median'] = statistics.median(r[selector+'_rmsd'] for r in rows)
        if selector in ('nearest','pocket','rnp'):
            score_key = {'nearest':'nearest_score','pocket':'pocket_aware_score','rnp':'rnp_blind_score'}[selector]
            out[f'{selector}.pose_or_none_success'] = statistics.mean((not r['oracle_correct']) if r[score_key]<.25 else r[selector+'_correct'] for r in rows)
    out['oracle.raw_pose_available'] = statistics.mean(r['oracle_correct'] for r in rows)
    out['random.expected_raw_pose_success'] = statistics.mean(r['random_success'] for r in rows)
    for left, right in [('confidence', s) for s in selectors if s != 'confidence']:
        out[f'{right}-minus-{left}.raw_pose_success'] = statistics.mean(r[right+'_correct'] - r[left+'_correct'] for r in rows)
    return out


def summarize(rows, *, scores=(), pairs=(), selectors=('confidence',)):
    point = estimate(rows, scores, pairs, selectors)
    samples = {k: [] for k in point}
    # Keep all estimates paired and retain the three observed week sizes.
    groups = defaultdict(list)
    for row in rows:
        groups[row['week']].append(row)
    rng = random.Random(SEED)
    for _ in range(SAMPLES):
        draw = [group[rng.randrange(len(group))] for _, group in sorted(groups.items()) for _ in group]
        for key, value in estimate(draw, scores, pairs, selectors).items():
            if value is not None:
                samples[key].append(value)
    return {
        'n': len(rows), 'oracle_positive': sum(r['oracle_correct'] for r in rows),
        'confidence_positive': sum(r['confidence_correct'] for r in rows),
        'weeks': dict(sorted(Counter(r['week'] for r in rows).items())),
        'target_ids': [r['item_id'] for r in rows],
        'metrics': {k: {'estimate': v, 'ci95': [percentile(samples[k], .025), percentile(samples[k], .975)] if samples[k] else None, 'valid_bootstrap_samples': len(samples[k])} for k, v in point.items()},
        'weekly_point_estimates': {week: {'n':len(group), 'metrics':estimate(group, scores, pairs, selectors)} for week, group in sorted(groups.items())},
    }


def main():
    report = json.loads(REPORT.read_text())
    outcomes_path = HERE/'outcomes.json'
    outcomes = json.loads(outcomes_path.read_text())
    by_id = {(r['week'], r['item_id']): r for r in outcomes['records']}
    rows = []
    for row in report['records']:
        row = dict(row)
        target = by_id.pop((row['week'], row['item_id']))
        choices = {c['id']:c for c in target['choices']}
        assert len(choices) == 10
        oracle = any(c['correct'] for c in choices.values())
        assert row['has_correct_pose'] is None or row['has_correct_pose'] == oracle
        row.update(round_id=target['round_id'], oracle_correct=oracle,
                   best_rmsd=min(c['rmsd'] for c in choices.values()),
                   random_success=sum(c['correct'] for c in choices.values())/len(choices))
        confidence = min(choices.values(), key=lambda c: (-c['ligand_plddt'], c['id']))
        smina = min(choices.values(), key=lambda c: (c['smina'], c['id']))
        selections = {'confidence':confidence['id'], 'smina':smina['id'],
                      'nearest':row['nearest_choice_id'], 'pocket':row['pocket_aware_choice_id'], 'rnp':row['rnp_blind_choice_id']}
        row['selection_mapping'] = {}
        for selector, choice_id in selections.items():
            if choice_id is None:
                continue
            c = choices[choice_id]  # Exact public choice identity; fail on a stale report.
            row[selector+'_correct'] = c['correct']
            row[selector+'_rmsd'] = c['rmsd']
            row['selection_mapping'][selector] = {k:c[k] for k in ('id','pose_sha256','method','method_version','rmsd','correct')}
        rows.append(row)
    assert not by_id and len(rows) == len({r['item_id'] for r in rows}) == 100
    nearest = [r for r in rows if valid(r, 'nearest_score', 'nearest_classification') and r['classification'] in ('familiar','novel')]
    assert len(nearest) == 80
    # Reproduce the original reported label-proxy AUROC, distinct from pose utility.
    label_auc = auc([r['classification']=='familiar' for r in nearest], [r['nearest_score'] for r in nearest])
    assert round(label_auc,4) == report['blind_estimators']['nearest_training_system']['auroc']
    main_rows = [r for r in nearest if valid(r,'train_shape_overlap','classification') and valid(r,'pocket_aware_score','pocket_aware_classification')]
    rnp_rows = [r for r in rows if valid(r,'rnp_exact_score','rnp_exact_classification') and valid(r,'rnp_blind_score','rnp_blind_classification')]
    common = [r for r in main_rows if r in rnp_rows]
    for r in nearest:
        r['crystal_familiar_binary'] = int(r['classification']=='familiar')
    blind_only = [r for r in rows if valid(r,'nearest_score','nearest_classification') and valid(r,'pocket_aware_score','pocket_aware_classification')]
    cohorts = {
      'all_available_blind_predictions': summarize(blind_only, scores=('nearest_score','pocket_aware_score'), pairs=(), selectors=('confidence','smina','nearest','pocket')),
      'crystal_overlap_vs_blind_same_targets': summarize(main_rows, scores=('train_shape_overlap','nearest_score','pocket_aware_score'), pairs=(('train_shape_overlap','nearest_score'),('train_shape_overlap','pocket_aware_score')), selectors=('confidence','smina','nearest','pocket')),
      'rnp_crystal_vs_blind_same_targets': summarize(rnp_rows, scores=('rnp_exact_score','rnp_blind_score'), pairs=(('rnp_exact_score','rnp_blind_score'),), selectors=('confidence','smina','rnp')),
      'all_metric_families_same_targets': summarize(common, scores=('train_shape_overlap','nearest_score','pocket_aware_score','rnp_exact_score','rnp_blind_score'), pairs=(('train_shape_overlap','nearest_score'),('train_shape_overlap','pocket_aware_score'),('rnp_exact_score','rnp_blind_score')), selectors=('confidence','smina','nearest','pocket','rnp')),
      'broader_blind_availability': summarize(nearest, scores=('crystal_familiar_binary','nearest_score','pocket_aware_score'), pairs=(('crystal_familiar_binary','nearest_score'),), selectors=('confidence','smina','nearest','pocket')),
    }
    def missing_group(group):
        return {'n':len(group),'oracle_positive':sum(r['oracle_correct'] for r in group),'confidence_positive':sum(r['confidence_correct'] for r in group),'crystal_classes':dict(Counter(r['classification'] for r in group)),'crystal_reasons':dict(Counter(r['reason'] for r in group))}
    output = {'format':'foldarium.paired-foldseek-analysis/v1','date':'2026-10-04',
      'source_report_sha256':sha256(REPORT.read_bytes()).hexdigest(),'outcomes_sha256':sha256(outcomes_path.read_bytes()).hexdigest(),
      'bootstrap':{'samples':SAMPLES,'seed':SEED,'unit':'target; paired across metrics; stratified by observed week','interval':'percentile95','unique_pdb_targets':100,'weeks':3},
      'original_proxy_label_auroc':label_auc,'cohorts':cohorts,
      'missingness':{'all':missing_group(rows),'continuous_main_included':missing_group(main_rows),'continuous_main_excluded':missing_group([r for r in rows if r not in main_rows]),'blind_nearest_missing':missing_group([r for r in rows if r not in blind_only]),'broader_label_comparison_excluded':missing_group([r for r in rows if r not in nearest]),'numeric_but_unknown_crystal':[r['item_id'] for r in rows if numeric(r['train_shape_overlap']) and r['classification']=='unknown']},
      'records':[{k:r[k] for k in ('round_id','week','item_id','oracle_correct','best_rmsd','confidence_correct','random_success','selection_mapping')} for r in rows]}
    (HERE/'results.json').write_text(json.dumps(output,indent=2,sort_keys=True,allow_nan=False)+'\n')
    print(json.dumps({'cohorts':{k:v['n'] for k,v in cohorts.items()},'missingness':output['missingness']},indent=2))


if __name__ == '__main__':
    assert auc([True,False],[1,0]) == 1
    assert auc([True,False],[0,0]) == .5
    assert auc([True,False],[0,1]) == 0
    main()
