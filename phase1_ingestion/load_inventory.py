"""Load the normalized synthetic inventory and verify links to demo configuration."""
import hashlib
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / 'data/examples/synthetic_asset_inventory.json'
ENVIRONMENT_INPUT = ROOT / 'data/examples/synthetic_environment.json'


def digest(value):
    return hashlib.sha256(value).hexdigest()


def parse_inventory(path=DEFAULT_INPUT, environment_path=ENVIRONMENT_INPUT):
    path, environment_path = Path(path), Path(environment_path)
    raw = path.read_bytes()
    data = json.loads(raw)
    environment_raw = environment_path.read_bytes()
    environment = json.loads(environment_raw)
    if data.get('schema_version') != 1 or data.get('synthetic') is not True:
        raise ValueError('Expected synthetic inventory schema version 1')
    if environment.get('synthetic') is not True:
        raise ValueError('Inventory can link only to synthetic demo configuration')
    if not isinstance(data.get('environment'), str) or not data['environment'].strip():
        raise ValueError('Missing environment name')
    if data['environment'] != environment.get('environment'):
        raise ValueError('Environment names do not match')
    stamp = datetime.fromisoformat(data['captured_at'].replace('Z', '+00:00'))
    env_stamp = datetime.fromisoformat(environment['captured_at'].replace('Z', '+00:00'))
    if stamp.tzinfo is None or env_stamp.tzinfo is None or stamp != env_stamp:
        raise ValueError('Demo inventory and configuration must have matching timezone-aware timestamps')
    resources = {(item['resource_type'], item['resource_id']): position
                 for position, item in enumerate(environment['resources'])}
    if len(resources) != len(environment['resources']):
        raise ValueError('Duplicate resource identity in configuration')
    assets = data.get('assets')
    if not isinstance(assets, list) or not assets:
        raise ValueError('assets must be a nonempty list')
    chunks, seen = [], set()
    for position, asset in enumerate(assets):
        for field in ('resource_type', 'resource_id', 'purpose', 'lifecycle_status'):
            if not isinstance(asset.get(field), str) or not asset[field].strip():
                raise ValueError(f'Missing or invalid asset {field}')
        identity = (asset['resource_type'], asset['resource_id'])
        if identity in seen:
            raise ValueError(f'Duplicate asset: {identity}')
        seen.add(identity)
        if identity not in resources:
            raise ValueError(f'Unlinked asset: {identity}')
        if 'owner' not in asset or (asset['owner'] is not None and
                                   (not isinstance(asset['owner'], str) or not asset['owner'].strip())):
            raise ValueError('owner must be a nonempty string or explicit null')
        if 'privilege_level' not in asset:
            raise ValueError('Missing privilege_level')
        privilege = asset['privilege_level']
        if asset['resource_type'] == 'AWS IAM user':
            if privilege not in ('privileged', 'non_privileged', 'unknown'):
                raise ValueError('Invalid IAM privilege level')
        elif privilege is not None:
            raise ValueError('Non-account assets must use null privilege_level')
        owner = asset['owner'] if asset['owner'] is not None else 'unknown / not supplied'
        level = {'privileged': 'privileged administrator account',
                 'non_privileged': 'non-privileged standard account',
                 'unknown': 'unknown / not supplied', None: 'not applicable to this resource type'}[privilege]
        configuration_locator = f"/resources/{resources[identity]}"
        text = '\n'.join([
            f"SYNTHETIC DEMO INVENTORY | {asset['resource_type']} | {asset['resource_id']}",
            f"Environment: {data['environment']}; snapshot: {data['captured_at']}",
            f"Owner: {owner}", f"Purpose: {asset['purpose']}",
            f"Account privilege level: {level}",
            f"Lifecycle status: {asset['lifecycle_status']}",
            f"Linked configuration: {environment_path.name} {configuration_locator}",
            'Inventory declarations provide context, not proof of effective permissions, MFA, control applicability or compliance.'
        ])
        locator = f'/assets/{position}'
        chunk_id = 'inventory-' + digest(json.dumps([path.name, locator, text]).encode())
        chunks.append(dict(chunk_id=chunk_id, text=text, source=path.name,
                           page=None, type='evidence', doc_kind='asset_inventory', locator=locator))
    sources = {path.name: dict(sha256=digest(raw), synthetic=True,
                               environment=data['environment'], captured_at=data['captured_at'],
                               linked_configuration=environment_path.name,
                               linked_configuration_sha256=digest(environment_raw))}
    return chunks, sources


def main():
    chunks, sources = parse_inventory()
    output = ROOT / 'data/processed/inventory_bundle.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix('.tmp')
    temporary.write_text(json.dumps(dict(chunks=chunks, sources=sources), indent=2) + '\n')
    temporary.replace(output)
    print(f'Prepared {len(chunks)} synthetic inventory chunks with verified configuration links: {output}')


if __name__ == '__main__':
    main()
