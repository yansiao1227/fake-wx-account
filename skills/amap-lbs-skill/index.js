const fs = require('fs');
const path = require('path');
const axios = require('axios');

// 配置文件路径
const CONFIG_FILE = path.join(__dirname, 'config.json');

const COORD_RE = /^\s*-?\d+(\.\d+)?\s*,\s*-?\d+(\.\d+)?\s*$/;

/**
 * 读取配置文件
 */
function readConfig() {
  try {
    if (fs.existsSync(CONFIG_FILE)) {
      const data = fs.readFileSync(CONFIG_FILE, 'utf8');
      return JSON.parse(data);
    }
  } catch (error) {
    console.error('读取配置文件失败:', error.message);
  }
  return {};
}

/**
 * 保存配置文件
 */
function saveConfig(config) {
  try {
    fs.writeFileSync(CONFIG_FILE, JSON.stringify(config, null, 2), 'utf8');
    // 设置文件权限为仅所有者可读写，防止密钥泄露
    try {
      fs.chmodSync(CONFIG_FILE, 0o600);
    } catch (_) {
      // Windows 等环境可能不支持 chmod，忽略
    }
    console.log('配置已保存到:', CONFIG_FILE);
    return true;
  } catch (error) {
    console.error('保存配置文件失败:', error.message);
    return false;
  }
}

/**
 * 获取高德 Web Service Key
 */
function getWebServiceKey() {
  const config = readConfig();
  return config.webServiceKey || null;
}

/**
 * 默认城市：环境变量 AMAP_DEFAULT_CITY > config.json defaultCity
 */
function getDefaultCity() {
  const fromEnv = (process.env.AMAP_DEFAULT_CITY || process.env.AMAP_CITY || '').trim();
  if (fromEnv) return normalizeCityName(fromEnv);
  const config = readConfig();
  if (config.defaultCity) return normalizeCityName(String(config.defaultCity));
  return '';
}

/**
 * 设置高德 Web Service Key
 */
function setWebServiceKey(key) {
  const config = readConfig();
  config.webServiceKey = key;
  return saveConfig(config);
}

/**
 * 检查并提示用户输入 Key
 */
async function ensureWebServiceKey() {
  // 优先从环境变量读取
  let key = process.env.AMAP_WEBSERVICE_KEY;

  if (!key && process.env.AMAP_KEY) {
    key = process.env.AMAP_KEY;
    console.warn('⚠️  环境变量 AMAP_KEY 已废弃，请迁移到 AMAP_WEBSERVICE_KEY');
  }

  if (!key) {
    // 尝试从配置文件读取
    key = getWebServiceKey();
  }

  if (!key) {
    console.log('\n⚠️  未找到高德 Web Service Key');
    console.log('请访问以下地址创建应用并获取 Key:');
    console.log('https://lbs.amap.com/api/webservice/create-project-and-key\n');
    throw new Error('请设置环境变量 AMAP_WEBSERVICE_KEY 或提供高德 Web Service Key');
  }

  return key;
}

function logInfo(silent, ...args) {
  if (!silent) console.log(...args);
}

function logError(silent, ...args) {
  if (!silent) console.error(...args);
}

/**
 * 是否为「经度,纬度」坐标字符串
 */
function isCoordinate(value) {
  if (!value || typeof value !== 'string') return false;
  if (!COORD_RE.test(value)) return false;
  const [lng, lat] = value.split(',').map((x) => Number(x.trim()));
  return Number.isFinite(lng) && Number.isFinite(lat) && Math.abs(lng) <= 180 && Math.abs(lat) <= 90;
}

/**
 * 规范化坐标字符串
 */
function normalizeCoordinate(value) {
  const [lng, lat] = value.split(',').map((x) => Number(x.trim()));
  return `${lng},${lat}`;
}

/**
 * 球面距离（米）
 */
function distanceMeters(a, b) {
  const [lng1, lat1] = a.split(',').map(Number);
  const [lng2, lat2] = b.split(',').map(Number);
  const toRad = (d) => (d * Math.PI) / 180;
  const R = 6371000;
  const dLat = toRad(lat2 - lat1);
  const dLng = toRad(lng2 - lng1);
  const x =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(x));
}

/**
 * 规范化城市名：去「市/地区」等后缀，保留「州」自治州需特殊处理
 */
function normalizeCityName(name) {
  if (!name) return '';
  let s = String(name).trim();
  if (!s || s === '[]') return '';
  // 高德有时返回数组序列化
  if (s.startsWith('[') && s.endsWith(']')) return '';
  s = s.replace(/选$/u, '');
  // 直辖市/常见简称
  s = s
    .replace(/特别行政区$/u, '')
    .replace(/壮族自治区|回族自治区|维吾尔自治区|自治区$/u, '')
    .replace(/(市|地区|盟)$/u, '');
  // 「XX自治州」保留到州名主体，如 延边朝鲜族自治州 → 延边
  s = s.replace(/(.+?)(朝鲜族|藏族|彝族|回族|苗族|壮族|蒙古族|侗族|土家族|哈萨克|傣族)?自治州$/u, '$1');
  return s.trim();
}

/** 常见城市/地级行政区关键词（用于从自然语言中抽取） */
const KNOWN_CITIES = [
  // 直辖市 & 常用简称
  '北京', '上海', '天津', '重庆',
  // 省会与热门城市（按长度长的优先匹配，避免「吉林市」被「吉林」等冲突时依赖排序）
  '哈尔滨', '齐齐哈尔', '石家庄', '呼和浩特', '乌鲁木齐', '克拉玛依',
  '连云港', '秦皇岛', '张家口', '牡丹江', '佳木斯', '平顶山', '焦作',
  '广州', '深圳', '佛山', '东莞', '珠海', '惠州', '中山', '汕头', '湛江', '江门',
  '杭州', '宁波', '温州', '嘉兴', '绍兴', '金华', '台州', '湖州', '丽水', '衢州', '舟山',
  '南京', '苏州', '无锡', '常州', '南通', '徐州', '扬州', '盐城', '泰州', '镇江', '淮安', '宿迁',
  '成都', '绵阳', '德阳', '宜宾', '南充', '泸州', '乐山', '达州', '内江',
  '武汉', '宜昌', '襄阳', '荆州', '黄冈', '十堰', '孝感',
  '西安', '咸阳', '宝鸡', '渭南', '汉中', '延安',
  '郑州', '洛阳', '开封', '南阳', '新乡', '许昌', '安阳', '商丘',
  '长沙', '株洲', '湘潭', '岳阳', '常德', '衡阳', '郴州',
  '济南', '青岛', '烟台', '潍坊', '临沂', '淄博', '济宁', '威海', '泰安', '日照', '德州',
  '福州', '厦门', '泉州', '漳州', '莆田', '龙岩', '三明', '南平', '宁德',
  '合肥', '芜湖', '蚌埠', '阜阳', '安庆', '马鞍山', '黄山',
  '南昌', '九江', '赣州', '上饶', '宜春', '吉安',
  '昆明', '大理', '丽江', '曲靖', '玉溪',
  '贵阳', '遵义', '安顺',
  '南宁', '桂林', '柳州', '北海', '玉林',
  '海口', '三亚', '儋州',
  '沈阳', '大连', '鞍山', '锦州', '营口', '丹东',
  '长春', '吉林', '延边',
  '太原', '大同', '运城', '临汾',
  '兰州', '天水', '酒泉',
  '西宁', '银川', '拉萨',
  '香港', '澳门',
  '保定', '唐山', '廊坊', '邯郸', '沧州', '邢台', '承德', '衡水',
  '无锡', '常熟', // 县级常写入口
  '嘉定', '松江', '浦东', '徐汇', '长宁', '静安', '黄浦', '虹口', '杨浦', '普陀', '闵行', '宝山', '青浦', '奉贤', '金山', // 上海区划提示 → 映射到上海
  '朝阳', '海淀', '丰台', '通州', '昌平', '大兴' // 北京区划弱提示
];

