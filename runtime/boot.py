"""Non-root dedicated entry point; Docker --init reaps subprocesses."""
import os
import json
import shutil
from pathlib import Path


home = Path(os.environ['HERMES_HOME'])
plugin = home / 'plugins' / 'central-auth'
plugin.mkdir(parents=True, exist_ok=True)
shutil.copy('/service/runtime/provider.py', plugin / '__init__.py')
shutil.copy('/service/runtime/plugin.yaml', plugin / 'plugin.yaml')
config_path = home / 'config.yaml'
if not config_path.exists():
    cfg = {
        'model': {'provider': 'custom', 'default': 'central-default',
                  'base_url': 'http://control:8000/v1', 'key_env': 'RUNTIME_TOKEN'},
        'dashboard': {'public_url': 'http://hc-user-' + os.environ['OWNER_ID'] + ':9119'},
        'plugins': {'enabled': ['central-auth']},
        'terminal': {'backend': 'local', 'cwd': str(home / 'workspace')},
        'browser': {'backend': 'local'},
    }
    config_path.write_text(json.dumps(cfg))
# Bind the main model to the centrally selected alias on process startup only.
from ruamel.yaml import YAML
cfg = YAML(typ='safe').load(config_path.read_text()) or {}
cfg['model'] = {'provider': 'custom', 'default': 'central-default', 'base_url': 'http://control:8000/v1', 'key_env': 'RUNTIME_TOKEN'}
config_path.write_text(json.dumps(cfg))
(home / 'workspace').mkdir(exist_ok=True)
os.execv('/opt/hermes/bin/hermes', ['hermes', 'dashboard', '--host', '0.0.0.0', '--port', '9119', '--no-open'])
