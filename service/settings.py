from functools import lru_cache
from pathlib import Path

import yaml


def secret(name: str) -> str:
    return (Path('/run/secrets') / name).read_text().strip()


@lru_cache
def config() -> dict:
    return yaml.safe_load(Path('/app/config.yaml').read_text())


def database_url() -> str:
    from sqlalchemy.engine import make_url
    return make_url(config()['database_url']).set(password=secret('database_password')).render_as_string(hide_password=False)