// 区划名 → 所属城市（弱提示，需结合其它证据）
const DISTRICT_TO_CITY = {
  嘉定: '上海', 松江: '上海', 浦东: '上海', 徐汇: '上海', 长宁: '上海', 静安: '上海',
  黄浦: '上海', 虹口: '上海', 杨浦: '上海', 普陀: '上海', 闵行: '上海', 宝山: '上海',
  青浦: '上海', 奉贤: '上海', 金山: '上海', 崇明: '上海',
  朝阳: '北京', 海淀: '北京', 丰台: '北京', 通州: '北京', 昌平: '北京', 大兴: '北京',
  西城: '北京', 东城: '北京', 石景山: '北京', 顺义: '北京', 房山: '北京'
};

const KNOWN_CITIES_SORTED = [...KNOWN_CITIES].sort((a, b) => b.length - a.length);

/**
 * 从文本中抽取城市名（不调用 API）
 */
function extractCityFromText(text) {
  if (!text || isCoordinate(text)) return '';
  const s = String(text).trim();

  // 「上海市xxx」「杭州市yyy」
  const mCity = s.match(/([\u4e00-\u9fa5]{2,12}?)市/);
  if (mCity) {
    const n = normalizeCityName(mCity[1] + '市');
    if (n) return n;
  }

  // 已知城市列表（长词优先）
  for (const c of KNOWN_CITIES_SORTED) {
    if (s.includes(c)) {
      if (DISTRICT_TO_CITY[c]) return DISTRICT_TO_CITY[c];
      // 「吉林」可能指省，若带「市」已在上面处理；单独「吉林市」含吉林
      return normalizeCityName(c);
    }
  }

  return '';
}

function uniqueStrings(list) {
  const out = [];
  const seen = new Set();
  for (const x of list) {
    const n = normalizeCityName(x);
    if (!n || seen.has(n)) continue;
    seen.add(n);
    out.push(n);
  }
  return out;
}

/**
 * 从 POI/地址字段猜城市
 */
function cityFromPoiLike(obj) {
  if (!obj) return '';
  const raw =
    obj.cityname ||
    obj.city ||
    (obj.pname && /市$/.test(obj.pname) ? obj.pname : '') ||
    '';
  let c = normalizeCityName(raw);
  if (c) return c;
  // 从 formatted_address / address 再抽
  return extractCityFromText(obj.formatted_address || obj.address || obj.name || '');
}

/**
 * 根据多个地点文本/坐标推断城市。
 * 优先级（由 resolveCityContext 编排）：
 *   显式 city → 文本抽取 → 坐标逆地理 → POI 投票(多地交叉) → 默认城市
 *
 * @returns {Promise<{
 *   city: string,
 *   source: 'explicit'|'text'|'regeo'|'poi'|'default'|'none',
 *   confidence: 'high'|'medium'|'low'|'none',
 *   detail: string,
 *   candidates: string[],
 *   usedDefault: boolean
 * }>}
 */
async function resolveCityContext(options = {}) {
  const silent = !!options.silent;
  const places = (options.places || []).map((p) => (p == null ? '' : String(p).trim())).filter(Boolean);
  const explicit = normalizeCityName(options.city || '');
  const defaultCity = normalizeCityName(
    options.defaultCity != null && options.defaultCity !== ''
      ? options.defaultCity
      : getDefaultCity()
  );

  if (explicit) {
    return {
      city: explicit,
      source: 'explicit',
      confidence: 'high',
      detail: '使用调用方显式指定的城市',
      candidates: [explicit],
      usedDefault: false
    };
  }

  // 1) 文本直接抽取
  const textCities = [];
  for (const p of places) {
    const c = extractCityFromText(p);
    if (c) textCities.push(c);
  }
  const textUnique = uniqueStrings(textCities);
  if (textUnique.length === 1) {
    return {
      city: textUnique[0],
      source: 'text',
      confidence: textCities.length >= 2 ? 'high' : 'medium',
      detail: `从地点文本识别到城市「${textUnique[0]}」`,
      candidates: textUnique,
      usedDefault: false
    };
  }
  if (textUnique.length > 1) {
    // 多城文本冲突：先记着，后面用 POI 投票裁决
    logInfo(silent, `ℹ️  文本中出现多个城市候选：${textUnique.join('、')}，继续用 POI 投票确认…`);
  }

  // 2) 坐标逆地理
  const regeoCities = [];
  for (const p of places) {
    if (!isCoordinate(p)) continue;
    try {
      const regeo = await regeocode({ location: normalizeCoordinate(p), silent: true });
      const ac = (regeo && regeo.regeocode && regeo.regeocode.addressComponent) || {};
      let c = normalizeCityName(ac.city) || normalizeCityName(ac.province);
      if (!c && regeo && regeo.regeocode) {
        c = extractCityFromText(regeo.regeocode.formatted_address || '');
      }
      if (c) regeoCities.push(c);
    } catch (_) {
      // ignore
    }
  }
  const regeoUnique = uniqueStrings(regeoCities);
  // 起终点都是坐标且逆地理城市一致 → 高置信
  if (regeoUnique.length === 1 && places.length > 0 && places.every((p) => isCoordinate(p))) {
    return {
      city: regeoUnique[0],
      source: 'regeo',
      confidence: 'high',
      detail: `由坐标逆地理得到城市「${regeoUnique[0]}」`,
      candidates: regeoUnique,
      usedDefault: false
    };
  }

  // 3) POI 投票（不限制城市，看返回结果的 cityname 分布）
  const vote = new Map(); // city -> score
  const perPlaceSets = [];
  const namePlaces = places.filter((p) => !isCoordinate(p));

  for (let i = 0; i < namePlaces.length; i++) {
    const p = namePlaces[i];
    // 终点/靠后地点权重略高（店铺名通常更具区分度）
    const placeWeight = 1 + i * 0.35;
    let pois = [];
    try {
      const result = await searchPOI({
        keywords: p,
        city: '',
        page: 1,
        offset: 10,
        cityLimit: false,
        silent: true
      });
      pois = (result && result.pois) || [];
    } catch (_) {
      pois = [];
    }

    const set = new Set();
    pois.forEach((poi, idx) => {
      let c = cityFromPoiLike(poi);
      if (!c && poi.adname && DISTRICT_TO_CITY[normalizeCityName(poi.adname)]) {
        c = DISTRICT_TO_CITY[normalizeCityName(poi.adname)];
      }
      if (!c) return;
      set.add(c);
      const rankWeight = Math.max(1, 10 - idx);
      vote.set(c, (vote.get(c) || 0) + rankWeight * placeWeight);
    });
    perPlaceSets.push(set);
    await new Promise((r) => setTimeout(r, 80));
  }

  // 并入逆地理选票
  for (const c of regeoCities) {
    vote.set(c, (vote.get(c) || 0) + 12);
  }
  for (const c of textCities) {
    vote.set(c, (vote.get(c) || 0) + 15);
  }

  const ranked = [...vote.entries()].sort((a, b) => b[1] - a[1]);
  const candidateList = ranked.map(([c]) => c);

  // 多地点：优先取交集中的最高票
  if (perPlaceSets.length >= 2) {
    let inter = null;
    for (const s of perPlaceSets) {
      if (!s.size) continue;
      inter = inter == null ? new Set(s) : new Set([...inter].filter((x) => s.has(x)));
    }
    if (inter && inter.size) {
      let best = '';
      let bestScore = -1;
      for (const c of inter) {
        const sc = vote.get(c) || 0;
        if (sc > bestScore) {
          best = c;
          bestScore = sc;
        }
      }
      if (best) {
        return {
          city: best,
          source: 'poi',
          confidence: 'high',
          detail: `多地点 POI 交叉推断为「${best}」（票数 ${bestScore.toFixed(1)}）`,
          candidates: candidateList.slice(0, 5),
          usedDefault: false
        };
      }
    }
  }

  if (ranked.length && ranked[0][1] >= 6) {
    const [best, score] = ranked[0];
    const second = ranked[1] ? ranked[1][1] : 0;
    const confidence = score >= 20 && score >= second * 1.4 ? 'high' : score >= 10 ? 'medium' : 'low';
    // 低置信且有默认城市时，不贸然采用极弱推断
    if (confidence === 'low' && defaultCity && score < 10) {
      // fall through to default
    } else {
      return {
        city: best,
        source: 'poi',
        confidence,
        detail: `根据地点 POI 分布推断为「${best}」（票数 ${score.toFixed(1)}）`,
        candidates: candidateList.slice(0, 5),
        usedDefault: false
      };
    }
  }

  // 4) 默认城市
  if (defaultCity) {
    return {
      city: defaultCity,
      source: 'default',
      confidence: 'low',
      detail: `未能从地点可靠推断城市，使用默认城市「${defaultCity}」（AMAP_DEFAULT_CITY / config.defaultCity）`,
      candidates: uniqueStrings([...candidateList, defaultCity]).slice(0, 5),
      usedDefault: true
    };
  }

  return {
    city: '',
    source: 'none',
    confidence: 'none',
    detail: '无法推断城市且未配置默认城市（可设 AMAP_DEFAULT_CITY 或 config.defaultCity，或传入 --city）',
    candidates: candidateList.slice(0, 5),
    usedDefault: false
  };
}

