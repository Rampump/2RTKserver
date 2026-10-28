// DOM 元素引用
const container = document.getElementById("signal-container");
const updateTimeEl = document.getElementById("update-time");
const systemCountEl = document.getElementById("system-count");
const satelliteCountEl = document.getElementById("satellite-count");
const strongSignalCountEl = document.getElementById("strong-signal-count");
const stationIdEl = document.getElementById("station-id");
const ecefXEl = document.getElementById("ecef-x");
const ecefYEl = document.getElementById("ecef-y");
const ecefZEl = document.getElementById("ecef-z");
const lonEl = document.getElementById("lon");
const latEl = document.getElementById("lat");
const heightEl = document.getElementById("height");
const rtcmTypeListEl = document.getElementById("rtcm-type-list");
const loadingIndicator = document.getElementById("loading-indicator");
const observedSystemsEl = document.getElementById("observed-systems")
const runModeEl = document.getElementById("run-mode");
const dataSourceEl = document.getElementById("data-source");
const targetCasterEl = document.getElementById("target-caster");
const dataSentEl = document.getElementById("data-sent");
const runTimeEl = document.getElementById("run-time");
const deviceModelEl = document.getElementById('device-model');
const firmwareVersionEl = document.getElementById('firmware-version');
const antennaModelEl = document.getElementById('antenna-model');
const lcStatusEl = document.getElementById('lc-status');
const lcAddressEl = document.getElementById('lc-address');
const lcMountEl = document.getElementById('lc-mount');
const lcClientsEl = document.getElementById('lc-clients');
const lcHintEl = document.getElementById('lc-hint');
const upStatusEl = document.getElementById('up-status');
const upTargetEl = document.getElementById('up-target');
const upMountpointEl = document.getElementById('up-mountpoint');
const upSentEl = document.getElementById('up-sent');
const upRateEl = document.getElementById('up-rate');
const upReconnectsEl = document.getElementById('up-reconnects');
const upSessionEl = document.getElementById('up-session');
const upHintEl = document.getElementById('up-hint');
const radioStatusEl = document.getElementById('radio-status');
const radioPortEl = document.getElementById('radio-port');
const radioBaudEl = document.getElementById('radio-baud');
const radioSentEl = document.getElementById('radio-sent');
const radioRateEl = document.getElementById('radio-rate');
const radioReconnectsEl = document.getElementById('radio-reconnects');
const radioHintEl = document.getElementById('radio-hint');
const amapBtn = document.getElementById('amap-btn');
const osmBtn = document.getElementById('osm-btn');

// 缓存最近一次后端状态，供"切换语言"时立即按新语言重渲染（无需等下一次 ws 推送）。
let _lastRunStatus = null;
let _lastUpload = null;
let _lastLocalCaster = null;
let _lastRadio = null;
let _lastConfig = null;

// 字节数转可读字符串（与后端 format_bytes 一致）
function formatBytes(num) {
  if (num === null || num === undefined || isNaN(num)) return '0 B';
  let n = Number(num);
  // 速率是浮点数（字节数 / 时间差），1024 以下也要取整，
  // 否则状态页会显示成 "609.132737367983 B/s" 这种一长串小数。
  if (n < 1024) return Math.round(n) + ' B';
  const units = ['KB', 'MB', 'GB', 'TB'];
  let i = -1;
  do {
    n /= 1024;
    i++;
  } while (n >= 1024 && i < units.length - 1);
  return n.toFixed(n >= 100 ? 0 : n >= 10 ? 1 : 2) + ' ' + units[i];
}

// === 加载频率映射表 JSON ===
let gnssSignalFreqMap = {};

fetch('/static/freq_map.json')
  .then(res => res.json())
  .then(data => {
    gnssSignalFreqMap = data;
    console.log('频率映射表已加载', gnssSignalFreqMap);
    initWebSocket();
  })
  .catch(err => {
    console.error('频率映射表加载失败', err);
    initWebSocket();
  });

// 配置信息
const gnssColorMap = {
  'GPS': '#165DFF',
  'BDS': '#00B42A',
  'GLONASS': '#FF7D00',
  'GALILEO': '#36CFC9',
  'QZSS': '#86909C',
  'IRNSS': '#F53F3F',
  'UNKNOWN': '#86909C'
};

// 把 #RRGGBB 转成 rgba()：星座徽标需要"主色 10% 透明度"的浅色底。
// 原来是用模板字符串拼 Tailwind 类名（bg-#165DFF/10），这种类名不合法、不会被生成。
function hexToRgba(hex, alpha) {
  const match = /^#?([0-9a-f]{6})$/i.exec(String(hex || ''));
  if (!match) return `rgba(100, 116, 139, ${alpha})`; // 兜底：slate-500
  const value = parseInt(match[1], 16);
  return `rgba(${(value >> 16) & 255}, ${(value >> 8) & 255}, ${value & 255}, ${alpha})`;
}
const gnssIconMap = {
  'GPS': 'fi fi-us',                   // 美国国旗
  'BDS': 'fi fi-cn',                  // 中国国旗
  'GLO': 'fi fi-ru',                 // 俄罗斯旗
  'GAL': 'fi fi-eu',                // 欧盟旗帜
  'QZS': 'fi fi-jp',               // 日本国旗
  'IRN': 'fi fi-in',              // 印度国旗
  'NAV': 'fi fi-in',             // 印度国旗
  'SBAS': 'fi fi-un',           // 联合国旗帜
  'UNKNOWN': 'fi fi-un'        // 联合国旗帜
};

// RTCM标准名称映射 (添加北斗系统映射)
const gnssAbbrMap = {
  'GPS': 'GPS',
  'BEIDOU': 'BDS', 
  'GLONASS': 'GLO',
  'GALILEO': 'GAL',
  'QZSS': 'QZS',
  'IRNSS': 'IRN',
  'SBAS': 'SBAS',
  'NAVIC':'NAV',
  'UNKNOWN': 'UNKNOWN'
};

// 信号强度阈值：统一在这里定义，避免"统计口径"和"柱状图配色"两套标准
const STRONG_SIGNAL_DB_HZ = 35;   // 判定为优良信号的门槛
const BAR_GREEN_DB_HZ = 35;
const BAR_AMBER_DB_HZ = 30;
const BAR_RED_DB_HZ = 15;

// WebSocket 连接管理
let ws;
let reconnectInterval;
const MAX_RECONNECT_ATTEMPTS = 10;
let reconnectAttempts = 0;
const RECONNECT_DELAY = 3000; 

// 状态变量
let systemCount = 0;
let satelliteCount = 0;
let strongSignalCount = 0;
const allSatellites = {}; // 按系统存储所有卫星数据
const observedSystems = new Set(); // 存储观测到的星座系统

