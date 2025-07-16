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
const amapBtn = document.getElementById('amap-btn');
const osmBtn = document.getElementById('osm-btn');

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
const msmSatsMap = {}; // 记录每个 MSM 消息类型对应的 sats 值
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
  activeBtn.classList.add('bg-primary', 'text-white');
  activeBtn.classList.remove('bg-gray-300', 'text-gray-700');

  inactiveBtn.disabled = false;
  inactiveBtn.classList.add('bg-gray-300', 'text-gray-700');
  inactiveBtn.classList.remove('bg-primary', 'text-white');
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
      updateMarker(stationPosition.lat, stationPosition.lon, dataSourceEl.textContent || "基准站");
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
      console.log("地图加载超时，尝试OpenStreetMap");
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
    
    // 监听瓦片错误事件
    map.on('tileerror', (event) => {
      console.error("地图瓦片加载错误:", event);
      // 如果错误超过5次，切换到OpenStreetMap
      if (mapSourceType === 'amap') {
        console.log("切换到OpenStreetMap");
        tryInitMap('osm', mapConfig);
      }
    });
    
  } catch (error) {
    console.error("高德地图初始化失败:", error);
    throw error; // 抛出异常以便上层处理
  }
}

// 初始化OpenStreetMap（修改）
function initOsmMap(mapConfig) {
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
          source: new ol.source.OSM()
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
    
    console.log("使用OpenStreetMap初始化成功");
    
  } catch (error) {
    console.error("OpenStreetMap初始化失败:", error);
    // 显示错误信息
    mapContainer.innerHTML = `
      <div class="map-placeholder flex flex-col items-center justify-center h-full">
        <i class="fa fa-exclamation-triangle text-danger text-4xl mb-3"></i>
        <h4 class="font-orbitron text-danger mb-1">地图初始化失败</h4>
        <p class="text-sm text-primary">${error.message}</p>
        <p class="text-xs mt-2">无法加载地图服务</p>
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
    name: "基准站"
  });

  // 设置标记样式，包括图标和文字标注
  feature.setStyle(new ol.style.Style({
    image: new ol.style.Icon({
      src: 'https://cdn.jsdelivr.net/gh/pointhi/leaflet-color-markers@master/img/marker-icon-2x-red.png',
      scale: 0.5,
      anchor: [0.5, 1]
    }),
    text: new ol.style.Text({
      text: "基准站",
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
  ws = new WebSocket("ws://" + location.host + "/ws");
  
  ws.onopen = () => {
    console.log("WebSocket连接成功");
    reconnectAttempts = 0; // 重置重连计数
    clearInterval(reconnectInterval);
    
    // 更新加载指示器
    loadingIndicator.innerHTML = `
      <div class="inline-block relative">
        <i class="fa fa-satellite text-primary text-3xl satellite-glow animate-spin" style="animation-duration: 8s"></i>
      </div>
      <p>正在接收RTCM数据...</p>
    `;
    
    // 重置观测系统集合
    observedSystems.clear();
    updateObservedSystemsDisplay();
    
    // 重置系统计数
    systemCount = 0;
    systemCountEl.textContent = systemCount;
    
    // 请求配置信息
    fetch('/config')
      .then(response => response.json())
      .then(config => {
        // 更新静态运行状态
        updateStaticRunStatus(config);
        
        // 使用默认配置初始化地图
        initMap({
          default_lat: 31.643763,  // 默认纬度
          default_lon: 105.466293, // 默认经度
          default_zoom: 10        // 默认缩放级别
        });
      })
      .catch(error => {
        console.error("获取配置失败:", error);
        // 使用默认配置初始化地图
        initMap({
          default_lat: 31.643763,
          default_lon: 105.466293,
          default_zoom: 10
        });
      });
  };

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      console.log("收到WebSocket数据：", data);

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
        rtcmTypeListEl.innerHTML = `${frequencyList}`;
      }

      // 更新运行状态
      if (data.run_status) {
        updateRunStatus(data.run_status);
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
      updateStatistics(gnss, data.cell_data, data.identity, data.sats);

      let block = document.getElementById(blockId);
      const isNewCard = !block;
      
      if (isNewCard) {
        block = document.createElement("div");
        block.id = blockId;
        block.className = "space-card rounded-xl p-4 glow-border fade-in";
        
        // 卡片背景使用透明深色
        block.innerHTML = `
          <div class="flex justify-between items-center mb-4">
            <div class="flex items-center">
              <span class="${gnssIcon} rounded mr-3" style="width: 1.5em; display: inline-block"></span>
              <h3 class="text-lg font-semibold text-white">${gnss} </h3>
            </div>
            <span class="signal-count text-sm px-2 py-1 bg-${gnssColor}/10 text-${gnssColor} rounded-full">
             观测频段信号数量：${data.cell_data.length}
            </span>
          </div>
          <div class="fixed-height-signal-bars signal-bars space-x-1 pb-2 pt-8 scrollbar-hide" style="background-color: rgba(10, 20, 40, 0.5);"></div>
        `;
        
        // 确保卡片被添加到容器中
        container.appendChild(block);
      } else {
        // 更新现有卡片的卫星数量显示
        const countSpan = block.querySelector('.signal-count');
        if (countSpan) {
          countSpan.textContent = `观测频段信号数量：${data.cell_data.length}`;
        }
      }

      const signalBarsEl = block.querySelector(".signal-bars");
      const sortedCellData = data.cell_data.sort((a, b) => a.CELLPRN - b.CELLPRN);
      signalBarsEl.innerHTML = sortedCellData.map(cell => createSignalBar(cell, gnssColor, gnss, gnssSignalFreqMap)).join("");
    } catch (error) {
      console.error("处理WebSocket数据出错:", error);
    }
  };

  ws.onerror = (error) => {
    console.error("WebSocket错误:", error);
    handleDisconnection();
  };

  ws.onclose = (event) => {
    console.log("WebSocket连接关闭，代码:", event.code, "原因:", event.reason);
    handleDisconnection();
  };
}

// 更新静态运行状态
function updateStaticRunStatus(config) {
  // 更新运行模式显示
  runModeEl.textContent = config.general.mode === 'serial' ? '串口模式' : '中继模式';

  // 根据模式显示数据源
  if (config.general.mode === 'serial') {
    const port = config.serial.port || "自动检测";
    const baud = config.serial.baud_rate || "自动检测";
    dataSourceEl.textContent = `${port} @ ${baud}bps`;
  } else {
    const host = config.relay.host || "未配置";
    const port = config.relay.port || "未配置";
    const mountpoint = config.relay.mountpoint || "未配置";
    dataSourceEl.textContent = `${host}:${port}/${mountpoint}`;
  }
  
  // 显示目标caster
  const ntripHost = config.ntrip.host || "未配置";
  const ntripPort = config.ntrip.port || "未配置";
  const ntripMountpoint = config.ntrip.mountpoint || "未配置";
  targetCasterEl.textContent = `${ntripHost}:${ntripPort}/${ntripMountpoint}`;
}

// 更新运行状态
function updateRunStatus(status) {
  if (status.mode) runModeEl.textContent = status.mode;
  if (status.data_source) dataSourceEl.textContent = status.data_source;
  if (status.target_caster) targetCasterEl.textContent = status.target_caster;
  if (status.data_sent) dataSentEl.textContent = status.data_sent;
  if (status.run_time) runTimeEl.textContent = status.run_time;
}

// 处理断开连接
function handleDisconnection() {
  if (reconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
    reconnectAttempts++;
    const delay = RECONNECT_DELAY * reconnectAttempts;
    
    loadingIndicator.classList.remove('hidden');
    loadingIndicator.innerHTML = `
      <div class="text-center py-4 text-warning">
        <i class="fa fa-refresh fa-spin text-xl mb-3"></i>
        <p>连接断开，${Math.round(delay/1000)}秒后尝试重连 (${reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS})</p>
      </div>
    `;
    
    setTimeout(initWebSocket, delay);
  } else {
    loadingIndicator.classList.remove('hidden');
    loadingIndicator.innerHTML = `
      <div class="text-center py-4 text-danger">
        <i class="fa fa-exclamation-triangle text-3xl mb-3"></i>
        <p>无法重新连接，请刷新页面</p>
      </div>
    `;
  }
}

// 更新时间显示
function updateTimeDisplay() {
  const now = new Date();
  updateTimeEl.textContent = `最后更新: ${now.toLocaleString()}`;
}

// 更新观测星座显示
function updateObservedSystemsDisplay() {
  if (observedSystems.size === 0) {
    observedSystemsEl.textContent = "暂无数据";
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
function updateStatistics(gnss, cellData, identity, sats) {
  // 仅在新系统首次出现时增加计数
  if (!allSatellites[gnss]) {
    systemCount++;
    systemCountEl.textContent = systemCount;
  }

  allSatellites[gnss] = cellData;

  // 更新 msmSatsMap 中对应消息类型的 sats 值
  if (identity) {
    msmSatsMap[identity] = sats;
  }

  // 重新计算所有 MSM 消息类型的 sats 值总和
  satelliteCount = Object.values(msmSatsMap).reduce((sum, value) => sum + value, 0);
  satelliteCountEl.textContent = satelliteCount;

  // 使用Set确保卫星唯一性
  const strongSatelliteIds = new Set();
  
  // 遍历所有系统
  Object.entries(allSatellites).forEach(([system, satellites]) => {
    if (!satellites) return;
    
    // 遍历系统内的所有卫星
    satellites.forEach(sat => {
      // 获取信号强度（优先使用DF408，否则使用其他字段）
      const strength = sat.DF408 ?? sat.DF403 ?? sat.DF409 ?? sat.DF405 ?? 0;
      
      // 信号强度超过35视为强信号
      if (strength > 35) {
        const satId = `${system}_${sat.CELLPRN}`;
        strongSatelliteIds.add(satId);
      }
    });
  });
  
  strongSignalCount = strongSatelliteIds.size;
  strongSignalCountEl.textContent = strongSignalCount;
}

// 创建信号条
function createSignalBar(cell, gnssColor, gnss, freqMap) {
  // 数据验证
  if (!cell || !cell.CELLPRN || !cell.CELLSIG) {
    console.error("无效的cell数据:", cell);
    return "";
  }

  const prn = cell.CELLPRN;
  const sig = cell.CELLSIG;
  const strength = cell.DF408 ?? cell.DF403 ?? cell.DF409 ?? cell.DF405 ?? 0;

  // 使用传入的gnss参数（不是cell.GNSS）查询频率信息
  const freqInfo = freqMap?.[gnss]?.[sig] || {
    band: '未知',
    freq: '未知'
  };

  // 设置柱条颜色
  let barColor = '#86909C'; // 默认灰色
  if (strength > 45) barColor = '#00B42A';
  else if (strength > 30) barColor = '#FF7D00';
  else if (strength > 15) barColor = '#F53F3F';

  const maxHeight = 120;
  let barHeight = Math.min(Math.round((strength / 100) * maxHeight), maxHeight);
  barHeight = Math.max(barHeight, 4); // 最小高度

  // 构建Tooltip文本
  const tooltip = `
信号类型: ${sig}
频段: ${freqInfo.band}
频率: ${freqInfo.freq} MHz
信号强度: ${strength}dBHz 
所属系统: ${gnss}
卫星PRN: ${prn}
  `.trim();

  return `
    <div class="signal-bar inline-block rounded-t-md relative"
         style="height: ${barHeight}px; width: 40px;
                background: linear-gradient(to top, ${barColor}, rgba(255,255,255,0.3))"
         title="${tooltip}">
      <div class="text-xs text-center absolute bottom-full left-0 right-0 mb-1 text-white">${freqInfo.band}</div>
      <div class="text-[9px] text-center absolute top-full left-0 right-0 mt-1 font-medium text-white">${prn}</div>
    </div>
  `;
}


// 定期发送心跳
setInterval(() => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    try {
      ws.send(JSON.stringify({ type: "heartbeat" }));
      console.log("发送心跳包");
    } catch (e) {
      console.error("发送心跳失败:", e);
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