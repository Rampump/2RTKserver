# -*- coding: utf-8 -*-
import serial, time

s = serial.Serial('COM3', 115200, timeout=1.0)
print('opened', flush=True)
time.sleep(0.5)
s.reset_input_buffer()
print('reading 2s...', flush=True)
data = b''
end = time.time() + 2.0
while time.time() < end:
    chunk = s.read(256)
    if chunk:
        data += chunk
s.close()

print(f'收到 {len(data)} 字节', flush=True)
if len(data) == 0:
    print('NO DATA')
else:
    print('--- HEX dump 前 512 字节 ---', flush=True)
    for i in range(0, min(len(data), 512), 16):
        hexpart = ' '.join(f'{b:02X}' for b in data[i:i+16])
        ascpart = ''.join(chr(b) if 32 <= b < 127 else '.' for b in data[i:i+16])
        print(f'{i:04X}: {hexpart:<48s}  {ascpart}', flush=True)
    print('\n--- 所有 $ 开头的行 ---', flush=True)
    text = data.decode('ascii', 'replace')
    for line in text.split('\r\n'):
        if '$' in line:
            idx = line.index('$')
            print(f'  {line[idx:]}', flush=True)