// 地图相关变量
let map = null;
let stationPosition = null;
let mapInitialized = false;
let mapSourceType = 'unknown'; // 'amap' 或 'osm'
let circle20kmLayer = null;
let circle50kmLayer = null;

// ================== 坐标转换函数 ==================
const PI = 3.14159265358979324;

// WGS84转GCJ02(火星坐标系) - 全球统一转换
function wgs84ToGcj02(wgsLon, wgsLat) {
  let dLat = transformLat(wgsLon - 105.0, wgsLat - 35.0);
  let dLon = transformLon(wgsLon - 105.0, wgsLat - 35.0);
  const radLat = (wgsLat / 180.0) * PI;
  let magic = Math.sin(radLat);
  magic = 1 - 0.006693421622965943 * magic * magic;
  const sqrtMagic = Math.sqrt(magic);
  dLat = (dLat * 180.0) / ((6378245.0 * (1 - 0.006693421622965943)) / (magic * sqrtMagic) * PI);
  dLon = (dLon * 180.0) / (6378245.0 / sqrtMagic * Math.cos(radLat) * PI);
  
  return [wgsLon + dLon, wgsLat + dLat];
}

// 辅助函数
function transformLat(x, y) {
  let ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x));
  ret += (20.0 * Math.sin(6.0 * x * PI) + 20.0 * Math.sin(2.0 * x * PI)) * 2.0 / 3.0;
  ret += (20.0 * Math.sin(y * PI) + 40.0 * Math.sin(y / 3.0 * PI)) * 2.0 / 3.0;
  ret += (160.0 * Math.sin(y / 12.0 * PI) + 320 * Math.sin(y * PI / 30.0)) * 2.0 / 3.0;
  return ret;
}

function transformLon(x, y) {
  let ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x));
  ret += (20.0 * Math.sin(6.0 * x * PI) + 20.0 * Math.sin(2.0 * x * PI)) * 2.0 / 3.0;
  ret += (20.0 * Math.sin(x * PI) + 40.0 * Math.sin(x / 3.0 * PI)) * 2.0 / 3.0;
  ret += (150.0 * Math.sin(x / 12.0 * PI) + 300.0 * Math.sin(x / 30.0 * PI)) * 2.0 / 3.0;
  return ret;
}

// 坐标转换缓存（提高性能）
const coordCache = new Map();

function cachedWgs84ToGcj02(lon, lat) {
  const key = `${lon.toFixed(6)}_${lat.toFixed(6)}`;
  
  if (coordCache.has(key)) {
    return coordCache.get(key);
  }
  
  const result = wgs84ToGcj02(lon, lat);
  coordCache.set(key, result);
  return result;
}

// 添加日志显示函数
function addLog(message, type = 'info') {
  const logContainer = document.getElementById('log-container');
  if (!logContainer) return;

  // 第一条真实日志到达时清掉模板里的"加载中"占位，否则它会一直挂在最上面
  const placeholder = logContainer.querySelector('[data-placeholder]');
  if (placeholder) placeholder.remove();
  
  // 创建日志条目
  const logEntry = document.createElement('div');
  
  // 设置日志类型样式（对应 theme.css 里的 .log-* ，原先是 Tailwind 工具类）
  const typeClasses = {
    'info': 'log-info',
    'success': 'log-success',
    'warning': 'log-warning',
    'error': 'log-error',
    'system': 'log-system'
  };
  
  // 获取当前时间
  const now = new Date();
  const timeString = now.toLocaleTimeString();
  
  // 设置日志内容
  logEntry.className = typeClasses[type] || 'log-info';
  // 用 textContent 而不是 innerHTML：日志里可能带有配置值等外部字符串，
  // 走 innerHTML 会把其中的 HTML 当标签解析（自伤型 XSS）
  logEntry.textContent = `[${timeString}] ${message}`;
  
  // 添加到容器并滚动到底部
  logContainer.appendChild(logEntry);
  logContainer.scrollTop = logContainer.scrollHeight;
  
  // 限制日志数量，超过500条自动清理
  const entries = logContainer.querySelectorAll('div');
  if (entries.length > 500) {
    logContainer.removeChild(entries[0]);
  }
}

// ================== 地图相关函数封装 ==================

// 创建标记层
function createMarkerLayer() {
  return new ol.layer.Vector({
    source: new ol.source.Vector(),
    style: new ol.style.Style({
      image: new ol.style.Icon({
        src: 'https://cdn.jsdelivr.net/gh/pointhi/leaflet-color-markers@master/img/marker-icon-2x-red.png',
        scale: 0.5,
        anchor: [0.5, 1]
      })
    })
  });
}

// 添加圆形图层到地图（新增函数）
function addCircleLayersToMap() {
  if (!map) return;

  // 创建20km图层如果不存在
  if (!circle20kmLayer) {
    circle20kmLayer = new ol.layer.Vector({
      source: new ol.source.Vector(),
      zIndex: 999
    });
  }

  // 创建50km图层如果不存在
  if (!circle50kmLayer) {
    circle50kmLayer = new ol.layer.Vector({
      source: new ol.source.Vector(),
      zIndex: 998
    });
  }

  // 添加到地图（防止重复添加）
  const layers = map.getLayers();
  if (!layers.getArray().includes(circle20kmLayer)) {
    map.addLayer(circle20kmLayer);
  }
  
  if (!layers.getArray().includes(circle50kmLayer)) {
    map.addLayer(circle50kmLayer);
  }
}

// 设置活动地图按钮
function setActiveMapButton(activeBtn, inactiveBtn) {
  activeBtn.disabled = true;
  activeBtn.classList.add('is-active');

  inactiveBtn.disabled = false;
  inactiveBtn.classList.remove('is-active');
}

// 地图初始化完成后的统一处理
function finalizeMapSetup() {
  mapInitialized = true;
  if (stationPosition) {
    updateCircles(stationPosition.lat, stationPosition.lon);
  }
}

// 尝试初始化地图
function tryInitMap(type, config) {
  if (map) map.setTarget(null);
  
  const initFunc = type === 'amap' ? initAmapMap : initOsmMap;
  try {
    initFunc(config);
    mapSourceType = type;
        if (stationPosition) {
      updateMarker(stationPosition.lat, stationPosition.lon, dataSourceEl.textContent || I18N.t('map.marker'));
      updateCircles(stationPosition.lat, stationPosition.lon);
    }

    return true;
  } catch (e) {
    console.warn(`${type} 地图初始化失败:`, e);
    return false;
  }
}

