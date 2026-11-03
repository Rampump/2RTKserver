# -*- coding: utf-8 -*-
"""标定用的坐标换算与基础算法：WGS84 正反算、3σ 野值剔除、Welford、卡尔曼。

定位：纯函数 + 纯数据结构。不读 state、不开串口、不起线程，
所以可以在标定线程、Web 线程或 PC 上单独跑，行为完全一致。

为什么反向换算不自己写：`utils.common.ecef_to_latlon()` 已经在跑（1005 解析
用的就是它），再写一份就会出现"标定写进去的坐标"和"监控页显示的坐标"
两套算法，误差对不上时根本没法排查。这里只包一层，保证双向闭环。

自测（不需要硬件）：
    python -m rtkserver.calibration.geo
"""
import math

from ..utils.common import ecef_to_latlon

# ==================== WGS84 椭球（标准值，不要改） ====================
WGS84_A = 6378137.0                      # 长半轴 (m)
WGS84_F = 1.0 / 298.257223563            # 扁率
WGS84_B = WGS84_A * (1.0 - WGS84_F)      # 短半轴 = 6356752.314245 (m)
WGS84_E2 = WGS84_F * (2.0 - WGS84_F)     # 第一偏心率平方 = 0.006694379990141


# ==================== 坐标正反算 ====================
def geodetic_to_ecef(lat_deg, lon_deg, height=0.0):
    """WGS84 经纬高 → ECEF（地心地固直角坐标），单位米。

    标定写进模块的 `PQTMCFGSVIN,...` 用的就是这三个数，
    所以这里必须用标准椭球参数，一个都不能图省事用 6371000 之类的球体近似。
    """
    lat = math.radians(float(lat_deg))
    lon = math.radians(float(lon_deg))
    h = float(height)
    sin_lat = math.sin(lat)
    cos_lat = math.cos(lat)
    # 卯酉圈曲率半径
    n = WGS84_A / math.sqrt(1.0 - WGS84_E2 * sin_lat * sin_lat)
    x = (n + h) * cos_lat * math.cos(lon)
    y = (n + h) * cos_lat * math.sin(lon)
    z = (n * (1.0 - WGS84_E2) + h) * sin_lat
    return x, y, z


def ecef_to_geodetic(x, y, z):
    """ECEF → 经纬高，复用全局实现（与 1005 解析完全同一套算法）。"""
    return ecef_to_latlon(x, y, z)


# ==================== 站心坐标（ENU）与 ECEF 互转 ====================
# 说明：这里**没有** APC → ARP 天线相位中心换算。
# 本产品的用途是"天线固定好 → 测出该点坐标 → 钉死写入基准站"，
# 测量环节没有 RTK 测量杆/对中杆，用户手上给出的就是天线参考点本身；
# 再叠一层"相位中心 − 参考点"的偏移，只会因为填错方向把整站静默抬降一个天线高。
def enu_to_ecef(e, n, u, lat_deg, lon_deg):
    """站心 ENU 偏移 → ECEF 三轴偏移（米）。只做旋转，不平移。

    旋转矩阵（标准 WGS84 局部切平面）：
        dX = −sinλ·E − sinφ·cosλ·N + cosφ·cosλ·U
        dY =  cosλ·E − sinφ·sinλ·N + cosφ·sinλ·U
        dZ =           cosφ·N      + sinφ·U
    """
    lat = math.radians(float(lat_deg))
    lon = math.radians(float(lon_deg))
    sin_lat, cos_lat = math.sin(lat), math.cos(lat)
    sin_lon, cos_lon = math.sin(lon), math.cos(lon)
    e, n, u = float(e), float(n), float(u)
    dx = -sin_lon * e - sin_lat * cos_lon * n + cos_lat * cos_lon * u
    dy = cos_lon * e - sin_lat * sin_lon * n + cos_lat * sin_lon * u
    dz = cos_lat * n + sin_lat * u
    return dx, dy, dz


