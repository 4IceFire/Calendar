"""Validate shared folder/display ordering without touching catalog identities."""
from __future__ import annotations


def validate_organization(value, item_ids):
    if not isinstance(value, dict):
        raise ValueError('Organisation must be an object')
    folders = value.get('folders', [])
    items = value.get('items', [])
    if not isinstance(folders, list) or not isinstance(items, list) or len(folders) > 500:
        raise ValueError('Invalid folders or items')
    normalized = []
    folder_ids = set()
    for index, folder in enumerate(folders):
        if not isinstance(folder, dict):
            raise ValueError('Invalid folder')
        fid = str(folder.get('id') or '').strip()
        name = str(folder.get('name') or '').strip()
        if not fid or len(fid) > 80 or fid in folder_ids or not name or len(name) > 120:
            raise ValueError('Folders require unique IDs and names of 1–120 characters')
        folder_ids.add(fid)
        normalized.append({'id': fid, 'name': name, 'order': index})
    allowed = {str(item) for item in item_ids}
    seen = set()
    ordered = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('Invalid item')
        iid = str(item.get('id'))
        fid = str(item.get('folderId') or '') or None
        if iid not in allowed or iid in seen or (fid is not None and fid not in folder_ids):
            raise ValueError('Unknown or duplicate item, or unknown folder')
        seen.add(iid)
        ordered.append({'id': iid, 'folderId': fid, 'order': len(ordered)})
    if seen != allowed:
        raise ValueError('The catalog changed. Reload before organising it.')
    return {'folders': normalized, 'items': ordered}


def reconcile_organization(value, item_ids):
    """New catalog members append at root; deleted members leave no dangling entry."""
    value = value if isinstance(value, dict) else {}
    ids = [str(item) for item in item_ids]
    folders = value.get('folders') or []
    valid_folders = {str(folder['id']) for folder in folders}
    seen = set()
    items = []
    for item in value.get('items') or []:
        iid = str(item.get('id'))
        if iid in ids and iid not in seen:
            seen.add(iid)
            fid = item.get('folderId')
            items.append({'id': iid, 'folderId': fid if fid in valid_folders else None})
    items.extend({'id': iid, 'folderId': None} for iid in ids if iid not in seen)
    return validate_organization({'folders': folders, 'items': items}, ids)
