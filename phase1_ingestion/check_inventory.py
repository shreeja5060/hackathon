"""Check demo inventory validation; --index also checks actual indexed retrieval."""
import argparse
import copy
import json
from pathlib import Path
import tempfile
from .load_inventory import DEFAULT_INPUT, ENVIRONMENT_INPUT, parse_inventory


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index', action='store_true')
    args = parser.parse_args()
    chunks, _ = parse_inventory()
    require(len(chunks) == 6, 'Expected six inventory entries')
    require(len({item['chunk_id'] for item in chunks}) == 6, 'Duplicate IDs')
    require('privileged administrator account' in chunks[0]['text'], 'Alex privilege missing')
    require('non-privileged standard account' in chunks[1]['text'], 'Sam privilege missing')
    require('Account privilege level: unknown / not supplied' in chunks[2]['text'], 'Jordan privilege guessed')
    require('Owner: unknown / not supplied' in chunks[2]['text'], 'Jordan owner guessed')
    require('not applicable to this resource type' in chunks[3]['text'], 'Bucket assigned account privilege')
    data = json.loads(DEFAULT_INPUT.read_text())
    environment = json.loads(ENVIRONMENT_INPUT.read_text())
    for index, chunk in enumerate(chunks):
        require(chunk['locator'] == f'/assets/{index}', 'Wrong source pointer')
        resource = data['assets'][index]
        linked = environment['resources'][index]
        require((resource['resource_id'], resource['resource_type']) ==
                (linked['resource_id'], linked['resource_type']), 'Wrong resource link')
        require(f'/resources/{index}' in chunk['text'], 'Configuration pointer missing')
    cases = []
    duplicate = copy.deepcopy(data); duplicate['assets'].append(duplicate['assets'][0]); cases.append(duplicate)
    unlinked = copy.deepcopy(data); unlinked['assets'][0]['resource_id'] = 'absent-user'; cases.append(unlinked)
    wrong_env = copy.deepcopy(data); wrong_env['environment'] = 'other-environment'; cases.append(wrong_env)
    wrong_time = copy.deepcopy(data); wrong_time['captured_at'] = '2026-10-01T18:00:00Z'; cases.append(wrong_time)
    wrong_privilege = copy.deepcopy(data); wrong_privilege['assets'][0]['privilege_level'] = 'maybe'; cases.append(wrong_privilege)
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / DEFAULT_INPUT.name
        for case in cases:
            path.write_text(json.dumps(case))
            try:
                parse_inventory(path)
            except ValueError:
                pass
            else:
                raise AssertionError('Invalid inventory accepted')
    print('PASS: six source/resource links; privileged, non-privileged, unknown and not-applicable values')
    print('PASS: rejected duplicates, unlinked resources, wrong environment/time and invalid privilege')
    if args.index:
        from .retriever import _index, get_original_chunk, search
        _, collection = _index()
        records = collection.get(where={'source': DEFAULT_INPUT.name}, include=['metadatas'])
        require(len(records['ids']) == 6, 'Rebuild index with inventory first')
        for chunk in chunks:
            require(get_original_chunk(chunk['chunk_id']) == chunk, 'Original inventory lookup mismatch')
        questions = [
            ('What is the account privilege level of demo-user-alex?', 0),
            ('Is demo-user-sam a standard non-privileged account?', 1),
            ('Who owns demo-user-jordan and is their account privilege known?', 2),
            ('Who owns demo-bucket-documents and what is its purpose?', 3),
            ('Who owns demo-sg-admin and what is its purpose?', 4),
            ('What is the inventory purpose and owner of demo-trail?', 5)]
        misses = []
        for query, index in questions:
            results = search(query, type='evidence', top_k=3)
            rank = next((position for position, hit in enumerate(results, 1)
                         if hit['source'] == DEFAULT_INPUT.name and hit['locator'] == chunks[index]['locator']), None)
            print(f"{'HIT' if rank else 'MISS'} {query}: rank {rank}")
            if rank is None:
                misses.append(query)
                for hit in results:
                    print('  Retrieved:', hit['source'], hit['locator'], hit['score'])
        require(not misses, 'Inventory retrieval misses need review; share the output')
        print('PASS: inventory exact lookup and six top-three retrieval checks')


if __name__ == '__main__':
    main()
