from contextlib import closing

import hashlib
import json
import sqlite3

import pytest

from scripts.import_qq_agent_emojis import import_bundle


def test_offline_import_preserves_usage_and_merges(tmp_path):
    root = tmp_path
    (root / 'data').mkdir()
    bundle = root / 'bundle'
    bundle.mkdir()
    raw = b'test'
    file_hash = hashlib.sha256(raw).hexdigest()
    image = bundle / (file_hash + '.png')
    image.write_bytes(raw)
    (bundle / 'metadata.json').write_text(json.dumps({file_hash: {'keywords': ['new'], 'sourceTags': ['old', 'new']}}))
    database = root / 'data/MaiBot.db'
    with closing(sqlite3.connect(database)) as connection:
        connection.execute('CREATE TABLE images(id INTEGER PRIMARY KEY,image_hash TEXT,description TEXT,full_path TEXT,image_type TEXT,query_count INTEGER,is_registered INTEGER,is_banned INTEGER,no_file_flag INTEGER,record_time TEXT,register_time TEXT,last_used_time TEXT,vlm_processed INTEGER)')
        connection.commit()
    assert import_bundle(root, database, bundle) == {'added': 1, 'merged': 0}
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("UPDATE images SET description='original,old',query_count=7,is_banned=1")
        connection.commit()
    assert import_bundle(root, database, bundle) == {'added': 0, 'merged': 1}
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute('SELECT description,query_count,is_banned FROM images').fetchone() == ('original,old,new', 7, 1)
    image.write_bytes(b'broken')
    with pytest.raises(ValueError, match='Invalid image'):
        import_bundle(root, database, bundle)
