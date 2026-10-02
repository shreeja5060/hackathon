"""Evaluate expected cited passages at ranks 1–3, without LLM calls.
Run: python -m phase1_ingestion.evaluate_retrieval
"""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = Path(__file__).parent / 'evaluation/retrieval_cases.json'
FIELDS = {'chunk_id', 'text', 'source', 'page', 'type', 'doc_kind', 'locator', 'score'}


def matches(record, expected):
    text = ' '.join(record['text'].lower().split())
    return (all(record.get(key) == expected[key] for key in ('source', 'locator', 'page'))
            and all(' '.join(term.lower().split()) in text for term in expected['text_contains']))


def metrics(rows):
    n = len(rows)
    return {'questions': n,
            'hit_at_1': sum(row['rank'] == 1 for row in rows) / n,
            'hit_at_3': sum(row['rank'] is not None for row in rows) / n,
            'mrr_at_3': sum(1 / row['rank'] if row['rank'] else 0 for row in rows) / n}


def validate_cases(cases):
    if not cases or len({case['id'] for case in cases}) != len(cases):
        raise ValueError('Empty evaluation or duplicate case IDs')
    for case in cases:
        if case['type'] not in {'internal', 'framework', 'evidence'}:
            raise ValueError('Invalid filter')
        if not case['query'].strip() or not case['expected']['text_contains']:
            raise ValueError('Query and expected text are required')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--validate-only', action='store_true')
    args = parser.parse_args()
    raw = CASES.read_bytes()
    cases = json.loads(raw)['cases']
    validate_cases(cases)
    if args.validate_only:
        print(f'Validated {len(cases)} evaluation definitions; no searches run.')
        return
    from .retriever import _index, search
    manifest, collection = _index()
    # Check targets before measuring relevance. Missing input is a setup error.
    stored = collection.get(include=['documents', 'metadatas'])
    records = [dict(meta, text=text) for meta, text in zip(stored['metadatas'], stored['documents'], strict=True)]
    missing = [case['id'] for case in cases if not any(
        record['type'] == case['type'] and matches(record, case['expected']) for record in records)]
    if missing:
        raise RuntimeError('Expected passages missing from active index: ' + ', '.join(missing)
                           + '. Check source data and rebuild before evaluation.')
    rows = []
    for case in cases:
        results = search(case['query'], type=case['type'], top_k=3)
        for result in results:
            if set(result) != FIELDS or result['type'] != case['type']:
                raise RuntimeError('Search contract mismatch: ' + case['id'])
        rank = next((i for i, result in enumerate(results, 1) if matches(result, case['expected'])), None)
        rows.append({**case, 'rank': rank, 'results': results})
        print(f"{'HIT' if rank else 'MISS'} {case['id']}: expected passage rank {rank or 'outside top 3'}")
    groups = defaultdict(list)
    for row in rows:
        groups[row['type']].append(row)
    summary = {'overall': metrics(rows), **{key: metrics(value) for key, value in groups.items()}}
    report = {'created_at': datetime.now(timezone.utc).isoformat(),
              'collection_name': manifest['collection_name'],
              'model_name': manifest['model_name'], 'model_revision': manifest['model_revision'],
              'cases_sha256': hashlib.sha256(raw).hexdigest(), 'summary': summary, 'cases': rows}
    output = ROOT / 'data/processed/retrieval_evaluation.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + '\n')
    for group, values in summary.items():
        print(f"{group}: Hit@1={values['hit_at_1']:.1%}, Hit@3={values['hit_at_3']:.1%}, MRR@3={values['mrr_at_3']:.3f} ({values['questions']} questions)")
    print(f'Full passages and scores saved to {output}')
    print('Development retrieval evaluation only; not answer or compliance accuracy. Review misses manually.')


if __name__ == '__main__':
    main()
