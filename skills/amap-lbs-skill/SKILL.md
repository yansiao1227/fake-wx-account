---
name: amap-lbs-skill
description: 高德地图综合服务，支持POI搜索、周边搜索、路径规划（地名/坐标）、旅游规划、热力图与地图链接。路线与周边优先使用 scripts/route-plan.js 与 scripts/nearby-search.js。
metadata:
  openclaw:
    requires:
      env:
        - AMAP_WEBSERVICE_KEY
      bins:
        - node
    primaryEnv: AMAP_WEBSERVICE_KEY
    homepage: https://lbs.amap.com/api/webservice/summary
    install:
      - kind: node
        package: axios
        bins: []
---

# 高德地图综合服务 Skill

高德地图综合服务：地点搜索、**周边搜索**、**路径规划（支持中文地名）**、旅游规划与地图可视化。

## 功能特性

- 🔍 POI / 关键词搜索
- 📍 周边搜索（中心可为地名或坐标）
- 🛣️ 路径规划：步行 / 驾车 / 骑行 / 公交地铁（**支持地名**，输出逐步怎么走）
- 🗺️ 旅游规划助手
- 🔥 热力图链接
- 🔗 地图可视化 / 高德导航链接
- 🎯 Key：`AMAP_WEBSERVICE_KEY` 或 skill 目录 `config.json`

## 首次配置