// 初始化地图 - 自动选择最佳地图源
function initMap(mapConfig) {
  // 确保参数有效，提供默认值
  const defaultLat = mapConfig.default_lat || 39.9042;
  const defaultLon = mapConfig.default_lon || 116.4074;
  const defaultZoom = mapConfig.default_zoom || 12;
  
  const mapContainer = document.getElementById('map-container');
  const placeholder = mapContainer.querySelector('.map-placeholder');
  
  if (placeholder) placeholder.remove();
  
  const success = tryInitMap('amap', {
    default_lat: defaultLat,
    default_lon: defaultLon,
    default_zoom: defaultZoom
  });
  
  const fallbackTimer = setTimeout(() => {
    if (!mapInitialized) {
      console.log(I18N.t('ws.map_timeout'));
      tryInitMap('osm', {
        default_lat: defaultLat,
        default_lon: defaultLon,
        default_zoom: defaultZoom
      });
    }
  }, 5000);

  // 添加按钮点击事件处理
  amapBtn.addEventListener('click', () => {
    if (tryInitMap('amap', mapConfig)) {
      setActiveMapButton(amapBtn, osmBtn);
    }
  });

  osmBtn.addEventListener('click', () => {
    // 手动再点一次＝用户主动重试：如果之前整条瓦片源都失败了，从头再来一遍
    if (osmProvidersExhausted) {
      osmProviderIndex = 0;
      osmProvidersExhausted = false;
    }
    if (tryInitMap('osm', mapConfig)) {
      setActiveMapButton(osmBtn, amapBtn);
    }
  });
}

// 初始化高德地图（修改）
function initAmapMap(mapConfig) {
  const mapContainer = document.getElementById('map-container');
  
  try {
    // 移除现有地图（如果存在）
    if (map) {
      map.setTarget(null);
    }
    
    map = new ol.Map({
      target: 'map-container',
      layers: [
        new ol.layer.Tile({
          source: new ol.source.XYZ({
            url: 'https://wprd01.is.autonavi.com/appmaptile?x={x}&y={y}&z={z}&lang=zh_cn&size=1&scl=1&style=7',
            crossOrigin: 'anonymous'
          })
        })
      ],
      view: new ol.View({
        center: ol.proj.fromLonLat([mapConfig.default_lon, mapConfig.default_lat]),
        zoom: mapConfig.default_zoom
      })
    });
    
    // 使用封装的函数创建标记层
    const markerLayer = createMarkerLayer();
    map.addLayer(markerLayer);
    
    // 添加缩放控件
    map.addControl(new ol.control.Zoom());
    
    // 添加圆形图层（关键修复）
    addCircleLayersToMap();
    
    // 调用统一的地图初始化完成处理
    finalizeMapSetup();
    
    console.log("使用高德地图初始化成功");
    addLog(I18N.t('ws.map_amap_ok'), 'success');
    
    // 监听瓦片错误事件
    map.on('tileerror', (event) => {
      // 单块瓦片偶发失败不该整图切换，累计到一定次数再降级，否则地图会来回抖
      amapTileErrors++;
      console.error("高德地图瓦片加载错误:", event, `(${amapTileErrors})`);
      if (amapTileErrors < 5) return;
      amapTileErrors = 0;
      if (mapSourceType === 'amap') {
        addLog(I18N.t('ws.amap_fallback'), 'warning');
        console.log("切换到OpenStreetMap");
        tryInitMap('osm', mapConfig);
      }
    });
    
  } catch (error) {
    console.error("高德地图初始化失败:", error);
    addLog(`${I18N.t('ws.map_amap_fail')}: ${error.message}`, 'error'); 
    throw error; // 抛出异常以便上层处理
  }
}