/**
 * 地理编码：地址/地名 → 坐标
 * @param {Object} params
 * @param {string} params.address - 地址或地名
 * @param {string} [params.city] - 城市约束（强烈建议填写）
 * @param {boolean} [params.silent]
 */
async function geocode(params) {
  const key = await ensureWebServiceKey();
  const silent = !!params.silent;
  const url = 'https://restapi.amap.com/v3/geocode/geo';
  const requestParams = {
    key,
    address: params.address,
    output: 'JSON'
  };
  if (params.city) requestParams.city = params.city;

  try {
    logInfo(silent, `📍 地理编码：${params.address}${params.city ? `（${params.city}）` : ''}`);
    const response = await axios.get(url, { params: requestParams });
    if (response.data.status === '1') {
      return response.data;
    }
    logError(silent, '❌ 地理编码失败:', response.data.info);
    return null;
  } catch (error) {
    logError(silent, '❌ 请求失败:', error.message);
    return null;
  }
}

/**
 * 逆地理编码：坐标 → 地址
 */
async function regeocode(params) {
  const key = await ensureWebServiceKey();
  const silent = !!params.silent;
  const url = 'https://restapi.amap.com/v3/geocode/regeo';
  try {
    const response = await axios.get(url, {
      params: {
        key,
        location: params.location,
        extensions: params.extensions || 'base',
        output: 'JSON'
      }
    });
    if (response.data.status === '1') return response.data;
    logError(silent, '❌ 逆地理编码失败:', response.data.info);
    return null;
  } catch (error) {
    logError(silent, '❌ 请求失败:', error.message);
    return null;
  }
}

/**
 * POI 搜索
 * @param {Object} params - 搜索参数
 * @param {string} params.keywords - 查询关键字
 * @param {string} params.city - 城市名称或城市编码
 * @param {string} params.types - POI类型编码
 * @param {string} params.location - 中心点坐标
 * @param {number} params.radius - 搜索半径(米)
 * @param {number} params.page - 当前页数
 * @param {number} params.offset - 每页记录数
 * @param {boolean} [params.silent]
 */
async function searchPOI(params = {}) {
  const key = await ensureWebServiceKey();
  const silent = !!params.silent;
  const url = 'https://restapi.amap.com/v5/place/text';

  const page = parseInt(params.page, 10) || 1;
  const offset = parseInt(params.offset, 10) || 10;

  const requestParams = {
    key,
    keywords: params.keywords || '',
    region: params.city || params.region || '',
    city_limit: params.cityLimit !== false && params.city_limit !== false,
    page_num: page,
    page_size: Math.min(offset, 25)
  };

  if (params.types) requestParams.types = params.types;
  if (params.location) requestParams.location = params.location;
  if (params.radius) requestParams.radius = params.radius;

  try {
    logInfo(silent, '🔍 正在搜索 POI...');
    const response = await axios.get(url, { params: requestParams });

    if (response.data.status === '1') {
      logInfo(silent, `✅ 搜索成功，共找到 ${response.data.count} 条结果\n`);
      return response.data;
    }
    logError(silent, '❌ 搜索失败:', response.data.info);
    return null;
  } catch (error) {
    logError(silent, '❌ 请求失败:', error.message);
    return null;
  }
}

/**
 * 周边搜索（以坐标为中心）
 * 优先使用 v5 around 接口，失败时回退到 text + location
 */
async function searchAround(params = {}) {
  const key = await ensureWebServiceKey();
  const silent = !!params.silent;
  const location = params.location;
  if (!location || !isCoordinate(location)) {
    throw new Error('searchAround 需要有效的 location 坐标（经度,纬度）');
  }

  const page = parseInt(params.page, 10) || 1;
  const offset = parseInt(params.offset, 10) || 10;
  const radius = parseInt(params.radius, 10) || 1000;

  const url = 'https://restapi.amap.com/v5/place/around';
  const requestParams = {
    key,
    keywords: params.keywords || '',
    location: normalizeCoordinate(location),
    radius,
    page_num: page,
    page_size: Math.min(offset, 25)
  };
  if (params.types) requestParams.types = params.types;
  if (params.city || params.region) requestParams.region = params.city || params.region;

  try {
    logInfo(silent, `📍 正在搜索周边（半径 ${radius}m）...`);
    const response = await axios.get(url, { params: requestParams });
    if (response.data.status === '1') {
      logInfo(silent, `✅ 周边搜索成功，共找到 ${response.data.count} 条结果\n`);
      return response.data;
    }
    logError(silent, '❌ 周边搜索失败:', response.data.info, '，尝试回退 text 搜索');
  } catch (error) {
    logError(silent, '❌ 周边搜索请求失败:', error.message, '，尝试回退 text 搜索');
  }

  return searchPOI({
    keywords: params.keywords,
    city: params.city,
    types: params.types,
    location: normalizeCoordinate(location),
    radius,
    page,
    offset,
    silent
  });
}

