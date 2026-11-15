from pathlib import Path
import os

def data_dir() -> Path:
    p = Path(os.environ.get("GRAPHIFY_HOME", Path.home() / ".graphify-platform")).expanduser().resolve()
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    p.chmod(0o700)
    for name in ("graphs", "staging", "logs"):
        child=p/name;child.mkdir(exist_ok=True, mode=0o700);child.chmod(0o700)
    return p

def db_path() -> Path:
    return data_dir() / "platform.sqlite3"