// OpenStreetMap 瓦片源清单
// ---------------------------------------------------------------------------
// 原先直接用 new ol.source.OSM()，写死 https://tile.openstreetmap.org/{z}/{x}/{y}.png。
// 两个坑：
//   1. 官方瓦片服务器按"使用政策"强制要求请求带 Referer。一旦本站在响应头里声明
//      Referrer-Policy: no-referrer，浏览器就不发 Referer，瓦片会被直接拒绝（403）。
//      现在服务端改成了 strict-origin-when-cross-origin，这个坑已消除。
//   2. 国内网络直连 tile.openstreetmap.org 时常不可达/超时。这里准备几个同样免 key
//      的镜像，当前源连续报错就自动往下切，不用改代码。
// 注意：镜像只换了渲染图，地图数据仍是 OSM（ODbL），署名统一保留。
const OSM_PROVIDERS = [
  // 官方在国内网络中经常直接连不通（实测超时），所以把实测可用的社区镜像放前面，
  // 官方放第三位作为海外网络的首选回退。
  { name: 'OSM 德国镜像', url: 'https://tile.openstreetmap.de/{z}/{x}/{y}.png' },
  { name: 'OSM 法国镜像', url: 'https://a.tile.openstreetmap.fr/osmfr/{z}/{x}/{y}.png' },
  { name: 'OSM 官方', url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png' },
  { name: 'Carto 底图', url: 'https://basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png' }
];
let osmProviderIndex = 0;
let amapTileErrors = 0;
let osmProvidersExhausted = false;

// 初始化OpenStreetMap（修改）
function initOsmMap(mapConfig) {
  const mapContainer = document.getElementById('map-container');
  
  try {
    
    if (map) {
      map.setTarget(null);
    }

    const provider = OSM_PROVIDERS[osmProviderIndex] || OSM_PROVIDERS[0];
    
    map = new ol.Map({
      target: 'map-container',
      layers: [
        new ol.layer.Tile({
          source: new ol.source.XYZ({
            url: provider.url,
            // 必须显示署名，OSM 使用政策的要求
            attributions: '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors',
            maxZoom: 19
          })
        })
      ],
      view: new ol.View({
        center: ol.proj.fromLonLat([mapConfig.default_lon, mapConfig.default_lat]),
        zoom: mapConfig.default_zoom
      })
    });
    
    // 使用封装的函数创建标记层
    const markerLayer = createMarkerLayer();
    map.addLayer(markerLayer);
    
    // 添加缩放控件
    map.addControl(new ol.control.Zoom());
    
    // 添加圆形图层（关键修复）
    addCircleLayersToMap();
    
    // 调用统一的地图初始化完成处理
    finalizeMapSetup();
    
    console.log(`使用OpenStreetMap初始化成功（瓦片源：${provider.name}）`);
    addLog(I18N.t('ws.map_osm_ok', { provider: provider.name }), 'success');

    // 当前瓦片源连续报错就切到下一个镜像。403（缺 Referer）和超时都会走到这里。
    const myMap = map;
    let tileErrors = 0;
    map.on('tileerror', () => {
      if (map !== myMap) return;          // 已经切换到别的地图实例，忽略旧事件
      if (osmProvidersExhausted) return;  // 全部失败后不再重复告警
      tileErrors++;
      if (tileErrors < 3) return;
      tileErrors = 0;

      if (osmProviderIndex < OSM_PROVIDERS.length - 1) {
        osmProviderIndex++;
        const next = OSM_PROVIDERS[osmProviderIndex];
        addLog(I18N.t('ws.map_tile_fail', { from: provider.name, to: next.name }), 'warning');
        tryInitMap('osm', mapConfig);
      } else {
        osmProvidersExhausted = true;
        addLog(I18N.t('ws.map_all_fail'), 'error');
      }
    });
    
  } catch (error) {
    console.error("OpenStreetMap初始化失败:", error);
    addLog(`${I18N.t('ws.map_amap_fail')}: ${error.message}`, 'error');
    // 显示错误信息
    mapContainer.innerHTML = `
      <div class="map-placeholder">
        <i class="fa fa-exclamation-triangle" style="color: var(--danger); opacity: 1" aria-hidden="true"></i>
        <span class="ph-title" style="color: var(--danger)">${I18N.t('ws.map_error')}</span>
        <span class="ph-desc">${error.message}</span>
        <span class="ph-desc">${I18N.t('ws.map_unavailable')}</span>
      </div>
    `;
    throw error; // 抛出异常以便上层处理
  }
}

// 更新标记位置（添加坐标转换）
function updateMarker(lat, lon, name) {
  stationPosition = { lat, lon };
  
  if (!mapInitialized || !map) return;
  
  const markerLayer = map.getLayers().getArray().find(l => l.getSource() instanceof ol.source.Vector);
  if (!markerLayer) return;
  
  const source = markerLayer.getSource();
  source.clear();
  
  // 坐标转换逻辑
  let displayLon = lon;
  let displayLat = lat;
  
  if (mapSourceType === 'amap') {
    // 高德地图需要转换到GCJ-02
    [displayLon, displayLat] = cachedWgs84ToGcj02(lon, lat);
    console.log(`坐标转换: WGS84(${lon.toFixed(6)},${lat.toFixed(6)}) -> GCJ02(${displayLon.toFixed(6)},${displayLat.toFixed(6)})`);
  } else {
    // OpenStreetMap直接使用WGS-84
    console.log(`使用原始坐标: WGS84(${lon.toFixed(6)},${lat.toFixed(6)})`);
  }
  
  const feature = new ol.Feature({
    geometry: new ol.geom.Point(ol.proj.fromLonLat([displayLon, displayLat])),
    name: I18N.t('map.marker')
  });

  // 设置标记样式，包括图标和文字标注
  feature.setStyle(new ol.style.Style({
    image: new ol.style.Icon({
      src: 'https://cdn.jsdelivr.net/gh/pointhi/leaflet-color-markers@master/img/marker-icon-2x-red.png',
      scale: 0.5,
      anchor: [0.5, 1]
    }),
    text: new ol.style.Text({
      text: I18N.t('map.marker'),
      font: '12px Arial',
      fill: new ol.style.Fill({ color: '#000' }),
      stroke: new ol.style.Stroke({ color: '#fff', width: 3 }),
      offsetX: 20,
      offsetY: 1
    })
  }));

  source.addFeature(feature);
  
  // 如果距离当前位置很远，则移动到新位置
  const view = map.getView();
  const currentCenter = view.getCenter();
  const newCenter = ol.proj.fromLonLat([displayLon, displayLat]);
  
  const distance = Math.sqrt(
    Math.pow(currentCenter[0] - newCenter[0], 2) + 
    Math.pow(currentCenter[1] - newCenter[1], 2)
  );
  
  if (distance > 100000) { // 100公里
    view.animate({
      center: newCenter,
      zoom: 10,
      duration: 2000
    });
  }
}

// 更新圆形覆盖范围（添加坐标转换）
function updateCircles(lat, lon) {
  if (!mapInitialized || !map) return;
  
  // 确保圆形图层存在（双重保险）
  addCircleLayersToMap();

  // 坐标转换逻辑
  let displayLon = lon;
  let displayLat = lat;
  
  if (mapSourceType === 'amap') {
    // 高德地图需要转换到GCJ-02
    [displayLon, displayLat] = cachedWgs84ToGcj02(lon, lat);
  }
  
  const center = ol.proj.fromLonLat([displayLon, displayLat]);
  const radius20km = 20000; // 20KM
  const radius50km = 50000; // 50KM

  // 圆形
const circle20kmFeature = new ol.Feature({
  geometry: new ol.geom.Circle(center, 20000)
});
circle20kmFeature.setStyle(new ol.style.Style({
  fill: new ol.style.Fill({
    color: 'rgba(0, 123, 255, 0.2)'
  }),
  stroke: new ol.style.Stroke({
    color: 'rgba(0, 123, 255, 0.5)',
    width: 2
  })
}));

const circle50kmFeature = new ol.Feature({
  geometry: new ol.geom.Circle(center, 50000)
});
circle50kmFeature.setStyle(new ol.style.Style({
  fill: new ol.style.Fill({
    color: 'rgba(0, 123, 255, 0.05)'
  }),
  stroke: new ol.style.Stroke({
    color: 'rgba(0, 123, 255, 0.3)',
    width: 1
  })
}));


  // 更新20KM图层数据
  const circle20kmSource = circle20kmLayer.getSource();
  circle20kmSource.clear();
  circle20kmSource.addFeature(circle20kmFeature);

  // 更新50KM图层数据
  const circle50kmSource = circle50kmLayer.getSource();
  circle50kmSource.clear();
  circle50kmSource.addFeature(circle50kmFeature);
}

// ================== 主应用逻辑 ==================

// 初始化WebSocket连接
function initWebSocket() {
  // 按当前页面协议自动选择 ws/wss，否则经由 HTTPS 反向代理访问时会被浏览器拦截
  const wsProtocol = location.protocol === 'https:' ? 'wss://' : 'ws://';
  ws = new WebSocket(wsProtocol + location.host + "/ws");
  
  ws.onopen = () => {
    console.log("WebSocket连接成功");
    reconnectAttempts = 0; // 重置重连计数
    clearInterval(reconnectInterval);
    
    addLog(I18N.t('ws.connected'), 'system');
    // 更新加载指示器
    loadingIndicator.innerHTML = `
      <div class="card-pad empty-state">
        <i class="fa fa-satellite spin" style="animation-duration: 8s" aria-hidden="true"></i>
        <span>${I18N.t('sig.receiving')}</span>
      </div>
    `;
    
    // 重置观测系统集合
    observedSystems.clear();
    updateObservedSystemsDisplay();
    
    // 重置系统计数
    systemCount = 0;
    systemCountEl.textContent = systemCount;
    
    // 请求运行配置（/api/load_config 返回 JSON；/config 返回的是 HTML 页面）
    fetch('/api/load_config', { headers: { 'Accept': 'application/json' } })
      .then(response => {
        const contentType = response.headers.get('content-type') || '';
        if (!response.ok || !contentType.includes('application/json')) {
          // 未登录时后端会跳转登录页，拿到的是 HTML，这里按"无配置"处理
          throw new Error('未登录或接口不可用');
        }
        return response.json();
      })
      .then(config => {
        updateStaticRunStatus(config);
      })
      .catch(error => {
        console.warn("获取运行配置失败（未登录时属正常）:", error.message);
      })
      .finally(() => {
        // 无论配置是否拿到，都按默认中心初始化地图
        initMap({
          default_lat: 31.643763,  // 默认纬度
          default_lon: 105.466293, // 默认经度
          default_zoom: 10         // 默认缩放级别
        });
      });
  };

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      console.log("收到WebSocket数据：", data);

      addLog(`${I18N.t('ws.received')}: ${JSON.stringify(data, null, 2).substring(0, 100)}...`, 'info');

      // 隐藏加载指示器
      if (loadingIndicator && !loadingIndicator.classList.contains('hidden')) {
        loadingIndicator.classList.add('hidden');
      }

      // 更新基站信息
      if (data.station_info && typeof data.station_info === "object") {
        console.log("收到基准站信息:", data.station_info);

        // 更新显示
        stationIdEl.textContent = data.station_info.station_id ?? "--";
        
        ecefXEl.textContent = data.station_info.ecef_x !== undefined
          ? data.station_info.ecef_x.toFixed(4)
          : "--";
          
        ecefYEl.textContent = data.station_info.ecef_y !== undefined
          ? data.station_info.ecef_y.toFixed(4)
          : "--";
          
        ecefZEl.textContent = data.station_info.ecef_z !== undefined
          ? data.station_info.ecef_z.toFixed(4)
          : "--";
          
        lonEl.textContent = data.station_info.longitude !== undefined
          ? data.station_info.longitude.toFixed(6)
          : "--";
          
        latEl.textContent = data.station_info.latitude !== undefined
          ? data.station_info.latitude.toFixed(6)
          : "--";
          
        heightEl.textContent = data.station_info.height !== undefined
          ? data.station_info.height.toFixed(2)
          : "--";
          
        // 更新地图标记
        if (data.station_info.latitude && data.station_info.longitude) {
          const dataSourceName = dataSourceEl.textContent || "基准站";
          updateMarker(data.station_info.latitude, data.station_info.longitude, dataSourceName);
          updateCircles(data.station_info.latitude, data.station_info.longitude);
        }
      }

      // 更新消息类型列表
      if (data.msg_type_frequency) {
        const frequencyList = Object.entries(data.msg_type_frequency)
          .map(([type, frequency]) => `${type}(${frequency})`)
          .join(', ');
        // 用 textContent：MSM 每秒多条，重建 innerHTML 会反复触发重排；
        // 同时也省掉一个不必要的 HTML 解析入口
        rtcmTypeListEl.textContent = frequencyList;
      }

      // 更新运行状态
      if (data.run_status) {
        updateRunStatus(data.run_status);
      }

      // 连接首帧的本地 Caster 状态（mode=none 时没有 run_status 在推，靠这条兜底）
      if (data.local_caster) {
        updateLocalCaster(data.local_caster);
      }

      // 数传透传状态兜底（同样在 mode=none 时靠首帧下发）
      if (data.radio) {
        updateRadioStatus(data.radio);
      }

      // 更新设备信息
      if (data.antenna_info) {
        antennaModelEl.textContent = data.antenna_info.antenna_name || "--";
      }
      if (data.device_info) {
        deviceModelEl.textContent = data.device_info.receiver_model || "--";
        firmwareVersionEl.textContent = data.device_info.firmware_version || "--";
      }

      // 如果是 MSM 数据才更新图表
      if (!data.cell_data || !data.gnss) return;

      // 使用 gnssAbbrMap 来获取标准化的 GNSS 名称
      const rawGnss = data.gnss || "UNKNOWN";
      const gnss = gnssAbbrMap[rawGnss] || gnssAbbrMap['UNKNOWN'];
      
      // 添加观测系统（确保不重复添加）
      if (!observedSystems.has(gnss)) {
        observedSystems.add(gnss);
        updateObservedSystemsDisplay();
      }
      
      const blockId = "block_" + gnss;
      const gnssColor = gnssColorMap[gnss] || gnssColorMap['UNKNOWN'];
      const gnssIcon = gnssIconMap[gnss] || gnssIconMap['UNKNOWN'];
      updateTimeDisplay();
      updateStatistics(gnss, data.cell_data);

      let block = document.getElementById(blockId);
      const isNewCard = !block;
      
      if (isNewCard) {
        block = document.createElement("div");
        block.id = blockId;
        block.className = "card card-pad fade-in";

        block.innerHTML = `
          <div class="gnss-hd">
            <div class="gnss-name">
              <span class="${gnssIcon}" aria-hidden="true"></span>
              <span>${gnss}</span>
            </div>
            <span class="badge signal-count"></span>
          </div>
          <div class="signal-bars"></div>
        `;

        // 确保卡片被添加到容器中
        container.appendChild(block);
      }

      // 更新（或初始化）卫星数量徽标。徽标底色用星座主色的 10% 透明度，
      // 原先拼的是 bg-#165DFF/10 这种非法类名，Tailwind 根本不会生成，等于没样式。
      const countSpan = block.querySelector('.signal-count');
      if (countSpan) {
        countSpan.textContent = `${I18N.t('sig.count')} ${data.cell_data.length}`;
        countSpan.style.color = gnssColor;
        countSpan.style.backgroundColor = hexToRgba(gnssColor, 0.1);
      }

      const signalBarsEl = block.querySelector(".signal-bars");
      renderSignalBars(signalBarsEl, data.cell_data, gnss, gnssSignalFreqMap);
    } catch (error) {
      console.error("处理WebSocket数据出错:", error);
      addLog(`${I18N.t('ws.data_error')}: ${error.message}`, 'error');
    }
  };

  ws.onerror = (error) => {
    console.error("WebSocket错误:", error);
    addLog(`${I18N.t('ws.error')}: ${error.message}`, 'error');
    handleDisconnection();
  };

  ws.onclose = (event) => {
    console.log("WebSocket连接关闭，代码:", event.code, "原因:", event.reason);
    addLog(I18N.t('ws.closed', { code: event.code, reason: event.reason }), 'warning');
    handleDisconnection();
  };
}