/**
 * 将地名或坐标解析为统一地点对象
 * 策略：坐标直通 → POI 搜索（优先）→ 地理编码
 *
 * @returns {Promise<{
 *   ok: boolean,
 *   location?: string,
 *   name?: string,
 *   address?: string,
 *   cityname?: string,
 *   adname?: string,
 *   source?: string,
 *   candidates?: Array,
 *   error?: string
 * }>}
 */
async function resolvePlace(input, options = {}) {
  const silent = !!options.silent;
  const city = options.city || '';
  const limit = parseInt(options.limit, 10) || 5;
  const raw = (input || '').trim();

  if (!raw) {
    return { ok: false, error: '地点不能为空' };
  }

  if (isCoordinate(raw)) {
    const location = normalizeCoordinate(raw);
    let name = location;
    let address = '';
    let cityname = city || '';
    const regeo = await regeocode({ location, silent: true });
    if (regeo && regeo.regeocode) {
      address = regeo.regeocode.formatted_address || '';
      name = address || location;
      const ac = regeo.regeocode.addressComponent || {};
      cityname = ac.city || ac.province || cityname;
      if (Array.isArray(cityname)) cityname = cityname[0] || city;
    }
    return {
      ok: true,
      location,
      name,
      address,
      cityname,
      source: 'coordinate',
      candidates: []
    };
  }

  // 1) POI 搜索（对店铺/地铁站等更准）；空结果时重试一次（缓解偶发限流）
  let pois = [];
  for (let attempt = 0; attempt < 2; attempt++) {
    const poiResult = await searchPOI({
      keywords: raw,
      city,
      page: 1,
      offset: limit,
      cityLimit: !!city,
      silent: true
    });
    pois = (poiResult && poiResult.pois) || [];
    if (pois.length > 0) break;
    if (attempt === 0) {
      await new Promise((r) => setTimeout(r, 200));
    }
  }

  if (pois.length > 0) {
    let selected = pois[0];
    let pickReason = 'top';

    // 若提供参考点，选最近
    if (options.near && isCoordinate(options.near)) {
      let best = selected;
      let bestDist = Infinity;
      for (const p of pois) {
        if (!p.location) continue;
        const d = distanceMeters(options.near, p.location);
        if (d < bestDist) {
          bestDist = d;
          best = p;
        }
      }
      selected = best;
      pickReason = 'nearest';
    }

    const candidates = pois.map((p, idx) => ({
      index: idx + 1,
      name: p.name,
      address: p.address || '',
      location: p.location,
      type: p.type || '',
      adname: p.adname || '',
      cityname: p.cityname || '',
      distance_m:
        options.near && isCoordinate(options.near) && p.location
          ? Math.round(distanceMeters(options.near, p.location))
          : p.distance
            ? Number(p.distance)
            : null
    }));

    // 展示时按距离排序（不改变 selected）
    if (options.near && isCoordinate(options.near)) {
      candidates.sort((a, b) => {
        if (a.distance_m == null) return 1;
        if (b.distance_m == null) return -1;
        return a.distance_m - b.distance_m;
      });
      candidates.forEach((c, i) => {
        c.index = i + 1;
      });
    }

    return {
      ok: true,
      location: selected.location,
      name: selected.name,
      address: selected.address || '',
      cityname: selected.cityname || city,
      adname: selected.adname || '',
      source: `poi:${pickReason}`,
      candidates
    };
  }

  // 2) 地理编码兜底
  const geo = await geocode({ address: raw, city, silent: true });
  let geocodes = (geo && geo.geocodes) || [];
  if (geocodes.length > 0) {
    // 有 city 时过滤到同城/同省
    if (city) {
      const filtered = geocodes.filter(
        (g) =>
          (g.city && String(g.city).includes(city)) ||
          (g.province && String(g.province).includes(city)) ||
          (g.formatted_address && String(g.formatted_address).includes(city))
      );
      if (filtered.length) geocodes = filtered;
    }

    let selected = geocodes[0];
    let pickReason = 'top';

    // 参考点就近（避免「马厂老火锅」落到远端分店）
    if (options.near && isCoordinate(options.near)) {
      let best = selected;
      let bestDist = Infinity;
      for (const g of geocodes) {
        if (!g.location) continue;
        const d = distanceMeters(options.near, g.location);
        if (d < bestDist) {
          bestDist = d;
          best = g;
        }
      }
      selected = best;
      pickReason = 'nearest';
    }

    const candidates = geocodes.slice(0, limit).map((g, idx) => ({
      index: idx + 1,
      name: g.formatted_address,
      address: g.formatted_address,
      location: g.location,
      level: g.level,
      cityname: g.city || g.province || '',
      distance_m:
        options.near && isCoordinate(options.near) && g.location
          ? Math.round(distanceMeters(options.near, g.location))
          : null
    }));
    if (options.near && isCoordinate(options.near)) {
      candidates.sort((a, b) => {
        if (a.distance_m == null) return 1;
        if (b.distance_m == null) return -1;
        return a.distance_m - b.distance_m;
      });
      candidates.forEach((c, i) => {
        c.index = i + 1;
      });
    }

    return {
      ok: true,
      location: selected.location,
      name: selected.formatted_address || raw,
      address: selected.formatted_address || '',
      cityname: selected.city || selected.province || city,
      adname: selected.district || '',
      level: selected.level,
      source: `geocode:${pickReason}`,
      candidates
    };
  }

  return {
    ok: false,
    error: `无法解析地点「${raw}」${city ? `（城市：${city}）` : '，建议指定 --city'}`
  };
}

/**
 * 步行路径规划
 */
async function walkingRoute(params) {
  const key = await ensureWebServiceKey();
  const silent = !!params.silent;
  const url = 'https://restapi.amap.com/v3/direction/walking';

  const requestParams = {
    key,
    origin: params.origin,
    destination: params.destination
  };

  try {
    logInfo(silent, '🚶 正在规划步行路线...');
    const response = await axios.get(url, { params: requestParams });

    if (response.data.status === '1') {
      logInfo(silent, '✅ 步行路线规划成功\n');
      return response.data;
    }
    logError(silent, '❌ 步行路线规划失败:', response.data.info);
    return null;
  } catch (error) {
    logError(silent, '❌ 请求失败:', error.message);
    return null;
  }
}

/**
 * 驾车路径规划
 * @param {Object} params
 * @param {string} [params.extensions] - base|all，默认 all（便于输出 steps）
 */
