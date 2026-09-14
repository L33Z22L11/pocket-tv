"""Local media, HLS URLs and extended M3U channel lists."""
from dataclasses import dataclass, field
from pathlib import Path
import re
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

PRESETS = {
    'iptv-org': 'https://iptv-org.github.io/iptv/index.m3u',
    'china': 'https://iptv-org.github.io/iptv/countries/cn.m3u',
    'free-tv': 'https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8',
}
EXTENSIONS = {'.mp4', '.mov', '.mkv', '.webm', '.avi', '.ts', '.m4v', '.gif'}

@dataclass
class Source:
    name: str
    url: str
    group: str = ''
    headers: dict = field(default_factory=dict)


def remote(value):
    return urlparse(str(value)).scheme in ('http', 'https')


def read_text(value):
    if remote(value):
        with urlopen(Request(value, headers={'User-Agent': 'Pocket-TV/1.0'}), timeout=15) as response:
            data = response.read(8 * 1024 * 1024 + 1)
    else:
        data = Path(value).read_bytes()
    if len(data) > 8 * 1024 * 1024:
        raise ValueError('Playlist exceeds 8 MiB')
    return data.decode('utf-8-sig')


def parse_playlist(text, origin):
    lines = [line.strip() for line in text.lstrip('\ufeff').splitlines()]
    if any(line.startswith('#EXT-X-') for line in lines):
        return [Source(Path(urlparse(origin).path).name or 'HLS', origin)]
    result, name, group, headers = [], '', '', {}
    for line in lines:
        if line.startswith('#EXTINF:'):
            match = re.match(r'#EXTINF:(?:[^,"\n]|"[^"]*")*,(.*)', line)
            name = match.group(1).strip() if match else ''
            match = re.search(r'group-title="([^"]*)"', line)
            group = match.group(1) if match else ''
        elif line.startswith('#EXTVLCOPT:'):
            key, _, value = line[11:].partition('=')
            if key in ('http-user-agent', 'http-referrer'):
                headers[{'http-user-agent': 'User-Agent', 'http-referrer': 'Referer'}[key]] = value
        elif line and not line.startswith('#'):
            url = urljoin(origin, line) if remote(origin) else (line if urlparse(line).scheme else str((Path(origin).parent / line).resolve()))
            result.append(Source(name or Path(urlparse(url).path).name or url, url, group, headers))
            name, group, headers = '', '', {}
    return result


def load(value):
    value = str(value)
    if not remote(value) and Path(value).is_dir():
        return [Source(p.name, str(p.resolve())) for p in sorted(Path(value).iterdir()) if p.suffix.lower() in EXTENSIONS]
    if Path(urlparse(value).path).suffix.lower() in ('.m3u', '.m3u8'):
        return parse_playlist(read_text(value), value)
    if not remote(value) and not Path(value).is_file():
        raise FileNotFoundError(value)
    return [Source(Path(urlparse(value).path).name or value, value)]