// 更新静态运行状态
// 后端 /api/load_config 返回的是扁平键（mode / serial_port / relay_host ...），
// 原实现读的是 config.general.mode、config.serial.port 这种嵌套结构，永远取不到值。
function updateStaticRunStatus(config) {
  if (!config || typeof config !== 'object') return;
  _lastConfig = config;

  const mode = config.mode || 'none';

  // 更新运行模式显示
  if (mode === 'serial') {
    runModeEl.textContent = I18N.t('mode.serial');
  } else if (mode === 'relay') {
    runModeEl.textContent = I18N.t('mode.relay');
  } else {
    runModeEl.textContent = I18N.t('mode.none');
  }

  // 根据模式显示数据源
  if (mode === 'serial') {
    const port = config.serial_port || I18N.t('mode.auto');
    const baud = config.baud_rate || I18N.t('mode.auto');
    dataSourceEl.textContent = `${port} @ ${baud}bps`;
  } else if (mode === 'relay') {
    const host = config.relay_host || I18N.t('mode.none');
    const port = config.relay_port || I18N.t('mode.none');
    const mountpoint = config.relay_mountpoint || I18N.t('mode.none');
    dataSourceEl.textContent = `${host}:${port}/${mountpoint}`;
  }

  // 显示目标caster
  const ntripHost = config.caster_host || I18N.t('mode.none');
  const ntripPort = config.caster_port || I18N.t('mode.none');
  const ntripMountpoint = config.caster_mountpoint || I18N.t('mode.none');
  targetCasterEl.textContent = `${ntripHost}:${ntripPort}/${ntripMountpoint}`;

  // 上传端卡片的静态兜底（websocket 数据到达前先显示目标地址/挂载点）
  if (upStatusEl && upTargetEl) {
    const hasHost = !!config.caster_host;
    if (hasHost) {
      upTargetEl.textContent = `${ntripHost}:${ntripPort}/${ntripMountpoint}`;
      upMountpointEl.textContent = config.caster_mountpoint || '--';
    } else {
      upStatusEl.textContent = I18N.t('up.unconfigured');
      upStatusEl.className = 'kv-value is-warning';
      upTargetEl.textContent = '--';
      upMountpointEl.textContent = '--';
    }
  }
}