async function drivingRoute(params) {
  const key = await ensureWebServiceKey();
  const silent = !!params.silent;
  const url = 'https://restapi.amap.com/v3/direction/driving';

  const requestParams = {
    key,
    origin: params.origin,
    destination: params.destination,
    strategy: params.strategy != null ? params.strategy : 10,
    extensions: params.extensions || 'all'
  };

  if (params.waypoints) {
    requestParams.waypoints = params.waypoints;
  }

  try {
    logInfo(silent, '🚗 正在规划驾车路线...');
    const response = await axios.get(url, { params: requestParams });

    if (response.data.status === '1') {
      logInfo(silent, '✅ 驾车路线规划成功\n');
      return response.data;
    }
    logError(silent, '❌ 驾车路线规划失败:', response.data.info);
    return null;
  } catch (error) {
    logError(silent, '❌ 请求失败:', error.message);
    return null;
  }
}

/**
 * 骑行路径规划
 */
async function ridingRoute(params) {
  const key = await ensureWebServiceKey();
  const silent = !!params.silent;
  const url = 'https://restapi.amap.com/v4/direction/bicycling';

  const requestParams = {
    key,
    origin: params.origin,
    destination: params.destination
  };

  try {
    logInfo(silent, '🚴 正在规划骑行路线...');
    const response = await axios.get(url, { params: requestParams });

    if (response.data.errcode === 0) {
      logInfo(silent, '✅ 骑行路线规划成功\n');
      return response.data;
    }
    logError(silent, '❌ 骑行路线规划失败:', response.data.errmsg);
    return null;
  } catch (error) {
    logError(silent, '❌ 请求失败:', error.message);
    return null;
  }
}

/**
 * 公交路径规划
 */
async function transitRoute(params) {
  const key = await ensureWebServiceKey();
  const silent = !!params.silent;
  const url = 'https://restapi.amap.com/v3/direction/transit/integrated';

  const requestParams = {
    key,
    origin: params.origin,
    destination: params.destination,
    city: params.city,
    strategy: params.strategy != null ? params.strategy : 0,
    nightflag: params.nightflag ? 1 : 0,
    extensions: params.extensions || 'all'
  };
  if (params.cityd) requestParams.cityd = params.cityd;

  try {
    logInfo(silent, '🚌 正在规划公交路线...');
    const response = await axios.get(url, { params: requestParams });

    if (response.data.status === '1') {
      logInfo(silent, '✅ 公交路线规划成功\n');
      return response.data;
    }
    logError(silent, '❌ 公交路线规划失败:', response.data.info);
    return null;
  } catch (error) {
    logError(silent, '❌ 请求失败:', error.message);
    return null;
  }
}

/**
 * 生成地图可视化链接
 */
function generateMapLink(mapTaskData) {
  const baseUrl = 'https://a.amap.com/jsapi_demo_show/static/openclaw/travel_plan.html';
  const dataStr = encodeURIComponent(JSON.stringify(mapTaskData));
  return `${baseUrl}?data=${dataStr}`;
}

/**
 * 生成高德客户端导航/搜索链接（不依赖 Web Key）
 */
function generateAmapClientLinks(origin, destination, mode = 'driving') {
  const modeMap = {
    driving: 'car',
    walking: 'walk',
    riding: 'ride',
    bicycling: 'ride',
    transfer: 'bus',
    transit: 'bus'
  };
  const t = modeMap[mode] || 'car';
  // 网页版路径规划
  const dir = `https://uri.amap.com/navigation?from=${encodeURIComponent(origin.location)}&to=${encodeURIComponent(destination.location)}&mode=${t}&coordinate=gaode&callnative=0`;
  const fromName = encodeURIComponent(origin.name || '起点');
  const toName = encodeURIComponent(destination.name || '终点');
  const dirNamed = `https://uri.amap.com/navigation?from=${origin.location},${fromName}&to=${destination.location},${toName}&mode=${t}&coordinate=gaode&callnative=0`;
  return { navigation: dirNamed || dir };
}

function metersToKmText(m) {
  const n = Number(m) || 0;
  if (n < 1000) return `${Math.round(n)} 米`;
  return `${(n / 1000).toFixed(2)} 公里`;
}

function secondsToMinText(s) {
  const n = Number(s) || 0;
  if (n < 60) return `${n} 秒`;
  const min = Math.round(n / 60);
  if (min < 60) return `${min} 分钟`;
  const h = Math.floor(min / 60);
  const r = min % 60;
  return r ? `${h} 小时 ${r} 分钟` : `${h} 小时`;
}

/**
 * 抽取路径 steps 文案
 */
function extractStepsFromRoute(type, data) {
  const steps = [];
  if (!data) return steps;

  if (type === 'walking' || type === 'driving') {
    const path0 = data.route && data.route.paths && data.route.paths[0];
    if (!path0) return steps;
    for (const s of path0.steps || []) {
      steps.push({
        instruction: s.instruction || s.action || '',
        road: s.road || '',
        distance: s.distance,
        duration: s.duration
      });
    }
    return steps;
  }

  if (type === 'riding') {
    const path0 = data.data && data.data.paths && data.data.paths[0];
    if (!path0) return steps;
    for (const s of path0.steps || []) {
      steps.push({
        instruction: s.instruction || '',
        road: s.road || '',
        distance: s.distance,
        duration: s.duration
      });
    }
    return steps;
  }

  if (type === 'transfer') {
    const transit0 = data.route && data.route.transits && data.route.transits[0];
    if (!transit0) return steps;
    for (const seg of transit0.segments || []) {
      const walking = seg.walking;
      if (walking && Number(walking.distance) > 0) {
        steps.push({
          instruction: `步行 ${metersToKmText(walking.distance)}（约 ${secondsToMinText(walking.duration)}）`,
          road: '',
          distance: walking.distance,
          duration: walking.duration,
          kind: 'walk'
        });
      }
      const buslines = (seg.bus && seg.bus.buslines) || [];
      for (const line of buslines) {
        const dep = line.departure_stop && line.departure_stop.name;
        const arr = line.arrival_stop && line.arrival_stop.name;
        const via = line.via_num != null ? `，途经 ${line.via_num} 站` : '';
        steps.push({
          instruction: `乘坐 ${line.name || '公交/地铁'}：${dep || '?'} → ${arr || '?'}${via}`,
          road: line.name || '',
          distance: line.distance,
          duration: line.duration,
          kind: 'transit'
        });
      }
      if (seg.railway && seg.railway.name) {
        steps.push({
          instruction: `乘坐 ${seg.railway.name}`,
          road: seg.railway.name,
          kind: 'railway'
        });
      }
    }
    return steps;
  }

  return steps;
}

function summarizeRoute(type, data) {
  if (!data) return null;

  if (type === 'walking' || type === 'driving') {
    const path0 = data.route && data.route.paths && data.route.paths[0];
    if (!path0) return null;
    return {
      distance: path0.distance,
      duration: path0.duration,
      tolls: path0.tolls,
      traffic_lights: path0.traffic_lights,
      strategy: path0.strategy
    };
  }

  if (type === 'riding') {
    const path0 = data.data && data.data.paths && data.data.paths[0];
    if (!path0) return null;
    return {
      distance: path0.distance,
      duration: path0.duration
    };
  }

  if (type === 'transfer') {
    const transits = (data.route && data.route.transits) || [];
    if (!transits.length) return null;
    const t0 = transits[0];
    return {
      plan_count: transits.length,
      distance: t0.distance,
      duration: t0.duration,
      cost: t0.cost,
      walking_distance: t0.walking_distance
    };
  }

  return null;
}

