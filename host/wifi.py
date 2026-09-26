"""USB Wi-Fi provisioning and authenticated LAN discovery. No passwords in logs."""
import argparse
import getpass
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import struct
import time
from connection import DeviceLocator
from transport import Link, STATUS, WIFI_CONFIG, WIFI_CLEAR, WIFI_SCAN, WIFI_SCAN_RESULT, ROOT, instance_lock

PROFILE = Path(os.environ.get('POCKET_TV_CONFIG', Path.home()/'.config/pocket-tv/device.json'))


def load_profile(path=PROFILE):
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    if len(data.get('device_id', '')) != 12 or len(bytes.fromhex(data.get('key', ''))) != 32:
        raise ValueError('Invalid Pocket TV pairing profile; provision again over USB')
    return data


def save_profile(data, path=PROFILE):
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.device-', dir=path.parent)
    try:
        if os.name != 'nt':
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as out:
            json.dump(data, out)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def provision_payload(ssid, password, key):
    ssid, password = ssid.encode('utf-8'), password.encode('utf-8')
    if b'\0' in ssid or b'\0' in password:
        raise ValueError('SSID/password cannot contain NUL')
    if not 1 <= len(ssid) <= 32:
        raise ValueError('SSID must be 1–32 UTF-8 bytes')
    if password and not 8 <= len(password) <= 63:
        raise ValueError('Wi-Fi password must be 8–63 UTF-8 bytes, or empty for an open network')
    return struct.pack('BB32s64s32s', len(ssid), len(password), ssid, password, key)


def discover(profile, timeout=.3):
    request = b'PTV6FIND'+profile['device_id'].encode('ascii')+secrets.token_bytes(12)
    expected = request+hmac.digest(bytes.fromhex(profile['key']), request, 'sha256')
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(timeout)
        sock.sendto(request, ('255.255.255.255', 5761))
        end = time.monotonic()+timeout
        while time.monotonic() < end:
            sock.settimeout(max(.001, end-time.monotonic()))
            try:
                reply, address = sock.recvfrom(128)
            except socket.timeout:
                break
            if hmac.compare_digest(reply, expected):
                return address[0]
    return None


def configure(ssid, password, port=None):
    """Shared by CLI and TUI; keeps the player/USB lock for the whole transaction."""
    locator = DeviceLocator(port)
    with instance_lock(ROOT/'logs/player.lock'):
        link = Link(locator.resolve())
        try:
            state = link.request(STATUS)
            if state.get('protocol') != 'PTV6':
                raise RuntimeError('请先安装支持 PTV6 的 Wi-Fi 固件')
            key = secrets.token_bytes(32)
            payload = provision_payload(ssid, password, key)
            profile = dict(device_id=state['device_id'], key=key.hex(), usb_serial=locator.serial_number, host='')
            save_profile(profile)
            link.request(WIFI_CONFIG, payload=payload)
            deadline = time.monotonic()+30
            while time.monotonic() < deadline:
                state = link.request(STATUS)
                if state['wifi_ip'] != '0.0.0.0':
                    profile['host'] = state['wifi_ip']
                    save_profile(profile)
                    return state['wifi_ip']
                time.sleep(.5)
            raise RuntimeError(f"未获得 IP，Wi-Fi 状态码 {state['wifi_error']}；请检查 2.4 GHz 网络和密码")
        finally:
            link.close()


def scan_networks(port=None):
    """Scan from the device's own 2.4 GHz radio, including when unprovisioned."""
    locator=DeviceLocator(port)
    with instance_lock(ROOT/'logs/player.lock'):
        link=Link(locator.resolve())
        try:
            link.request(WIFI_SCAN)
            deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                state=link.request(WIFI_SCAN_RESULT)
                if state.get('scan_done'):break
                time.sleep(.2)
            else:raise TimeoutError('扫描超时，请重新扫描或手动输入')
            networks={}
            for index in range(state['scan_count']):
                row=state if index==0 else link.request(WIFI_SCAN_RESULT,argument=index)
                ssid=bytes.fromhex(row['scan_ssid_hex']).decode('utf-8',errors='replace')
                if not ssid or not 1<=row['scan_channel']<=14:continue
                network=dict(ssid=ssid,rssi=row['scan_rssi'],channel=row['scan_channel'],secure=row['scan_auth']!=0)
                if ssid not in networks or network['rssi']>networks[ssid]['rssi']:networks[ssid]=network
            return sorted(networks.values(),key=lambda n:n['rssi'],reverse=True)
        finally:link.close()


def main():
    parser = argparse.ArgumentParser(description='通过 USB 配置 Pocket TV 的 2.4 GHz Wi-Fi')
    parser.add_argument('--port')
    parser.add_argument('--ssid', help='不指定则交互输入；密码始终隐藏输入')
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--forget', action='store_true', help='清除设备 Wi-Fi/配对密钥和本机配对文件')
    args = parser.parse_args()
    if not args.status and not args.forget:
        ssid = args.ssid if args.ssid is not None else input('2.4 GHz Wi-Fi 名称 (SSID)：').strip()
        password = getpass.getpass('Wi-Fi 密码（开放网络直接回车）：')
        address = configure(ssid, password, args.port)
        print(f'Wi-Fi 已连接：{address}；播放器将优先 USB，断开后自动转 Wi-Fi。')
        return
    locator = DeviceLocator(args.port)
    with instance_lock(ROOT/'logs/player.lock'):
        link = Link(locator.resolve())
        try:
            state = link.request(STATUS)
            if state.get('protocol') != 'PTV6':
                raise RuntimeError('请先安装支持 PTV6 的 Wi-Fi 固件')
            if args.status:
                print(json.dumps({k: state[k] for k in ('device_id', 'wifi_ip', 'wifi_error')}, ensure_ascii=False))
                return
            if args.forget:
                link.request(WIFI_CLEAR)
                old = load_profile()
                if old and old['device_id'] == state['device_id']:
                    PROFILE.unlink(missing_ok=True)
                print('已清除设备 Wi-Fi 和配对信息')
                return
        finally:
            link.close()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        raise SystemExit(f'Pocket TV Wi-Fi: {exc}')

