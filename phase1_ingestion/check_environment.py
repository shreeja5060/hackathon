"""Offline parser checks; add --index after rebuilding to check real retrieval."""
import json
import sys
import tempfile
from pathlib import Path
from .load_environment import DEFAULT_INPUT, parse_environment


def main():
    chunks, _ = parse_environment(DEFAULT_INPUT)
    assert len(chunks) == 6
    assert len({c['chunk_id'] for c in chunks}) == 6
    assert 'mfa_enabled]: false' in chunks[0]['text']
    assert 'mfa_enabled]: true' in chunks[1]['text']
    assert 'mfa_enabled]: unknown / not supplied' in chunks[2]['text']
    data = json.loads(DEFAULT_INPUT.read_text())
    for chunk in chunks:
        position = int(chunk['locator'].split('/')[-1])
        assert data['resources'][position]['resource_id'] in chunk['text']
        assert 'SYNTHETIC DEMO' in chunk['text']
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'bad.json'
        data['resources'].append(data['resources'][0])
        path.write_text(json.dumps(data))
        try:
            parse_environment(path)
        except ValueError as error:
            assert 'Duplicate resource' in str(error)
        else:
            raise AssertionError('Duplicate resource accepted')
    print('PASS: source locations, unique IDs, synthetic labels, true/false/unknown and duplicate rejection')
    if '--index' in sys.argv:
        from .retriever import _index, get_original_chunk, search
        _, collection = _index()
        for chunk in chunks:
            assert get_original_chunk(chunk['chunk_id']) == chunk
        stored = collection.get(where={'source': DEFAULT_INPUT.name}, include=['metadatas'])
        assert len(stored['ids']) == 6
        results = search('Is multifactor authentication MFA enabled for demo-user-alex?', type='evidence', top_k=3)
        assert any('demo-user-alex' in result['text'] for result in results)
        for result in results:
            assert result['type'] == 'evidence'
            print(result['locator'], round(result['score'], 3), result['text'])
        print('PASS: indexed evidence, exact original lookup and MFA retrieval')


if __name__ == '__main__':
    main()