/**
 * 格式化路线规划结果（给人 / Agent 阅读）
 */
function formatRoutePlanText(plan) {
  if (!plan || !plan.ok) {
    return `❌ 路线规划失败：${(plan && plan.error) || '未知错误'}`;
  }

  const typeLabels = {
    walking: '步行',
    driving: '驾车',
    riding: '骑行',
    transfer: '公交/地铁'
  };
  const label = typeLabels[plan.type] || plan.type;
  const lines = [];

  lines.push(`✅ ${label}路线规划成功`);
  if (plan.city || (plan.cityInfo && plan.cityInfo.city)) {
    const c = plan.city || plan.cityInfo.city;
    const src = plan.cityInfo ? plan.cityInfo.source : '';
    const conf = plan.cityInfo ? plan.cityInfo.confidence : '';
    lines.push(
      `🏙️  城市：${c}${src ? `（${src}${conf && conf !== 'none' ? `, ${conf}` : ''}）` : ''}`
    );
  }
  lines.push('');
  lines.push(`📍 起点：${plan.origin.name}`);
  if (plan.origin.address) lines.push(`   ${plan.origin.address}`);
  lines.push(`   坐标：${plan.origin.location}（来源：${plan.origin.source || '-'}）`);
  lines.push(`🏁 终点：${plan.destination.name}`);
  if (plan.destination.address) lines.push(`   ${plan.destination.address}`);
  lines.push(`   坐标：${plan.destination.location}（来源：${plan.destination.source || '-'}）`);

  if (plan.summary) {
    lines.push('');
    lines.push('📊 路线摘要');
    if (plan.summary.distance != null) lines.push(`   距离：${metersToKmText(plan.summary.distance)}`);
    if (plan.summary.duration != null) lines.push(`   预计时间：${secondsToMinText(plan.summary.duration)}`);
    if (plan.summary.cost != null && plan.summary.cost !== '') lines.push(`   费用：${plan.summary.cost} 元`);
    if (plan.summary.tolls != null && String(plan.summary.tolls) !== '0') {
      lines.push(`   过路费：${plan.summary.tolls} 元`);
    }
    if (plan.summary.traffic_lights != null) lines.push(`   红绿灯：${plan.summary.traffic_lights} 个`);
    if (plan.summary.walking_distance != null) {
      lines.push(`   步行距离：${metersToKmText(plan.summary.walking_distance)}`);
    }
    if (plan.summary.plan_count != null) lines.push(`   方案数：${plan.summary.plan_count}`);
  }

  if (plan.steps && plan.steps.length) {
    lines.push('');
    lines.push('🧭 怎么走');
    plan.steps.forEach((s, i) => {
      const dist = s.distance != null && s.distance !== '' ? `（${metersToKmText(s.distance)}）` : '';
      lines.push(`   ${i + 1}. ${s.instruction}${dist}`);
    });
  }

  // 多候选提示（连锁店等）
  const destCandidates = (plan.destination.candidates || []).filter(
    (c) => c.location !== plan.destination.location
  );
  if (destCandidates.length) {
    lines.push('');
    lines.push('💡 终点存在多个候选（已按就近/默认选择其一）。如需其他门店可指定更全名称：');
    plan.destination.candidates.slice(0, 5).forEach((c) => {
      const d = c.distance_m != null ? `，距起点约 ${metersToKmText(c.distance_m)}` : '';
      lines.push(`   ${c.index}. ${c.name} — ${c.address || ''}${d}`);
    });
  }

  if (plan.mapLink) {
    lines.push('');
    lines.push('🗺️ 地图可视化：');
    lines.push(plan.mapLink);
  }
  if (plan.clientLinks && plan.clientLinks.navigation) {
    lines.push('');
    lines.push('🔗 高德导航链接：');
    lines.push(plan.clientLinks.navigation);
  }

  if (plan.warning) {
    lines.push('');
    lines.push(`⚠️  ${plan.warning}`);
  }

  return lines.join('\n');
}

/**
 * 高级路线规划：支持地名或坐标
 *
 * @param {Object} params
 * @param {string} params.origin
 * @param {string} params.destination
 * @param {string} [params.type] - walking|driving|riding|transfer
 * @param {string} [params.city] - 城市（公交必填/地名消歧强烈建议）
 * @param {string} [params.waypoints]
 * @param {number} [params.strategy]
 * @param {boolean} [params.nightflag]
 * @param {boolean} [params.pickNearest=true] - 终点多候选时相对起点就近
 * @param {boolean} [params.silent]
 */