def ecef_to_enu(dx, dy, dz, lat_deg, lon_deg):
    """ECEF 三轴偏移 → 站心 ENU。`enu_to_ecef` 的转置（旋转矩阵正交，逆即转置）。"""
    lat = math.radians(float(lat_deg))
    lon = math.radians(float(lon_deg))
    sin_lat, cos_lat = math.sin(lat), math.cos(lat)
    sin_lon, cos_lon = math.sin(lon), math.cos(lon)
    dx, dy, dz = float(dx), float(dy), float(dz)
    e = -sin_lon * dx + cos_lon * dy
    n = -sin_lat * cos_lon * dx - sin_lat * sin_lon * dy + cos_lat * dz
    u = cos_lat * cos_lon * dx + cos_lat * sin_lon * dy + sin_lat * dz
    return e, n, u


def max_axis_delta(x1, y1, z1, x2, y2, z2):
    """两组 ECEF 的最大单轴偏差（米）。标定记录的 verify_delta 用这个。

    用最大轴偏差而不是三维距离：写进模块的坐标错在哪个轴上，
    看单轴偏差能一眼定位（例如 z 差 2 米多半是高程/大地水准面搞混了）。
    """
    return max(abs(float(x1) - float(x2)),
               abs(float(y1) - float(y2)),
               abs(float(z1) - float(z2)))


# ==================== Welford 在线均值/方差 ====================
class Welford:
    """单变量在线均值与方差（数值稳定，不需要先攒满样本）。

    标定的样本是"每来一帧就更新一次"，用 sum/sum2 累加在 ARM 单精度模拟下
    会出现大数相减的精度崩坏（ECEF 是 6.4e6 量级），所以必须用 Welford。
    """

    __slots__ = ('count', 'mean', '_m2')

    def __init__(self):
        self.count = 0
        self.mean = 0.0
        self._m2 = 0.0

    def add(self, value):
        value = float(value)
        self.count += 1
        delta = value - self.mean
        self.mean += delta / self.count
        self._m2 += delta * (value - self.mean)
        return self.mean

    @property
    def m2(self):
        return self._m2

    @property
    def variance(self):
        """样本方差（n-1）。样本不足 2 个时返回 0.0，调用方不必判空。"""
        if self.count < 2:
            return 0.0
        return self._m2 / (self.count - 1)

    @property
    def std(self):
        return math.sqrt(self.variance)

    def reset(self):
        self.count = 0
        self.mean = 0.0
        self._m2 = 0.0


class Welford3:
    """三轴 ECEF 的在线统计。`sigma3d` 就是标定记录里的 accuracy 字段。"""

    __slots__ = ('x', 'y', 'z')

    def __init__(self):
        self.x = Welford()
        self.y = Welford()
        self.z = Welford()

    def add(self, x, y, z):
        self.x.add(x)
        self.y.add(y)
        self.z.add(z)
        return self.mean

    @property
    def count(self):
        return self.x.count

    @property
    def mean(self):
        return self.x.mean, self.y.mean, self.z.mean

    @property
    def sigma3d(self):
        """三维点位标准差 σ = sqrt(σx² + σy² + σz²)，单位米。"""
        return math.sqrt(self.x.variance + self.y.variance + self.z.variance)

    def reset(self):
        self.x.reset()
        self.y.reset()
        self.z.reset()


# ==================== 3σ 野值剔除 ====================
def reject_outliers_3sigma(values, k=3.0, min_samples=8):
    """单变量 3σ 剔除，返回 `(kept_values, kept_indexes)`。

    样本少于 `min_samples` 时原样返回：8 个点算出来的 σ 本身噪声极大，
    硬剔除只会把正常点当野值丢掉（尤其是刚开 Survey-in 的头几秒）。
    标准差为 0（全部相同）时不剔除任何点。
    """
    data = [float(v) for v in values]
    n = len(data)
    if n < min_samples:
        return data, list(range(n))

    stat = Welford()
    for v in data:
        stat.add(v)
    sigma = stat.std
    if sigma <= 0.0:
        return data, list(range(n))

    limit = k * sigma
    kept_values = []
    kept_indexes = []
    for index, v in enumerate(data):
        if abs(v - stat.mean) <= limit:
            kept_values.append(v)
            kept_indexes.append(index)
    return kept_values, kept_indexes