// 更新运行状态
function updateRunStatus(status) {
  _lastRunStatus = status;
  if (status.mode) runModeEl.textContent = status.mode;
  if (status.data_source) dataSourceEl.textContent = status.data_source;
  if (status.target_caster) targetCasterEl.textContent = status.target_caster;
  if (status.data_sent) dataSentEl.textContent = status.data_sent;
  if (status.run_time) runTimeEl.textContent = status.run_time;
  if (status.local_caster) updateLocalCaster(status.local_caster);
  if (status.upload) updateUploadStatus(status.upload);
  if (status.radio) updateRadioStatus(status.radio);
}

// 上传端 NTRIP Caster 状态：显示往哪个地址/挂载点推、认证结果、已发送量、速率、重连次数。
function updateUploadStatus(info) {
  if (!info || !upStatusEl) return;
  _lastUpload = info;

  const setValue = (el, text, kind) => {
    if (!el) return;
    el.textContent = text;
    el.className = 'kv-value' + (kind ? ' is-' + kind : '');
  };

  // 地址 / 挂载点：未启用上传出口时一律显示 --，
  // 不把数据库里的历史残留地址显示成"正在上传"（这是用户当初的困惑点）。
  const host = info.host || '';
  const port = info.port || '';
  const mp = info.mountpoint || '';
  if (info.enabled) {
    upTargetEl.textContent = info.target || (host ? `${host}:${port}/${mp}` : '--');
    upMountpointEl.textContent = mp || '--';
  } else {
    upTargetEl.textContent = '--';
    upMountpointEl.textContent = '--';
  }

  // 数据统计
  const bytes = Number(info.sent_bytes) || 0;
  const frames = Number(info.sent_frames) || 0;
  upSentEl.textContent = `${formatBytes(bytes)} · ${frames} ${I18N.t('up.frames')}`;
  const rate = Number(info.rate_bps) || 0;
  upRateEl.textContent = rate > 0 ? `${formatBytes(rate)}/s` : '--';
  upReconnectsEl.textContent = String(Number(info.reconnects) || 0);
  upSessionEl.textContent = info.session_time || '0h 0m 0s';

  const state = info.state || 'idle';
  const stateText = info.state_text || state;

  if (!info.enabled) {
    setValue(upStatusEl, I18N.t('up.unconfigured'), 'warning');
    if (upHintEl) upHintEl.textContent = I18N.t('up.unconfigured_hint');
  } else if (state === 'authed') {
    setValue(upStatusEl, stateText, 'success');
    if (upHintEl) upHintEl.textContent = I18N.t('up.connected_hint', { target: info.target });
  } else if (state === 'connecting') {
    setValue(upStatusEl, stateText, '');
    if (upHintEl) upHintEl.textContent = I18N.t('up.connecting_hint');
  } else if (state === 'retrying') {
    setValue(upStatusEl, stateText, 'warning');
    if (upHintEl) upHintEl.textContent = info.last_error
      ? I18N.t('up.retrying_hint', { error: info.last_error })
      : I18N.t('up.retrying_hint_noerr');
  } else if (state === 'error') {
    setValue(upStatusEl, stateText, 'error');
    if (upHintEl) upHintEl.textContent = info.last_error || I18N.t('up.error_hint');
  } else {
    setValue(upStatusEl, stateText, '');
    if (upHintEl) upHintEl.textContent = '';
  }
}

// 本地 Caster 状态：配置页的勾选是"保存后生效"，这里显示的是实际在跑的监听服务，
// 两者可能暂时不一致（刚改完还没重启），所以文案要区分"未启用"和"已启用但没跑起来"。
function updateLocalCaster(info) {
  if (!info || !lcStatusEl) return;
  _lastLocalCaster = info;

  const setValue = (el, text, kind) => {
    if (!el) return;
    el.textContent = text;
    el.className = 'kv-value' + (kind ? ' is-' + kind : '');
  };

  lcAddressEl.textContent = info.example || '--';
  lcMountEl.textContent = info.mountpoint
    ? `${info.mountpoint} / ${info.user || '--'}`
    : '--';

  if (!info.enabled) {
    setValue(lcStatusEl, I18N.t('lc.disabled'), 'warning');
    setValue(lcClientsEl, '--', '');
    if (lcHintEl) lcHintEl.textContent = I18N.t('lc.disabled_hint');
  } else if (info.running) {
    setValue(lcStatusEl, I18N.t('lc.running'), 'success');
    setValue(lcClientsEl, `${info.clients} / ${info.max_clients}`, info.clients > 0 ? 'success' : '');
    if (lcHintEl) {
      lcHintEl.textContent =
        I18N.t('lc.running_hint', { addr: info.example, mp: info.mountpoint, user: info.user });
    }
  } else if (info.mode === 'none') {
    setValue(lcStatusEl, I18N.t('lc.none'), 'warning');
    setValue(lcClientsEl, '--', '');
    if (lcHintEl) lcHintEl.textContent = I18N.t('lc.none_hint');
  } else {
    setValue(lcStatusEl, I18N.t('lc.enabled_not_run'), 'warning');
    setValue(lcClientsEl, '--', '');
    if (lcHintEl) lcHintEl.textContent = I18N.t('lc.enabled_not_run_hint');
  }
}

