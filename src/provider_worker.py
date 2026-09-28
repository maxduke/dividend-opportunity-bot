"""One trusted local provider invocation. stdin/stdout are a private IPC channel."""

import contextlib
import importlib
import pickle
import sys


def main():
    output = sys.stdout.buffer
    module, qualified_name, args, kwargs, proxy_active = pickle.loads(sys.stdin.buffer.read())
    try:
        # Provider libraries may print; keep stdout reserved for the result.
        with contextlib.redirect_stdout(sys.stderr):
            if proxy_active:
                from .provider_bootstrap import install_verified_proxy_patch
                install_verified_proxy_patch()
            function = importlib.import_module(module)
            for part in qualified_name.split('.'):
                function = getattr(function, part)
            result = function(*args, **kwargs)
        response = (True, result)
    except Exception as exc:
        # Provider exception strings can contain authenticated request URLs.
        response = (False, type(exc).__name__)
    output.write(pickle.dumps(response))
    output.flush()


if __name__ == '__main__':
    main()