def reject_outliers_3sigma_xyz(samples, k=3.0, min_samples=8):
    """三维样本 `[(x, y, z), ...]` 的 3σ 剔除。

    任一轴越界即整点丢弃：ECEF 三个分量描述的是同一个点，
    只保留两个分量没有意义。返回 `(kept_samples, kept_indexes)`。
    """
    data = [(float(s[0]), float(s[1]), float(s[2])) for s in samples]
    n = len(data)
    if n < min_samples:
        return data, list(range(n))

    stats = Welford3()
    for x, y, z in data:
        stats.add(x, y, z)

    limits = (k * stats.x.std, k * stats.y.std, k * stats.z.std)
    means = stats.mean

    kept = []
    kept_indexes = []
    for index, (x, y, z) in enumerate(data):
        if (abs(x - means[0]) <= limits[0]
                and abs(y - means[1]) <= limits[1]
                and abs(z - means[2]) <= limits[2]):
            kept.append((x, y, z))
            kept_indexes.append(index)
    return kept, kept_indexes


# ==================== 卡尔曼（常值模型，三轴独立） ====================
class Kalman1D:
    """一维常值模型的卡尔曼滤波：状态 x 不随预测步变化，只累积过程噪声。

    预测：P ← P + q
    更新：K = P / (P + r);  x ← x + K(z − x);  P ← (1 − K)P

    参数含义（务必按单位理解，否则调不出效果）：
        r  观测噪声方差，单位 m²。模块 1Hz 输出的定位抖动 σ≈0.3m 时 r≈0.09。
        q  每步过程噪声方差，单位 m²。基准站静止，理想情况是 0，
           但给一点（默认 1e-4）可以让滤波器不至于对真实变化反应太慢。
    """

    __slots__ = ('q', 'r', 'x', 'p')

    def __init__(self, q=1e-4, r=1.0, x0=None, p0=None):
        self.q = float(q)
        self.r = float(r)
        self.x = None if x0 is None else float(x0)
        # 首次观测时把 P 设成 r：表示"我完全相信第一帧"
        self.p = None if p0 is None else float(p0)

    def update(self, z):
        z = float(z)
        if self.x is None:
            self.x = z
            self.p = self.r if self.p is None else self.p
            return self.x
        self.p += self.q
        gain = self.p / (self.p + self.r)
        self.x += gain * (z - self.x)
        self.p *= (1.0 - gain)
        return self.x

    @property
    def variance(self):
        """当前状态估计方差（不是观测方差）。"""
        return 0.0 if self.p is None else self.p

    def reset(self):
        self.x = None
        self.p = None


class Kalman3:
    """ECEF 三轴独立滤波。为什么不做 3×3 协方差：

    基准站静止、三轴误差近似不相关，做全协方差要写矩阵求逆，
    在 ARM 单核上是纯浪费，而且现场根本调不出差别。
    """

    __slots__ = ('axes',)

    def __init__(self, q=1e-4, r=1.0):
        self.axes = (Kalman1D(q=q, r=r),
                     Kalman1D(q=q, r=r),
                     Kalman1D(q=q, r=r))

    def update(self, x, y, z):
        return (self.axes[0].update(x),
                self.axes[1].update(y),
                self.axes[2].update(z))

    @property
    def state(self):
        return tuple(axis.x for axis in self.axes)

    @property
    def sigma(self):
        """滤波后的 1σ 估计（米）。用于页面上"还在收敛"的提示。"""
        return math.sqrt(sum(axis.variance for axis in self.axes))

    def reset(self):
        for axis in self.axes:
            axis.reset()