// 无线电数传透传状态：单向广播，只发不收。显示串口/波特率/已发送/速率/重连次数。
function updateRadioStatus(info) {
  if (!info || !radioStatusEl) return;
  _lastRadio = info;

  const setValue = (el, text, kind) => {
    if (!el) return;
    el.textContent = text;
    el.className = 'kv-value' + (kind ? ' is-' + kind : '');
  };

  radioPortEl.textContent = info.port || '--';
  radioBaudEl.textContent = info.baud ? `${info.baud} bps` : '--';

  const bytes = Number(info.sent_bytes) || 0;
  radioSentEl.textContent = formatBytes(bytes);
  const rate = Number(info.rate_bps) || 0;
  radioRateEl.textContent = rate > 0 ? `${formatBytes(rate)}/s` : '--';
  radioReconnectsEl.textContent = String(Number(info.reconnects) || 0);

  if (!info.enabled) {
    setValue(radioStatusEl, I18N.t('radio.disabled'), 'warning');
    if (radioHintEl) radioHintEl.textContent = I18N.t('radio.disabled_hint');
  } else if (!info.port) {
    setValue(radioStatusEl, I18N.t('radio.no_port'), 'warning');
    if (radioHintEl) radioHintEl.textContent = I18N.t('radio.no_port_hint');
  } else if (info.connected) {
    setValue(radioStatusEl, I18N.t('radio.sending'), 'success');
    if (radioHintEl) radioHintEl.textContent = I18N.t('radio.sending_hint', { port: info.port });
  } else if (info.running && !info.connected) {
    setValue(radioStatusEl, I18N.t('radio.reconnecting'), 'warning');
    if (radioHintEl) {
      radioHintEl.textContent = info.last_error
        ? I18N.t('radio.reconnecting_hint', { error: info.last_error })
        : I18N.t('radio.reconnecting_hint_noerr');
    }
  } else {
    setValue(radioStatusEl, I18N.t('radio.not_run'), 'warning');
    if (radioHintEl) radioHintEl.textContent = I18N.t('radio.not_run_hint');
  }
}

// 处理断开连接
function handleDisconnection() {
  if (reconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
    reconnectAttempts++;
    const delay = RECONNECT_DELAY * reconnectAttempts;
    addLog(I18N.t('ws.disconnected', { sec: Math.round(delay/1000), cur: reconnectAttempts, max: MAX_RECONNECT_ATTEMPTS }), 'warning');
    loadingIndicator.classList.remove('hidden');
    loadingIndicator.innerHTML = `
      <div class="card-pad empty-state is-warning">
        <i class="fa fa-refresh spin" aria-hidden="true"></i>
        <span>${I18N.t('ws.disconnected_retry', { sec: Math.round(delay/1000), cur: reconnectAttempts, max: MAX_RECONNECT_ATTEMPTS })}</span>
      </div>
    `;
    
    setTimeout(initWebSocket, delay);
  } else {
    loadingIndicator.classList.remove('hidden');
    addLog(I18N.t('ws.cannot_reconnect'), 'error');
    loadingIndicator.innerHTML = `
      <div class="card-pad empty-state is-error">
        <i class="fa fa-exclamation-triangle" aria-hidden="true"></i>
        <span>${I18N.t('ws.cannot_reconnect')}</span>
      </div>
    `;
  }
}

// 更新时间显示
function updateTimeDisplay() {
  const now = new Date();
  updateTimeEl.textContent = `${I18N.t('ws.last_update')}: ${now.toLocaleString()}`;
}

// 更新观测星座显示
function updateObservedSystemsDisplay() {
  if (observedSystems.size === 0) {
    observedSystemsEl.textContent = I18N.t('sat.no_data');
    return;
  }
  
  // 将Set转换为数组
  const systemsArray = Array.from(observedSystems);
  
  // 映射为RTCM标准名称并连接
  const displayText = systemsArray.map(sys => {
    // 北斗系统特殊处理
    if (sys === 'BDS') return 'BDS';
    return gnssAbbrMap[sys] || sys;
  }).join('+');
  
  observedSystemsEl.textContent = displayText;
}

// 更新统计信息
function updateStatistics(gnss, cellData) {
  // 仅在新系统首次出现时增加计数
  if (!allSatellites[gnss]) {
    systemCount++;
    systemCountEl.textContent = systemCount;
  }

  allSatellites[gnss] = cellData;

  // 卫星总数按 (星座, PRN) 去重
  // 原实现是把各 MSM 消息号（1074/1075/1077…）的 sats 相加，
  // 同一星座同时播发多种 MSM 时，同一颗卫星会被重复计数 2~3 次。
  const uniqueSatelliteIds = new Set();
  Object.entries(allSatellites).forEach(([system, satellites]) => {
    if (!satellites) return;
    satellites.forEach(sat => {
      if (sat && sat.CELLPRN !== undefined) {
        uniqueSatelliteIds.add(`${system}_${sat.CELLPRN}`);
      }
    });
  });
  satelliteCount = uniqueSatelliteIds.size;
  satelliteCountEl.textContent = satelliteCount;

  // 使用Set确保卫星唯一性
  const strongSatelliteIds = new Set();
  
  // 遍历所有系统
  Object.entries(allSatellites).forEach(([system, satellites]) => {
    if (!satellites) return;
    
    // 遍历系统内的所有卫星
    satellites.forEach(sat => {
      if (!sat) return;
      // 获取信号强度（优先使用DF408，否则使用其他字段）
      const strength = sat.DF408 ?? sat.DF403 ?? sat.DF409 ?? sat.DF405 ?? 0;
      
      // 信号强度超过阈值视为强信号
      if (strength >= STRONG_SIGNAL_DB_HZ) {
        const satId = `${system}_${sat.CELLPRN}`;
        strongSatelliteIds.add(satId);
      }
    });
  });
  
  strongSignalCount = strongSatelliteIds.size;
  strongSignalCountEl.textContent = strongSignalCount;
}

