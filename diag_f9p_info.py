# -*- coding: utf-8 -*-
import sys, traceback
sys.path.insert(0, r'E:\GitHub\2RTKserver')
from rtkserver.calibration.drivers.base import Channel
from rtkserver.calibration.drivers.ublox_zed_f9p import ZEDF9PDriver

ch = Channel('COM12', 38400)
ch.open()
drv = ZEDF9PDriver(ch)
try:
    ok, msg, data = drv.info()
    print(f'info: ok={ok} msg={msg}', flush=True)
    for k in ('model_key','display_name','firmware','hw_version','sn','sn_source','uniq_id','caps'):
        print(f'  {k:15s} = {data.get(k)!r}', flush=True)
except Exception:
    traceback.print_exc()
ch.close()
print('done', flush=True)