1. 在 [高德开放平台](https://lbs.amap.com/api/webservice/create-project-and-key) 创建 Web 服务 Key  
2. 设置环境变量：`AMAP_WEBSERVICE_KEY=your_key`（推荐写在 `~/.cow/.env`）  
3. 或在 skill 目录创建 `config.json`：`{"webServiceKey":"..."}`  

**工作目录**：执行脚本前先 `cd` 到本 skill 根目录（`SKILL.md` 所在目录），或使用脚本绝对路径。

```bash
# 在 skill 根目录执行
export AMAP_WEBSERVICE_KEY=your_key   # Windows 可用系统/会话环境变量
```

---

## 触发条件

用户表达以下意图时使用本 skill：

- 搜地点 / 找店 / POI（「天安门在哪」「搜肯德基」）
- **周边**（「西直门附近美食」「江苏路周边火锅」）
- **路线**（「从 A 到 B 怎么走」「地铁怎么去」「驾车导航」）
- 旅游行程（「杭州一日游」）
- 热力图 / 地图链接

---

## 场景判断（先判定再调用）

| 场景 | 用户意图 | 优先命令 |
|------|----------|----------|
| A 关键词搜索（不要求周边） | 搜美食、天安门在哪 | 地图链接 或 `poi-search.js` |
| B **周边搜索** | X 附近/周边 Y | **`nearby-search.js`** |
| C **路径规划** | 从 A 到 B、怎么走、导航 | **`route-plan.js`** |
| D POI 详细列表 | 要地址电话列表 | `poi-search.js` |
| E 旅游规划 | 一日游、兴趣点串联 | `travel-planner.js` |
| F 热力图 | 热力图可视化 | 场景「热力图」拼链接 |
| G 仅打开网页搜 | 无 Key 时的降级 | `https://www.amap.com/search?query=` |

> **路线 / 周边请优先用场景 B、C 的高级脚本**，不要先对地名裸调地理编码再手填坐标（易解析到外省）。

---

## 场景 C：路径规划（推荐）

**一站式脚本**：地名或坐标均可；自动 POI/地理编码消歧；连锁店默认按起点**就近**选点；输出距离、时间、**逐步怎么走**、地图/导航链接。

### 命令

```bash
# 可不传 city：自动推测；失败则用默认城市
node scripts/route-plan.js --origin=江苏路 --destination=马厂老火锅

# 公交/地铁同样可自动推测城市
node scripts/route-plan.js --origin=江苏路 --destination=马厂老火锅 --type=transfer

# 显式指定城市（最高优先级）
node scripts/route-plan.js --origin=江苏路 --destination=马厂老火锅 --city=上海

# 步行 / 骑行
node scripts/route-plan.js --origin=北京站 --destination=天安门 --type=walking
node scripts/route-plan.js --origin=春熙路 --destination=宽窄巷子 --type=riding

# 坐标仍可用（会逆地理推测城市）
node scripts/route-plan.js --origin=121.430635,31.220408 --destination=121.457872,31.186690 --type=driving

# 结构化输出
node scripts/route-plan.js --origin=江苏路 --destination=马厂老火锅 --json
```

### 参数

| 参数 | 说明 |
|------|------|
| `--origin` | 起点：中文地名或 `经度,纬度` |
| `--destination` | 终点：中文地名或 `经度,纬度` |
| `--city` | 可选。显式城市（最高优先级） |
| `--defaultCity` | 可选。本次调用默认城市（覆盖环境/配置里的默认） |
| `--type` | `walking` / `driving`（默认）/ `riding` / `transfer` |
| `--waypoints` | 途经点，多个用 `;` 或 `\|` |
| `--pickNearest` | 默认 true：终点多候选时相对起点就近 |
| `--json` | 输出 JSON |

别名：`bike`→骑行，`bus`/`transit`/`metro`→`transfer`。

### 城市解析顺序（自动）

1. **`--city` 显式指定**  
2. **自动推测**：地点文本中的城市名（如「上海市…」）→ 坐标逆地理 → 对起终点做 POI 投票/交叉  
3. **默认城市**：`--defaultCity` → 环境变量 `AMAP_DEFAULT_CITY`（或 `AMAP_CITY`）→ `config.json` 的 `defaultCity`  

公交/地铁在「推测 + 默认」都拿不到城市时才会报错。输出中会标注城市来源（explicit / text / regeo / poi / default）。

### Agent 执行要点

1. 从用户话里抽出 **起点、终点、出行方式**；若用户已说城市则传 `--city`。  
2. **不必强行先问城市**：可直接调用 `route-plan.js`，依赖自动推测 + 默认城市。  
3. 若输出警告「来自默认配置 / 低置信度」且路线明显不对，再向用户确认城市并加 `--city` 重试。  
4. 将脚本 stdout **原样或精炼**回复用户（保留城市来源、怎么走、门店候选、链接）。  
5. 若终点有多家分店，脚本会列出候选；用户指定分店名时应重新规划。

### 兼容旧脚本

```bash
# 仅坐标的旧接口仍可用；若传入地名会自动走高级规划
node scripts/route-planning.js --type=driving --origin=江苏路 --destination=马厂老火锅 --city=上海
node scripts/route-planning.js --type=walking --origin=116.397428,39.90923 --destination=116.427281,39.903719
```

---

## 场景 B：周边搜索（推荐）

中心点支持**地名或坐标**，无需先手动 geocode。

```bash
node scripts/nearby-search.js --around=西直门 --keywords=美食 --radius=1000
node scripts/nearby-search.js --around=江苏路 --keywords=火锅 --radius=1500
node scripts/nearby-search.js --around=116.397428,39.90923 --keywords=咖啡 --radius=800
node scripts/nearby-search.js --around=江苏路 --keywords=加油站 --json
# 也可显式指定
node scripts/nearby-search.js --around=江苏路 --keywords=火锅 --city=上海 --radius=1500
```

| 参数 | 说明 |
|------|------|
| `--around` | 中心：地名或 `经度,纬度` |
| `--keywords` | 类别/关键词：美食、酒店、超市… |
| `--city` | 可选；不传则自动推测，失败用默认城市 |
| `--defaultCity` | 可选；本次默认城市 |
| `--radius` | 米，默认 1000 |
| `--offset` | 条数，默认 10，最大 25 |

输出：中心点解析结果、按距离排序的 POI、地图搜索链接。

---

## 场景 D：POI 文本搜索

```bash
node scripts/poi-search.js --keywords=肯德基 --city=北京
node scripts/poi-search.js --keywords=酒店 --location=116.397428,39.90923 --radius=1000
node scripts/poi-search.js --keywords=餐厅 --city=上海 --page=1 --offset=20
```

适合「在某市搜品牌/类型列表」。**「某地附近」请用 nearby-search.js。**

---

## 场景 A：无 Key 时的网页搜索降级

不调 Web API，仅生成链接：

```
https://www.amap.com/search?query={关键词}
```

示例：`query=美食`、`query=天安门`。

有 Key 时优先场景 B/C/D，结果更可控。

---

## 场景 E：旅游规划

```bash
node scripts/travel-planner.js --city=北京 --interests=景点,美食,酒店
node scripts/travel-planner.js --city=杭州 --interests=西湖,美食,茶馆 --routeType=walking
node scripts/travel-planner.js --city=上海 --interests=外滩,南京路 --routeType=driving
```

- 按兴趣检索 POI，并尝试生成段间路线摘要与地图链接  
- 复杂「从 A 到 B」单次导航请用 **route-plan.js**

---

## 场景 F：热力图

```
http://a.amap.com/jsapi_demo_show/static/openclaw/heatmap.html?mapStyle={grey|light}&dataUrl={URL编码后的数据地址}
```

执行前可按需发送埋点（可选）：

```bash
curl -s "https://restapi.amap.com/v3/log/init?eventId=skill.call&product=skill_openclaw&platform=JS&label=heatmap&value=call"
```

---

## 场景 G：Python / Electron 导航（可选，多数环境不可用）

`gaode_skill.py` 通过 Unix Domain Socket 连接高德 JSAPI Electron 桌面应用，**不是** Web 服务路径规划。

```bash
python gaode_skill.py direction 北京站 天安门 driving
python gaode_skill.py search 北京站周边的川菜
```

- 需要应用已启动且存在 socket：`/tmp/jsapi-electron.sock`  
- **Windows / 无 Electron 时不要使用**；路线请统一用 `scripts/route-plan.js`

---

## 配置管理

`config.json`（勿提交密钥）：

```json
{
  "webServiceKey": "your_amap_webservice_key_here",
  "defaultCity": "上海"
}
```

- Key 优先级：环境变量 `AMAP_WEBSERVICE_KEY` > `AMAP_KEY`（废弃）> `config.json`  
- 默认城市优先级：`--defaultCity` > `AMAP_DEFAULT_CITY` / `AMAP_CITY` > `config.defaultCity`

---

## 库函数（index.js，供脚本/二次开发）

| 函数 | 作用 |
|------|------|
| `searchPOI` | 文本 POI 搜索 |
| `searchAround` | 周边搜索 |
| `geocode` / `regeocode` | 地理/逆地理编码 |
| `resolvePlace` | 地名或坐标 → 统一地点 |
| `resolveCityContext` / `extractCityFromText` / `getDefaultCity` | 城市推测与默认城市 |
| `planRoute` | 高级路径规划（含城市自动解析） |
| `planNearby` | 高级周边搜索（含城市自动解析） |
| `formatRoutePlanText` / `formatNearbyText` | 可读输出 |
| `walkingRoute` / `drivingRoute` / `ridingRoute` / `transitRoute` | 底层 direction API |
| `travelPlanner` / `generateMapLink` | 旅游与可视化 |

---

## 注意事项

1. **城市**：优先自动推测；配置 `AMAP_DEFAULT_CITY` 或 `config.defaultCity` 作为兜底。模糊地名仍建议用户确认。  
2. **公交**：`--type=transfer`；城市可由推测/默认提供，二者皆无时会报错。  
3. **坐标格式**：`经度,纬度`（高德 GCJ-02），经度在前。  
4. **Key 权限**：需开通 Web 服务（路径规划、搜索、地理编码等）。  
5. **配额**：城市推测会额外打 POI 请求，注意 QPS/日配额。  
6. **埋点**：部分旧文档中的 `restapi.amap.com/v3/log/init` 为可选统计，不包含 Key 与用户隐私；失败可忽略。  
7. **不要**为了路径规划单独 `curl` geocode 取第一条结果再规划——请走 `route-plan.js`。  
8. 回复用户时给出：**城市（及来源）+ 起终点确认 + 时间距离 + 怎么走 + 链接**。

## 相关链接

- [高德开放平台](https://lbs.amap.com/)
- [创建 Key](https://lbs.amap.com/api/webservice/create-project-and-key)
- [路径规划](https://lbs.amap.com/api/webservice/guide/api/direction)
- [搜索 POI](https://lbs.amap.com/api/webservice/guide/api-advanced/newpoisearch)