// 计算信号条的样式（高度 + 颜色）
function computeBarStyle(strength) {
  let barColor = '#86909C'; // 默认灰色
  if (strength >= BAR_GREEN_DB_HZ) barColor = '#00B42A';
  else if (strength >= BAR_AMBER_DB_HZ) barColor = '#FF7D00';
  else if (strength >= BAR_RED_DB_HZ) barColor = '#F53F3F';

  const maxHeight = 120;
  let barHeight = Math.min(Math.round((strength / 100) * maxHeight), maxHeight);
  barHeight = Math.max(barHeight, 4); // 最小高度
  return { height: barHeight, color: barColor };
}

function getSignalStrength(cell) {
  return cell.DF408 ?? cell.DF403 ?? cell.DF409 ?? cell.DF405 ?? 0;
}

// 创建单个信号条 DOM 节点
function createSignalBarNode(cell, gnss, freqMap) {
  const prn = cell.CELLPRN;
  const sig = cell.CELLSIG;
  // 使用传入的gnss参数（不是cell.GNSS）查询频率信息
  const freqInfo = freqMap?.[gnss]?.[sig] || { band: I18N.t('sig.unknown'), freq: I18N.t('sig.unknown') };

  const bar = document.createElement('div');
  bar.className = 'signal-bar';
  bar.dataset.key = `${prn}_${sig}`;
  bar.innerHTML = `
      <span class="bar-band"></span>
      <span class="bar-prn"></span>
  `;
  bar.querySelector('.bar-band').textContent = freqInfo.band;
  bar.querySelector('.bar-prn').textContent = prn;
  return bar;
}

// 更新单个信号条的动态部分
function updateSignalBarNode(bar, cell, gnss, freqMap) {
  const prn = cell.CELLPRN;
  const sig = cell.CELLSIG;
  const strength = getSignalStrength(cell);
  const freqInfo = freqMap?.[gnss]?.[sig] || { band: I18N.t('sig.unknown'), freq: I18N.t('sig.unknown') };
  const style = computeBarStyle(strength);

  bar.style.height = `${style.height}px`;
  // 只给纯色，顶部高光和圆角交给 .signal-bar 的 CSS，切换主题/配色不用再动 JS
  bar.style.backgroundColor = style.color;
  bar.title = [
    `${I18N.t('sig.band')}: ${sig}`,
    `${I18N.t('sig.freq_band')}: ${freqInfo.band}`,
    `${I18N.t('sig.freq')}: ${freqInfo.freq} MHz`,
    `${I18N.t('sig.strength')}: ${strength}dBHz `,
    `${I18N.t('sig.system')}: ${gnss}`,
    `${I18N.t('sig.prn')}: ${prn}`
  ].join('\n').trim();

  const bandEl = bar.querySelector('.bar-band');
  if (bandEl) bandEl.textContent = freqInfo.band;
}

// 增量渲染信号条：复用已有 DOM 节点，只更新高度/颜色
// 原实现每次都重建整段 innerHTML，MSM 高频播发时会整块闪烁并反复触发重排。
function renderSignalBars(container, cells, gnss, freqMap) {
  if (!container || !Array.isArray(cells)) return;

  const valid = cells.filter(cell => cell && cell.CELLPRN && cell.CELLSIG);
  const sorted = valid.slice().sort((a, b) => a.CELLPRN - b.CELLPRN);

  const existing = new Map();
  Array.from(container.children).forEach(el => {
    if (el.dataset && el.dataset.key) existing.set(el.dataset.key, el);
  });

  const usedKeys = new Set();
  sorted.forEach(cell => {
    const key = `${cell.CELLPRN}_${cell.CELLSIG}`;
    usedKeys.add(key);
    let bar = existing.get(key);
    if (!bar) {
      bar = createSignalBarNode(cell, gnss, freqMap);
    }
    updateSignalBarNode(bar, cell, gnss, freqMap);
    // appendChild 对已有节点是"移动"，顺带完成按 PRN 排序
    container.appendChild(bar);
  });

  // 移除本轮不再出现的频点
  Array.from(container.children).forEach(el => {
    const key = el.dataset ? el.dataset.key : undefined;
    if (key && !usedKeys.has(key)) el.remove();
  });
}


// 定期发送心跳
setInterval(() => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    try {
      ws.send(JSON.stringify({ type: "heartbeat" }));
    } catch (e) {
      console.warn("发送心跳失败:", e);
    }
  }
}, 15000); // 每15秒发送一次心跳

// 监听页面可见性变化
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    console.log("页面变为可见状态，检查连接");
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      console.log("尝试重新连接");
      initWebSocket();
    } else {
      // 发送测试消息确认连接
      try {
        ws.send(JSON.stringify({ type: "visibility_check" }));
      } catch (e) {
        console.log("连接可能已断开，尝试重新连接");
        initWebSocket();
      }
    }
  }
});
// 捐赠面板：桌面端靠 :hover 展开，触屏（手簿/平板）没有 hover，必须支持点按开合。
(function () {
  const donate = document.querySelector('.donate');
  const btn = donate && donate.querySelector('.donate-btn');
  if (!donate || !btn) return;

  const toggle = (open) => {
    donate.classList.toggle('is-open', open);
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  };

  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    toggle(!donate.classList.contains('is-open'));
  });

  // 点击面板外部关闭（仅点按态需要，悬停态不受影响）
  document.addEventListener('click', (e) => {
    if (donate.classList.contains('is-open') && !donate.contains(e.target)) {
      toggle(false);
    }
  });

  // Esc 关闭
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && donate.classList.contains('is-open')) {
      toggle(false);
      btn.focus();
    }
  });
})();

// ============================================================================
// 语言切换：切换后立即按新语言重渲染所有动态文案（静态 data-i18n 已由 i18n.js 处理）
// ============================================================================
document.addEventListener('langchange', () => {
  // 静态运行状态（模式/数据源/目标 caster）
  if (_lastConfig) updateStaticRunStatus(_lastConfig);
  // 上传端 / 本地 Caster / 数传透传 状态
  if (_lastUpload) updateUploadStatus(_lastUpload);
  if (_lastLocalCaster) updateLocalCaster(_lastLocalCaster);
  if (_lastRadio) updateRadioStatus(_lastRadio);
  // 观测星座的空态文案
  updateObservedSystemsDisplay();
  // 地图标记文字（如果地图已初始化且有位置）
  if (stationPosition) {
    updateMarker(stationPosition.lat, stationPosition.lon, dataSourceEl.textContent || I18N.t('map.marker'));
  }
});
