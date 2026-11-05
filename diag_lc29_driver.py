# -*- coding: utf-8 -*-
import sys, traceback
sys.path.insert(0, r'E:\GitHub\2RTKserver')
from rtkserver.calibration.drivers.base import Channel
from rtkserver.calibration.drivers.quectel_lc29hbs import LC29HBSDriver

ch = Channel('COM3', 115200)
ch.open()
print('channel opened', flush=True)
ch.drain()

drv = LC29HBSDriver(ch)
print('driver created', flush=True)

try:
    ok, msg, data = drv.detect()
    print(f'detect: ok={ok} msg={msg}', flush=True)
    print(f'  data={data}', flush=True)

    if ok:
        ok2, msg2, data2 = drv.info()
        print(f'info: ok={ok2} msg={msg2}', flush=True)
        print(f'  data={data2}', flush=True)
except Exception:
    traceback.print_exc()

ch.close()
print('done', flush=True)