async function planRoute(params = {}) {
  const silent = !!params.silent;
  let type = (params.type || params.mode || 'driving').toLowerCase();
  // 兼容别名
  if (type === 'bicycling' || type === 'bike') type = 'riding';
  if (type === 'transit' || type === 'bus' || type === 'metro') type = 'transfer';

  const valid = new Set(['walking', 'driving', 'riding', 'transfer']);
  if (!valid.has(type)) {
    return { ok: false, error: `不支持的路线类型：${type}` };
  }

  const pickNearest = params.pickNearest !== false;

  // 城市：显式 > 从起终点推测 > 默认城市
  const cityInfo = await resolveCityContext({
    city: params.city || '',
    defaultCity: params.defaultCity,
    places: [params.origin, params.destination, params.waypoints].filter(Boolean),
    silent
  });
  const city = cityInfo.city || '';

  if (cityInfo.source !== 'none') {
    logInfo(
      silent,
      `🏙️  城市：${city || '（无）'}（来源：${cityInfo.source}${cityInfo.usedDefault ? '，已回退默认' : ''}，置信度：${cityInfo.confidence}）`
    );
    if (cityInfo.detail) logInfo(silent, `   ${cityInfo.detail}`);
  }

  if (type === 'transfer' && !city) {
    return {
      ok: false,
      error:
        '公交/地铁需要城市参数：未能从起终点推测出城市，且未配置默认城市。请传 --city 或设置 AMAP_DEFAULT_CITY / config.defaultCity',
      cityInfo
    };
  }

  // 先解析起点
  const origin = await resolvePlace(params.origin, {
    city,
    silent: true,
    limit: parseInt(params.candidateLimit, 10) || 8
  });
  if (!origin.ok) {
    return { ok: false, error: `起点解析失败：${origin.error}`, cityInfo };
  }

  // 若起点解析出明确城市且当前靠默认/低置信，可二次收紧
  const originCity = normalizeCityName(origin.cityname);
  let effectiveCity = city;
  if (!params.city && originCity && (cityInfo.source === 'default' || cityInfo.confidence === 'low')) {
    if (!effectiveCity || effectiveCity === originCity) {
      effectiveCity = originCity;
    }
  }

  // 轻微间隔，降低连续 POI 请求被限流概率
  await new Promise((r) => setTimeout(r, 120));

  // 再解析终点（可用起点坐标就近）
  const destination = await resolvePlace(params.destination, {
    city: effectiveCity || city,
    silent: true,
    limit: parseInt(params.candidateLimit, 10) || 8,
    near: pickNearest ? origin.location : undefined
  });
  if (!destination.ok) {
    return { ok: false, error: `终点解析失败：${destination.error}`, cityInfo };
  }

  // 公交 city 优先用有效城市
  const routeCity = effectiveCity || city || normalizeCityName(destination.cityname) || normalizeCityName(origin.cityname);

  // 途经点（可选，逗号分隔多个地名/坐标用 | 或 ;）
  let waypoints = params.waypoints || '';
  if (waypoints && !String(waypoints).split(/[;|]/).every((p) => isCoordinate(p.trim()) || !p.trim())) {
    const parts = String(waypoints)
      .split(/[;|]/)
      .map((x) => x.trim())
      .filter(Boolean);
    const resolved = [];
    for (const p of parts) {
      if (isCoordinate(p)) {
        resolved.push(normalizeCoordinate(p));
      } else {
        const r = await resolvePlace(p, {
          city: routeCity,
          silent: true,
          near: origin.location
        });
        if (r.ok) resolved.push(r.location);
      }
    }
    waypoints = resolved.join(';');
  }

  let raw = null;
  if (type === 'walking') {
    raw = await walkingRoute({
      origin: origin.location,
      destination: destination.location,
      silent
    });
  } else if (type === 'driving') {
    raw = await drivingRoute({
      origin: origin.location,
      destination: destination.location,
      waypoints: waypoints || undefined,
      strategy: params.strategy,
      extensions: params.extensions || 'all',
      silent
    });
  } else if (type === 'riding') {
    raw = await ridingRoute({
      origin: origin.location,
      destination: destination.location,
      silent
    });
  } else if (type === 'transfer') {
    if (!routeCity) {
      return {
        ok: false,
        error: '公交/地铁缺少城市：推测与默认均不可用',
        origin,
        destination,
        cityInfo
      };
    }
    raw = await transitRoute({
      origin: origin.location,
      destination: destination.location,
      city: routeCity,
      strategy: params.strategy,
      nightflag: params.nightflag,
      silent
    });
  }

  if (!raw) {
    return {
      ok: false,
      error: '路径规划 API 未返回有效结果，请检查坐标/城市/Key 权限',
      origin,
      destination,
      cityInfo,
      city: routeCity
    };
  }

  // 空路径保护
  if (type === 'transfer') {
    const n = (raw.route && raw.route.transits && raw.route.transits.length) || 0;
    if (!n) {
      return {
        ok: false,
        error: '未找到公交/地铁方案（可能跨城、城市参数不准或距离过近/过远）',
        origin,
        destination,
        cityInfo,
        city: routeCity,
        raw
      };
    }
  } else if (type === 'riding') {
    if (!(raw.data && raw.data.paths && raw.data.paths.length)) {
      return { ok: false, error: '未找到骑行路线', origin, destination, cityInfo, city: routeCity, raw };
    }
  } else if (!(raw.route && raw.route.paths && raw.route.paths.length)) {
    return { ok: false, error: '未找到路线', origin, destination, cityInfo, city: routeCity, raw };
  }

  const summary = summarizeRoute(type, raw);
  const steps = extractStepsFromRoute(type, raw);

  const [oLng, oLat] = origin.location.split(',').map(Number);
  const [dLng, dLat] = destination.location.split(',').map(Number);
  const mapTaskData = [
    {
      type: 'route',
      routeType: type,
      start: [oLng, oLat],
      end: [dLng, dLat],
      city: type === 'transfer' ? routeCity : undefined,
      remark: `${origin.name} → ${destination.name}`
    }
  ];
  const mapLink = generateMapLink(mapTaskData);
  const clientLinks = generateAmapClientLinks(origin, destination, type);

  let warning = '';
  if (cityInfo.usedDefault) {
    warning = `城市来自默认配置「${routeCity}」，若路线不对请显式指定 --city。`;
  } else if (cityInfo.source === 'none') {
    warning = '未能确定城市，结果可能有偏差；建议指定 --city 或配置 AMAP_DEFAULT_CITY。';
  } else if (cityInfo.confidence === 'low') {
    warning = `城市「${routeCity}」为低置信度推测（${cityInfo.source}），如有误请传 --city。`;
  }
  if (destination.level && /省|市/.test(String(destination.level)) && String(destination.source || '').startsWith('geocode')) {
    warning = (warning ? warning + ' ' : '') + '终点地理编码粒度较粗，建议改用更具体的地点名或 POI 名称。';
  }

  return {
    ok: true,
    type,
    city: routeCity,
    cityInfo: {
      ...cityInfo,
      city: routeCity
    },
    origin,
    destination,
    summary,
    steps,
    mapTaskData,
    mapLink,
    clientLinks,
    warning: warning || undefined,
    raw
  };
}

/**
 * 周边搜索高级封装：中心可为地名或坐标
 */
async function planNearby(params = {}) {
  const silent = !!params.silent;
  const keywords = params.keywords || params.query || '';
  const radius = parseInt(params.radius, 10) || 1000;
  const around = params.around || params.location || params.center;

  if (!around) {
    return { ok: false, error: '请提供 around/center（中心地点名或坐标）' };
  }
  if (!keywords) {
    return { ok: false, error: '请提供 keywords（搜索类别，如 美食、酒店）' };
  }

  const cityInfo = await resolveCityContext({
    city: params.city || '',
    defaultCity: params.defaultCity,
    places: [around, keywords],
    silent
  });
  const city = cityInfo.city || '';

  if (cityInfo.source !== 'none') {
    logInfo(
      silent,
      `🏙️  城市：${city || '（无）'}（来源：${cityInfo.source}${cityInfo.usedDefault ? '，已回退默认' : ''}，置信度：${cityInfo.confidence}）`
    );
  }

  const center = await resolvePlace(around, { city, silent: true, limit: 5 });
  if (!center.ok) {
    return { ok: false, error: `中心点解析失败：${center.error}`, cityInfo };
  }

  const effectiveCity = city || normalizeCityName(center.cityname) || '';

  const result = await searchAround({
    keywords,
    location: center.location,
    radius,
    city: effectiveCity,
    types: params.types,
    page: params.page || 1,
    offset: params.offset || 10,
    silent
  });

  const pois = (result && result.pois) || [];
  // 附带直线距离
  const items = pois.map((p, idx) => {
    const dist =
      p.distance != null && p.distance !== ''
        ? Number(p.distance)
        : p.location
          ? Math.round(distanceMeters(center.location, p.location))
          : null;
    return {
      index: idx + 1,
      name: p.name,
      address: p.address || '',
      type: p.type || '',
      tel: p.tel || '',
      location: p.location,
      distance_m: dist,
      adname: p.adname || '',
      cityname: p.cityname || ''
    };
  });

  items.sort((a, b) => {
    if (a.distance_m == null) return 1;
    if (b.distance_m == null) return -1;
    return a.distance_m - b.distance_m;
  });
  items.forEach((it, i) => {
    it.index = i + 1;
  });

  const [lng, lat] = center.location.split(',').map(Number);
  const mapTaskData = [
    {
      type: 'poi',
      lnglat: [lng, lat],
      sort: '中心',
      text: center.name,
      remark: `周边 ${radius}m 搜「${keywords}」`
    },
    ...items.slice(0, 10).map((it) => {
      const [x, y] = (it.location || '0,0').split(',').map(Number);
      return {
        type: 'poi',
        lnglat: [x, y],
        sort: keywords,
        text: it.name,
        remark: it.address || ''
      };
    })
  ];

  // 兼容 ditu 周边搜索链接
  const webLink = `https://ditu.amap.com/search?query=${encodeURIComponent(keywords)}&query_type=RQBXY&longitude=${lng}&latitude=${lat}&range=${radius}`;

  let warning;
  if (cityInfo.usedDefault) {
    warning = `城市来自默认配置「${effectiveCity}」，若结果不对请显式指定 --city。`;
  } else if (!effectiveCity) {
    warning = '未能确定城市，中心点可能解析偏移；建议指定 --city 或配置 AMAP_DEFAULT_CITY。';
  } else if (cityInfo.confidence === 'low') {
    warning = `城市「${effectiveCity}」为低置信度推测，如有误请传 --city。`;
  }

  return {
    ok: true,
    city: effectiveCity,
    cityInfo: { ...cityInfo, city: effectiveCity },
    center,
    keywords,
    radius,
    count: result ? result.count : items.length,
    items,
    mapTaskData,
    mapLink: generateMapLink(mapTaskData),
    webLink,
    warning
  };
}