# ==================== 自测入口（不需要硬件） ====================
def _selftest():
    """已知点往返校验 + 3σ 剔除 + Welford 一致性 + 滤波收敛。

    跑法：python -m rtkserver.calibration.geo
    任一条不满足即抛 AssertionError，退出码非 0。
    """
    import random

    print('=== geo 自测 ===')

    # 1) 椭球常数
    assert abs(WGS84_B - 6356752.314245) < 1e-6, WGS84_B
    assert abs(WGS84_E2 - 0.0066943799901413) < 1e-15, WGS84_E2
    print(f'[1] 椭球常数 OK  b={WGS84_B:.6f}  e2={WGS84_E2:.15f}')

    # 2) 极点/原点这类可以手算的点
    x, y, z = geodetic_to_ecef(0.0, 0.0, 0.0)
    assert abs(x - WGS84_A) < 1e-9 and abs(y) < 1e-9 and abs(z) < 1e-9
    x, y, z = geodetic_to_ecef(90.0, 0.0, 0.0)
    assert abs(x) < 1e-6 and abs(z - WGS84_B) < 1e-6, (x, z)
    print('[2] 赤道/北极已知值 OK')

    # 3) 双向互转：要求经纬度误差 < 1e-6 度
    points = [
        (39.9042, 116.4074, 50.0),
        (22.5431, 114.0579, -10.0),
        (45.75, 126.65, 150.0),
        (-33.8688, 151.2093, 30.0),
        (0.0, 0.0, 0.0),
        (89.9, -179.9, 3000.0),
        (-89.9, 179.9, -100.0),
    ]
    worst_latlon = 0.0
    worst_h = 0.0
    for lat, lon, h in points:
        ex, ey, ez = geodetic_to_ecef(lat, lon, h)
        lat2, lon2, h2 = ecef_to_geodetic(ex, ey, ez)
        d_lat = abs(lat - lat2)
        # 经度在 ±180 附近会绕圈，按最短弧算
        d_lon = abs(lon - lon2)
        d_lon = min(d_lon, 360.0 - d_lon)
        worst_latlon = max(worst_latlon, d_lat, d_lon)
        worst_h = max(worst_h, abs(h - h2))
    assert worst_latlon < 1e-6, worst_latlon
    assert worst_h < 1e-3, worst_h
    print(f'[3] 往返误差 OK  最大经纬误差={worst_latlon:.3e}°  最大高程误差={worst_h:.3e}m')

    # 4) 3σ 剔除：95 个正常点 + 5 个 +50m 野值
    random.seed(20261101)
    values = [random.gauss(0.0, 0.5) for _ in range(95)]
    values += [50.0 + i for i in range(5)]
    kept, kept_idx = reject_outliers_3sigma(values)
    assert len(kept) == 95, len(kept)
    assert max(kept_idx) == 94, kept_idx[-3:]
    print(f'[4] 3σ 剔除 OK  保留 {len(kept)}/100，野值全部识别')

    # 5) 样本不足时不剔除
    short = [1.0, 1000.0, 2.0]
    kept_short, _ = reject_outliers_3sigma(short)
    assert len(kept_short) == 3
    print('[5] 小样本保护 OK（<8 点不做剔除）')

    # 6) Welford 与两遍法一致
    data = [random.gauss(1000.0, 3.0) for _ in range(500)]
    stat = Welford()
    for v in data:
        stat.add(v)
    mean_ref = sum(data) / len(data)
    var_ref = sum((v - mean_ref) ** 2 for v in data) / (len(data) - 1)
    assert abs(stat.mean - mean_ref) < 1e-9
    assert abs(stat.variance - var_ref) < 1e-6 * max(1.0, var_ref)
    print(f'[6] Welford OK  mean={stat.mean:.6f} std={stat.std:.6f}')

    # 7) 三轴 Welford 的 sigma3d
    stat3 = Welford3()
    for _ in range(200):
        stat3.add(random.gauss(0, 1), random.gauss(0, 2), random.gauss(0, 3))
    # σ3d ≈ sqrt(1+4+9) = 3.7417
    assert abs(stat3.sigma3d - math.sqrt(14.0)) < 0.4, stat3.sigma3d
    print(f'[7] Welford3 OK  sigma3d={stat3.sigma3d:.4f}（理论 3.7417）')

    # 8) 卡尔曼收敛：真值 1000，观测 σ=0.3
    kf = Kalman1D(q=1e-4, r=0.09)
    raw = []
    filtered = []
    for _ in range(300):
        z = random.gauss(1000.0, 0.3)
        raw.append(z)
        filtered.append(kf.update(z))
    tail_raw = raw[-100:]
    tail_filtered = filtered[-100:]
    std_raw = math.sqrt(sum((v - sum(tail_raw) / 100) ** 2 for v in tail_raw) / 99)
    std_f = math.sqrt(sum((v - sum(tail_filtered) / 100) ** 2 for v in tail_filtered) / 99)
    assert std_f < std_raw * 0.5, (std_f, std_raw)
    assert abs(filtered[-1] - 1000.0) < 0.15, filtered[-1]
    print(f'[8] 卡尔曼收敛 OK  原始σ={std_raw:.3f}  滤波σ={std_f:.3f}  末值={filtered[-1]:.3f}')

    # 9) 卡尔曼对单点跳变的抑制：稳态增益 K≈0.033（q=1e-4, r=0.09），
    #    100 米跳变只应带动 3.3 米左右。注意卡尔曼不是野值剔除器，
    #    真正剔除野值是上面的 3σ 的职责，这里只验证"不会被一帧带飞"。
    kf2 = Kalman1D(q=1e-4, r=0.09)
    for _ in range(200):
        kf2.update(500.0)
    kf2.update(600.0)   # 单点跳 100 米
    shift = abs(kf2.x - 500.0)
    assert shift < 5.0, shift
    print(f'[9] 单点跳变抑制 OK  跳 100m 只带动 {shift:.3f}m')

    # 10) max_axis_delta
    assert abs(max_axis_delta(1, 2, 3, 1.5, 2, 3) - 0.5) < 1e-12
    print('[10] max_axis_delta OK')

    # 11) ENU 三轴单位向量：U 必须就是椭球法线（本地垂线）方向
    lat0, lon0 = 30.5, 114.3
    du = enu_to_ecef(0.0, 0.0, 1.0, lat0, lon0)
    # 法线方向 = (cosφcosλ, cosφsinλ, sinφ)，注意它不等于地心位置向量的方向
    # （两者差一个约 0.1° 的垂线偏差，用位置向量去判会得到 0.99999 而不是 1）
    phi, lam = math.radians(lat0), math.radians(lon0)
    normal = (math.cos(phi) * math.cos(lam), math.cos(phi) * math.sin(lam), math.sin(phi))
    assert max(abs(a - b) for a, b in zip(du, normal)) < 1e-12, du
    # E 方向必须与经线方向垂直（与 z 轴正交）
    de = enu_to_ecef(1.0, 0.0, 0.0, lat0, lon0)
    assert abs(de[2]) < 1e-12, de
    # 三轴长度都是 1（旋转不放大）
    for vec in (de, enu_to_ecef(0.0, 1.0, 0.0, lat0, lon0), du):
        assert abs(math.sqrt(sum(v * v for v in vec)) - 1.0) < 1e-12, vec
    print('[11] ENU→ECEF 旋转 OK  单位向量、U 与天顶重合')

    # 12) ENU ↔ ECEF 往返
    worst = 0.0
    for e, n, u in ((1.0, 2.0, 3.0), (-0.5, 0.25, 1.75), (0.0, 0.0, 0.0)):
        d = enu_to_ecef(e, n, u, lat0, lon0)
        back = ecef_to_enu(*d, lat0, lon0)
        worst = max(worst, max(abs(a - b) for a, b in zip((e, n, u), back)))
    assert worst < 1e-12, worst
    print(f'[12] ENU 往返 OK  最大误差 {worst:.3e} m')

    print('=== geo 自测全部通过 ===')
    return 0


if __name__ == '__main__':
    raise SystemExit(_selftest())
