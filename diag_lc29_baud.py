# -*- coding: utf-8 -*-
"""LC29H(BS) 波特率扫描：找哪个档能看到可读的 NMEA 文本。"""
import serial, time

PORT = 'COM3'
BAUDS = [115200, 9600, 38400, 57600, 19200, 4800, 230400, 460800, 921600]

for baud in BAUDS:
    try:
        s = serial.Serial(PORT, baud, timeout=0.3)
        time.sleep(0.2)
        s.reset_input_buffer()
        time.sleep(0.8)
        data = s.read(s.in_waiting or 4096)
        s.close()
        # 检查有多少可打印 ASCII 字符
        printable = sum(1 for b in data if 32 <= b < 127 or b in (10, 13, 36))
        ratio = printable / max(len(data), 1)
        has_dollar = b'$' in data
        has_pqtm = b'PQTM' in data or b'GGA' in data or b'GNGGA' in data
        marker = ' <<<' if (ratio > 0.7 and has_dollar) else ''
        print(f'{baud:>8} bps: {len(data):>5} bytes, printable={ratio:.0%}, $={has_dollar}, PQTM/GGA={has_pqtm}{marker}')
        if ratio > 0.7 and has_dollar:
            # 打印前几行
            for line in data.split(b'\r\n'):
                if line.strip() and (b'$' in line or b'PQTM' in line):
                    print(f'    {line.decode("ascii", "replace")}')
    except Exception as e:
        print(f'{baud:>8} bps: 打开失败 {e}')
