# -*- coding: utf-8 -*-
"""LC29H(BS) 裸串口诊断：直接发 PQTMVERNO 看模块回什么。"""
import serial, time, sys

PORT = 'COM3'
BAUD = 115200

def checksum(body):
    cs = 0
    for b in body.encode('ascii'):
        cs ^= b
    return f'{cs:02X}'

s = serial.Serial(PORT, BAUD, timeout=0.5)
print(f'=== {PORT} @ {BAUD} ===')

# 先被动看 1 秒模块在吐什么
time.sleep(0.3)
s.reset_input_buffer()
time.sleep(1.0)
passive = s.read(s.in_waiting or 4096)
print(f'[被动 1s] 收到 {len(passive)} 字节')
for line in passive.split(b'\r\n'):
    if line.strip():
        print(f'  {line.decode("ascii", "replace")}')

# 发 PQTMVERNO
cmd = f'PQTMVERNO'
frame = f'${cmd}*{checksum(cmd)}\r\n'
print(f'\n[发送] {frame.strip()}')
s.write(frame.encode('ascii'))
s.flush()

# 等 2 秒，把所有回来的字节打出来
end = time.time() + 2.0
buf = b''
while time.time() < end:
    chunk = s.read(256)
    if chunk:
        buf += chunk
    else:
        time.sleep(0.02)

print(f'\n[PQTMVERNO 后 2s] 收到 {len(buf)} 字节')
for line in buf.split(b'\r\n'):
    if line.strip():
        print(f'  {line.decode("ascii", "replace")}')

# 再发一次 PQTMSN（LG290P 有，LC29HBS 文档说没有）
cmd2 = 'PQTMSN'
frame2 = f'${cmd2}*{checksum(cmd2)}\r\n'
print(f'\n[发送] {frame2.strip()}')
s.write(frame2.encode('ascii'))
s.flush()
end = time.time() + 1.5
buf2 = b''
while time.time() < end:
    chunk = s.read(256)
    if chunk:
        buf2 += chunk
    else:
        time.sleep(0.02)
print(f'\n[PQTMSN 后 1.5s] 收到 {len(buf2)} 字节')
for line in buf2.split(b'\r\n'):
    if line.strip():
        print(f'  {line.decode("ascii", "replace")}')

# 再发 PQTMUNIQID
cmd3 = 'PQTMUNIQID'
frame3 = f'${cmd3}*{checksum(cmd3)}\r\n'
print(f'\n[发送] {frame3.strip()}')
s.write(frame3.encode('ascii'))
s.flush()
end = time.time() + 1.5
buf3 = b''
while time.time() < end:
    chunk = s.read(256)
    if chunk:
        buf3 += chunk
    else:
        time.sleep(0.02)
print(f'\n[PQTMUNIQID 后 1.5s] 收到 {len(buf3)} 字节')
for line in buf3.split(b'\r\n'):
    if line.strip():
        print(f'  {line.decode("ascii", "replace")}')

s.close()
print('\n=== 诊断结束 ===')
