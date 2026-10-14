"""Optional QQ-Agent emoji download. No download or registration without explicit flags."""
from pathlib import Path
from typing import Dict
from urllib.request import urlopen

import argparse
import hashlib
import json
import os
import tempfile


def download_file(url: str, destination: Path, expected_hash: str, expected_size: int) -> bool:
    """Resume at file granularity; validate before atomically replacing any destination."""
    if destination.is_file() and hashlib.sha256(destination.read_bytes()).hexdigest() == expected_hash:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix='.download', delete=False) as output:
            temporary = Path(output.name)
            with urlopen(url, timeout=60) as response:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > expected_size:
                        raise ValueError('Download exceeds expected size')
                    digest.update(chunk)
                    output.write(chunk)
        if size != expected_size or digest.hexdigest() != expected_hash:
            raise ValueError(f'Download checksum/size mismatch: {destination.name}')
        os.replace(temporary, destination)
        temporary = None
        return True
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def download_bundle(bundle: Path) -> Dict[str, int]:
    manifest = json.loads((bundle / 'manifest.json').read_text(encoding='utf-8'))
    revision = manifest['revision']
    if len(revision) != 40 or any(c not in '0123456789abcdef' for c in revision):
        raise ValueError('Invalid resource revision')
    downloaded = reused = 0
    for item in manifest['files']:
        name = item['name']
        if Path(name).name != name or name != item['sha256'] + Path(name).suffix:
            raise ValueError('Invalid resource filename')
        url = f'https://raw.githubusercontent.com/Xinze-Joey-Li/MaiBot/{revision}/resources/qq-agent/emoji/{name}'
        if download_file(url, bundle / name, item['sha256'], item['bytes']):
            downloaded += 1
        else:
            reused += 1
    return {'downloaded': downloaded, 'reused': reused}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--download', action='store_true', help='Download the optional 148-image pack (497 MiB)')
    parser.add_argument('--register', action='store_true', help='Register downloaded images; stop MaiBot first')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    bundle = root / 'resources/qq-agent/emoji'
    if args.download:
        print(download_bundle(bundle))
    if args.register:
        # Import only on explicit request; execution as a script puts scripts/ on sys.path.
        from import_qq_agent_emojis import import_bundle
        print(import_bundle(root, root / 'data/MaiBot.db', bundle))
    if not args.download and not args.register:
        manifest = json.loads((bundle / 'manifest.json').read_text(encoding='utf-8'))
        present = sum((bundle / item['name']).is_file() for item in manifest['files'])
        print(f"Optional emoji pack: {present}/{len(manifest['files'])} files present; no files downloaded. Use --download, optionally --register.")


if __name__ == '__main__':
    main()
