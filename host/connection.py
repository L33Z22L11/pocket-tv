"""Find the same physical USB device after its serial port re-enumerates."""
from serial.tools import list_ports

class DeviceLocator:
    def __init__(self, port=None):
        self.port = port
        self.serial_number = None
        self.location = None

    def resolve(self):
        devices = [p for p in list_ports.comports() if p.vid == 0x303a and p.pid == 0x1001]
        if self.serial_number:
            matches = [p for p in devices if p.serial_number == self.serial_number]
        elif self.location:
            matches = [p for p in devices if p.location == self.location]
        elif self.port:
            matches = [p for p in devices if p.device == self.port]
        else:
            matches = devices
        if len(matches) > 1:
            raise ValueError('找到多个 ESP32-C3，请用 --port 指定设备')
        if not matches:
            raise FileNotFoundError('等待原设备 USB 重新连接')
        selected = matches[0]
        self.serial_number = selected.serial_number or self.serial_number
        self.location = selected.location or self.location
        self.port = selected.device
        return self.port
