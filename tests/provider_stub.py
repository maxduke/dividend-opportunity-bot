"""Importable offline providers executed in disposable child processes."""
import os
import time
from pathlib import Path


def hang(marker):
    Path(marker).write_text(str(os.getpid()))
    while True:
        time.sleep(1)


def echo(value):
    print('provider diagnostic output')
    return value


def fail():
    raise ConnectionError('https://example.invalid/?token=secret')
