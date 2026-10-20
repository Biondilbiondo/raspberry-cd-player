import configparser
import logging

log = logging.getLogger("database")

config = configparser.ConfigParser()
config.read('config.ini')

DATABASE_FILE = config.get('storage', 'database_file')

def db_add_entry(vault_key, ntracks, track, path):
    with open(DATABASE_FILE, 'a') as f:
        print(f"{vault_key:s} {ntracks:02d} {track:02d} {path:s}", file=f)

def _parse_db_line(l):
    res = {}
    tok = l.split()
    res['vault_key'] = tok[0]
    res['ntracks'] = int(tok[1])
    res['track'] = int(tok[2])
    res['path'] = ' '.join(tok[3:])
    return res

def db_get_track(vault_key, track):
    with open(DATABASE_FILE, 'r') as f:
        for l in f:
            item = _parse_db_line(l)
            if item['vault_key'] == vault_key and item['track'] == track:
                return item
    return None

def db_is_cd_present(vault_key):
    with open(DATABASE_FILE, 'r') as f:
        for l in f:
            item = _parse_db_line(l)
            if item['vault_key'] == vault_key:
                return True
    return False

def db_is_cd_complete(vault_key):
    ntracks = None
    track_cnt = 0
    with open(DATABASE_FILE, 'r') as f:
        for l in f:
            item = _parse_db_line(l)
            if item['vault_key'] == vault_key:
                track_cnt += 1
                ntracks = item['ntracks']

    if ntracks is not None and ntracks == track_cnt:
        return True
    
    return False
