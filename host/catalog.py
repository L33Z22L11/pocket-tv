"""Persistent TUI preferences and last-good playlist; no device credentials here."""
import hashlib
import json
from pathlib import Path
from dataclasses import asdict
from sources import Source, load


def channel_key(source):
    return hashlib.sha256((source.name+'\n'+source.url).encode()).hexdigest()


class Catalog:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.favorites = set(self._read('favorites.json', []))

    def _read(self, name, default):
        try:
            return json.loads((self.folder/name).read_text())
        except (OSError, ValueError):
            return default

    def refresh(self, source):
        cache = hashlib.sha256(str(source).encode()).hexdigest()+'.json'
        try:
            channels = load(source)
            if not channels:
                raise ValueError('频道列表为空')
            temporary = self.folder/(cache+'.tmp')
            temporary.write_text(json.dumps([asdict(c) for c in channels],ensure_ascii=False))
            temporary.replace(self.folder/cache)
            return channels, False
        except (OSError, ValueError):
            cached = self._read(cache, [])
            if not cached:
                raise
            return [Source(**c) for c in cached], True

    def toggle(self, source):
        key = channel_key(source)
        if key in self.favorites:
            self.favorites.remove(key)
        else:
            self.favorites.add(key)
        temporary = self.folder/'favorites.tmp'
        temporary.write_text(json.dumps(sorted(self.favorites)))
        temporary.replace(self.folder/'favorites.json')

    def filter(self, channels, query='', group='', favorites=False):
        terms = query.casefold().split()
        return [i for i,c in enumerate(channels)
                if (not group or c.group == group)
                and (not favorites or channel_key(c) in self.favorites)
                and all(t in (c.name+' '+c.group).casefold() for t in terms)]
