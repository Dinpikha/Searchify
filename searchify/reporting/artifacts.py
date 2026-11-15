"""Private append-only metadata artifacts for explicitly requested plan saving."""
import json
import os
from pathlib import Path
import tempfile
import uuid


def save_artifact(directory,prefix,data):
    directory=Path(directory).expanduser(); directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    path=directory/(prefix+'-'+str(uuid.uuid4())+'.json')
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',dir=directory,prefix='.',suffix='.tmp',delete=False,encoding='utf-8') as target:
            temporary=target.name; json.dump(data,target,indent=2,ensure_ascii=True); target.write('\n'); target.flush(); os.fsync(target.fileno())
        os.link(temporary,path)
    finally:
        if temporary is not None: os.unlink(temporary)
    return path
