---
name: amap-map
description: 使用高德地图 Web 服务查询地点、周边 POI、地址与坐标，并规划步行、骑行或驾车路线。用户询问位置、附近地点、地址解析或路线时使用。
metadata:
  cowagent:
    requires:
      bins: ["python"]
      env: ["AMAP_API_KEY"]
---

# 高德位置服务

直接调用高德地图 Web 服务 API。

## 使用

在本 skill 目录下运行以下命令，并优先读取 JSON：

```powershell
python scripts/amap.py text_search "咖啡店" "上海" 10
python scripts/amap.py around_search "121.4737,31.2304" "咖啡店" 1000 10
python scripts/amap.py poi_detail "POI_ID"
python scripts/amap.py geo "北京市天安门广场" "北京"
python scripts/amap.py regeocode "116.397428,39.90923"
python scripts/amap.py walking "116.397428,39.90923" "116.407428,39.91923"
python scripts/amap.py bicycling "116.397428,39.90923" "116.407428,39.91923"
python scripts/amap.py driving "116.397428,39.90923" "116.407428,39.91923"
```

## 约束

- 凭据仅从环境变量 `AMAP_API_KEY` 读取，禁止输出密钥。
- 地址、坐标、路线端点会发送给高德；涉及敏感住址或实时行程时提醒用户注意隐私。
- API 返回 `status: "0"` 时，报告 `info` 与 `infocode`，不要猜测结果。
- 搜索结果较多时，仅提炼最相关的若干项。
