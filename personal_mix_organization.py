"""Pure Personal Mixes display organisation; never controls mixer routing."""
from catalog_organization import reconcile_organization, validate_organization


def validate_channel_organizations(config):
    for provider in ('digico', 'dlive'):
        key = provider + '_channel_organization'
        value = config.get(key)
        if value is not None:
            if not isinstance(value, dict) or set(value) - {'folders', 'items'}:
                raise ValueError('Invalid channel organisation')
            folders, items = value.get('folders'), value.get('items')
            if not isinstance(folders, list) or not isinstance(items, list):
                raise ValueError('Channel organisation requires folders and items')
            for folder in folders:
                if (not isinstance(folder, dict) or not isinstance(folder.get('id'), str) or
                        not isinstance(folder.get('name'), str) or
                        any(ord(c) < 32 for c in folder['id'] + folder['name'])):
                    raise ValueError('Group IDs and names must be text')
            for item in items:
                if (not isinstance(item, dict) or not isinstance(item.get('id'), str) or
                        (item.get('folderId') is not None and not isinstance(item['folderId'], str))):
                    raise ValueError('Channel organisation IDs must be text')
            channels = config.get(provider + '_channels') or []
            ids = (range(1, len(channels) + 1) if provider == 'digico' else
                   [item['channel'] for item in channels])
            config[key] = validate_organization(value, ids)
    return config


def project_channels(channels, organization):
    """Canonical groups override legacy headings, including same-named groups.

    Discovery may add/remove physical inputs. Reconcile only the returned view;
    reads never persist a migration or change the configured organisation.
    """
    if organization is None:
        return channels
    tree = reconcile_organization(organization, [item['channel'] for item in channels])
    by_id = {str(item['channel']): item for item in channels}
    result = []
    for folder in [None] + tree['folders']:
        fid = folder['id'] if folder else None
        for item in tree['items']:
            if item['folderId'] == fid:
                result.append(dict(by_id[item['id']], groupId=fid,
                                   group=folder['name'] if folder else 'Ungrouped'))
    return result
