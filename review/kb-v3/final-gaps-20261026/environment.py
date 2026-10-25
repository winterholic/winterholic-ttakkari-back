"""Review-only runtime. Own Run DB, public JWT, real pinned E5, no operational settings."""
import importlib.util
import os
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
CORPUS = Path('/Users/winterholic/development/testing-assets/kb-v3-corpus-20261026.json')
E5_CACHE = Path('/Users/winterholic/development/testing-assets/kb-v3-e5')
API_URL = 'http://127.0.0.1:8819'

def setup():
    url = os.environ.get('TTAKKARI_TEST_DB', '')
    run = os.environ.get('TTAKKARI_RUN_ID', '')
    parsed = urllib.parse.urlsplit(url)
    if not run or (parsed.hostname, parsed.path.lstrip('/'), parsed.username) != ('127.0.0.1','ttakkari_test','ttk_test'):
        raise RuntimeError('unexpected_own_run_database')
    markers=[]
    for marker in Path('/tmp').glob('ttakkari-run-test-*/run-test-cluster.marker'):
        values=dict(line.split('=',1) for line in marker.read_text().splitlines() if '=' in line)
        if values.get('run_id')==run and values.get('port')==str(parsed.port) and values.get('folder')==marker.parent.name:
            markers.append(marker)
    if len(markers)!=1:
        raise RuntimeError('run_database_marker_ownership_mismatch')
    spec2 = importlib.util.spec_from_file_location('live_probe', REPO.parent / 'front-kb-v3/scripts/kb-live/serve_backend.py')
    live = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(live)
    base = HERE / '.runtime'
    live.configure_env(url, 'http://127.0.0.1:5199', base)
    models = base / 'home/models'
    if not models.exists():
        models.symlink_to(E5_CACHE, target_is_directory=True)
    os.environ.update(TTAKKARI_CONTEXT_EMBEDDER='e5', TTAKKARI_CONTEXT_EMBEDDING_CPU_THREADS='2',
                      TTAKKARI_CONTEXT_EMBEDDING_TIMEOUT_SECONDS='300', TTAKKARI_NO_ENV_FILE='1')
    from ttakkari.config import get_settings
    get_settings.cache_clear()
    assert get_settings().context_embedder == 'e5'
    assert get_settings().kb_embedding_snapshot == 'ac6781cd1cf88b8306a536d7c9d18a5bd57cc14b'
    return url
