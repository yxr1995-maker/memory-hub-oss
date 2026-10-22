#!/usr/bin/env python3
"""Bounded scheduled entry point for native capture and durable learning."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

HUB = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get('MEMORY_HUB_DATA', str(Path.home()/'.memory-hub')))
PYTHON = Path(sys.executable)


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    result = {'started_at': datetime.now(timezone.utc).isoformat(), 'stages': []}
    env = dict(os.environ)
    env.setdefault('OPENCODEX_URL', 'http://127.0.0.1:3425/v1')
    env.setdefault('CLAUDE_MEM_MODEL', 'group/route')
    commands = [
        ('capture', [str(PYTHON), str(HUB/'scripts/hermes_capture.py'), '--once']),
        ('learning', [str(PYTHON), str(HUB/'scripts/learning_loop.py'), '--once',
                      '--source-file', str(HUB/'staging/observations-hermes-native.jsonl'),
                      '--source-file', str(HUB/'staging/observations-hermes.jsonl'),
                      '--source-file', str(Path.home()/'Documents/memory-hub/staging/observations-hermes.jsonl')]),
    ]
    status = 0
    for stage, command in commands:
        try:
            proc = subprocess.run(command, cwd=HUB, env=env, capture_output=True, text=True,
                                  timeout=45 if stage == 'capture' else 180)
            try:
                detail = json.loads(proc.stdout)
            except ValueError:
                detail = {'result': 'invalid_receipt'}
            result['stages'].append({'stage': stage, 'returncode': proc.returncode, 'receipt': detail})
            if proc.returncode or detail.get('result') == 'invalid_receipt':
                status = proc.returncode or 1
                break
        except subprocess.TimeoutExpired:
            result['stages'].append({'stage': stage, 'result': 'timeout'})
            status = 124
            break
        except OSError as exc:
            result['stages'].append({'stage': stage, 'result': 'failed', 'error': type(exc).__name__})
            status = 1
            break
    result.update(finished_at=datetime.now(timezone.utc).isoformat(), exit_code=status)
    fd, name = tempfile.mkstemp(prefix='.learning-schedule-', dir=DATA)
    with os.fdopen(fd, 'w') as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(name, DATA/'learning-schedule-state.json')
    print(json.dumps(result, ensure_ascii=False))
    return status


if __name__ == '__main__':
    sys.exit(main())