function formatNearbyText(plan) {
  if (!plan || !plan.ok) {
    return `❌ 周边搜索失败：${(plan && plan.error) || '未知错误'}`;
  }
  const lines = [];
  lines.push(`✅ 已搜索「${plan.center.name}」周边 ${plan.radius} 米内的「${plan.keywords}」`);
  if (plan.city || (plan.cityInfo && plan.cityInfo.city)) {
    const c = plan.city || plan.cityInfo.city;
    const src = plan.cityInfo ? plan.cityInfo.source : '';
    lines.push(`   城市：${c}${src ? `（${src}）` : ''}`);
  }
  lines.push(`   中心坐标：${plan.center.location}`);
  if (plan.center.address) lines.push(`   地址：${plan.center.address}`);
  lines.push('');
  if (!plan.items.length) {
    lines.push('未找到结果，可尝试扩大半径或更换关键词。');
  } else {
    lines.push(`共返回 ${plan.items.length} 条（接口计数 ${plan.count || '-'}）：`);
    lines.push('');
    for (const it of plan.items) {
      lines.push(`${it.index}. ${it.name}`);
      if (it.address) lines.push(`   📌 ${it.address}`);
      if (it.type) lines.push(`   🏷️  ${it.type.split(';')[0]}`);
      if (it.distance_m != null) lines.push(`   📏 ${metersToKmText(it.distance_m)}`);
      if (it.location) lines.push(`   🌐 ${it.location}`);
      lines.push('');
    }
  }
  if (plan.webLink) {
    lines.push('🔗 地图搜索链接：');
    lines.push(plan.webLink);
  }
  if (plan.mapLink) {
    lines.push('');
    lines.push('🗺️ 可视化：');
    lines.push(plan.mapLink);
  }
  if (plan.warning) {
    lines.push('');
    lines.push(`⚠️  ${plan.warning}`);
  }
  return lines.join('\n');
}

/**
 * 旅游规划助手
 */
async function travelPlanner(params) {
  const { city, interests = [], routeType = 'walking' } = params;
  const silent = !!params.silent;

  logInfo(silent, `\n🗺️  开始为您规划 ${city} 的旅游行程...\n`);

  const mapTaskData = [];
  const poiResults = [];

  for (const interest of interests) {
    logInfo(silent, `📍 搜索 ${interest}...`);
    const result = await searchPOI({
      keywords: interest,
      city,
      page: 1,
      offset: 5,
      silent: true
    });

    if (result && result.pois && result.pois.length > 0) {
      // 每类取前 2 个，避免把所有兴趣点串成超长折线
      const picked = result.pois.slice(0, 2);
      poiResults.push(...picked);

      picked.forEach((poi) => {
        const [lng, lat] = poi.location.split(',').map(Number);
        mapTaskData.push({
          type: 'poi',
          lnglat: [lng, lat],
          sort: poi.type || interest,
          text: poi.name,
          remark: poi.address || `${interest}推荐`
        });
      });
    }
  }

  const routeSegments = [];
  if (poiResults.length >= 2) {
    logInfo(silent, `\n🛣️  规划游览路线（${routeType}）...\n`);

    for (let i = 0; i < poiResults.length - 1; i++) {
      const start = poiResults[i];
      const end = poiResults[i + 1];
      const [startLng, startLat] = start.location.split(',').map(Number);
      const [endLng, endLat] = end.location.split(',').map(Number);

      const routeTask = {
        type: 'route',
        routeType,
        start: [startLng, startLat],
        end: [endLng, endLat],
        remark: `从 ${start.name} 到 ${end.name}`
      };
      if (routeType === 'transfer') routeTask.city = city;
      mapTaskData.push(routeTask);

      // 真正请求路段摘要（失败不阻断）
      try {
        const seg = await planRoute({
          origin: start.location,
          destination: end.location,
          type: routeType,
          city,
          pickNearest: false,
          silent: true
        });
        if (seg.ok) {
          routeSegments.push({
            from: start.name,
            to: end.name,
            summary: seg.summary,
            steps: (seg.steps || []).slice(0, 5)
          });
        }
      } catch (_) {
        // ignore segment failures
      }
    }
  }

  logInfo(silent, '\n✅ 旅游规划完成！\n');
  logInfo(silent, '📍 推荐地点：');
  poiResults.forEach((poi, index) => {
    logInfo(silent, `${index + 1}. ${poi.name}`);
    logInfo(silent, `   地址: ${poi.address}`);
    logInfo(silent, `   类型: ${poi.type}\n`);
  });

  const mapLink = generateMapLink(mapTaskData);
  return {
    pois: poiResults,
    mapTaskData,
    mapLink,
    routeSegments
  };
}

// 导出函数供其他脚本使用
module.exports = {
  readConfig,
  saveConfig,
  getWebServiceKey,
  getDefaultCity,
  setWebServiceKey,
  ensureWebServiceKey,
  isCoordinate,
  normalizeCoordinate,
  normalizeCityName,
  extractCityFromText,
  resolveCityContext,
  distanceMeters,
  geocode,
  regeocode,
  searchPOI,
  searchAround,
  resolvePlace,
  walkingRoute,
  drivingRoute,
  ridingRoute,
  transitRoute,
  planRoute,
  planNearby,
  formatRoutePlanText,
  formatNearbyText,
  generateMapLink,
  generateAmapClientLinks,
  travelPlanner
};

// 如果直接运行此文件，执行示例搜索
if (require.main === module) {
  (async () => {
    try {
      const result = await searchPOI({
        keywords: '肯德基',
        city: '北京',
        page: 1,
        offset: 10
      });

      if (result && result.pois) {
        console.log('搜索结果:');
        result.pois.forEach((poi, index) => {
          console.log(`${index + 1}. ${poi.name}`);
          console.log(`   地址: ${poi.address}`);
          console.log(`   类型: ${poi.type}`);
          console.log(`   坐标: ${poi.location}\n`);
        });
      }
    } catch (error) {
      console.error('执行失败:', error.message);
      process.exit(1);
    }
  })();
}
