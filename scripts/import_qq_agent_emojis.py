"""Import the bundled emoji offline, preserving existing tags and usage."""
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Dict

import hashlib
import json
import re
import shutil
import sqlite3


def import_bundle(root: Path, database: Path, bundle: Path) -> Dict[str, int]:
    metadata = json.loads((bundle / 'metadata.json').read_text(encoding='utf-8'))
    sources = {}
    for file_hash in metadata:
        files = list(bundle.glob(file_hash + '.*'))
        if len(files) != 1 or hashlib.sha256(files[0].read_bytes()).hexdigest() != file_hash:
            raise ValueError(f'Invalid image or missing file; fetch the complete resource directory: {file_hash}')
        sources[file_hash] = files[0]
    if not database.is_file():
        raise FileNotFoundError('Start MaiBot once to initialize the database before importing')
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    backup = database.with_name(database.name + '.emoji-backup-' + stamp)
    added = merged = 0
    try:
        with closing(sqlite3.connect(backup)) as target:
            connection.backup(target)
        connection.execute('BEGIN IMMEDIATE')
        now = datetime.now().isoformat(sep=' ')
        for file_hash, entry in metadata.items():
            rows = connection.execute("SELECT * FROM images WHERE image_hash=? AND image_type='EMOJI'", (file_hash,)).fetchall()
            if len(rows) > 1:
                raise ValueError(f'Duplicate database hash: {file_hash}')
            old = rows[0] if rows else None
            values = ([old['description']] if old else []) + entry['sourceTags'] + entry['keywords']
            tags = []
            seen = set()
            for value in values:
                for tag in re.split(r'[,，、;；\r\n\t]+', value):
                    tag = tag.strip()
                    if tag and tag.casefold() not in seen:
                        tags.append(tag)
                        seen.add(tag.casefold())
            source = sources[file_hash]
            path = root / old['full_path'] if old else root / 'data/emoji' / source.name
            if not path.resolve().is_relative_to(root.resolve()):
                raise ValueError('Image path outside project')
            if not path.is_file():
                path = root / 'data/emoji' / source.name
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, path)
            if hashlib.sha256(path.read_bytes()).hexdigest() != file_hash:
                raise ValueError(f'Existing image hash mismatch: {file_hash}')
            relative = path.relative_to(root).as_posix()
            if old:
                # Keep user bans, counters and timestamps while merging tags.
                connection.execute('UPDATE images SET description=?,full_path=?,is_registered=1,no_file_flag=0,register_time=COALESCE(register_time,?) WHERE id=?', (','.join(tags),relative,now,old['id']))
                merged += 1
            else:
                connection.execute("INSERT INTO images (image_hash,description,full_path,image_type,query_count,is_registered,is_banned,no_file_flag,record_time,register_time,last_used_time,vlm_processed) VALUES (?,?,?,'EMOJI',0,1,0,0,?,?,NULL,0)", (file_hash,','.join(tags),relative,now,now))
                added += 1
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {'added': added, 'merged': merged}


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    print(import_bundle(root, root / 'data/MaiBot.db', root / 'resources/qq-agent/emoji'))
