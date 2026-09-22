"""SignCollect install-root resolver for Python scripts.

VENDORED FILE - byte-identical in every consumer repository, and identical to
consumer/sc_paths.py in signlab_signcollect-lib, which is the original. Do not
edit one copy; edit the library's and re-copy. Put the copy next to the
script that imports it (Python puts the script's directory on sys.path).

    from sc_paths import sc_path, sc_dir, sc_setting
    sc_path('media', 'studioFiles', 'takes')  # '/web/gebarenoverleg_media/studioFiles/takes'
    sc_dir('media_fbx')                       # '/web/gebarenoverleg_media/fbx/'
    sc_setting('SOME_KEY', 'default')         # environment, then env file, then default

Same resolution order as paths.php, first hit wins:
  1. SC_WEB_ROOT environment variable
  2. SC_WEB_ROOT= in the env file: $SC_ENV_FILE, else /web/.env
  3. /web

The default is /web and must stay /web: with nothing configured every call
returns the exact string the literal it replaced contained.
"""

import os
import sys

_DEFAULT_ROOT = '/web'

# Same four names as paths.php's sc_locations(); values are relative to the root.
_LOCATIONS = {
    'media': 'gebarenoverleg_media',
    'media_raw': 'gebarenoverleg_media/studioFilesMini/raw',
    'media_post': 'gebarenoverleg_media/studioFilesMini/post',
    'media_fbx': 'gebarenoverleg_media/fbx',
}

_env_cache = None
_root_cache = None


def sc_env_file():
    """Path of the env file: $SC_ENV_FILE, else /web/.env."""
    return os.environ.get('SC_ENV_FILE') or _DEFAULT_ROOT + '/.env'


def sc_env():
    """The env file as a dict ({} if missing or unreadable). KEY=value per
    line, # comments, optional surrounding quotes - same subset as db_config.php."""
    global _env_cache
    if _env_cache is None:
        values = {}
        try:
            with open(sc_env_file(), encoding='utf-8') as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line[0] == '#' or '=' not in line:
                        continue
                    key, value = line.split('=', 1)
                    values[key.strip()] = value.strip().strip('"\'')
        except OSError:
            pass
        _env_cache = values
    return _env_cache


def sc_setting(key, default=None):
    """A config value: environment variable, then env file, then default."""
    value = os.environ.get(key)
    if value:
        return value
    value = sc_env().get(key)
    if value:
        return value
    return default


def sc_root():
    """The install root, no trailing slash."""
    global _root_cache
    if _root_cache is None:
        candidate = sc_setting('SC_WEB_ROOT', _DEFAULT_ROOT)
        if not candidate.startswith('/'):
            sys.stderr.write("SignCollect config: SC_WEB_ROOT '%s' is not absolute; using /web\n" % candidate)
            candidate = _DEFAULT_ROOT
        _root_cache = '' if candidate == '/' else candidate.rstrip('/')
    return _root_cache


def sc_path(*parts):
    """Absolute path below the root, no trailing slash. A bare first segment
    (no '/' or '.') is looked up in the named locations."""
    parts = list(parts)
    if parts and '/' not in parts[0] and '.' not in parts[0]:
        parts[0] = _LOCATIONS.get(parts[0], parts[0])
    path = sc_root()
    for part in parts:
        part = part.strip('/')
        if part and part != '.':
            path += '/' + part
    return path


def sc_dir(*parts):
    """sc_path() with exactly one trailing slash."""
    return sc_path(*parts) + '/'
