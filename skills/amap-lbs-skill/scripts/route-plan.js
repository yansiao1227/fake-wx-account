#!/usr/bin/env node

/**
 * 高级路径规划（支持地名或坐标）
 *
 * 用法:
 *   node scripts/route-plan.js --origin=江苏路 --destination=马厂老火锅 --city=上海
 *   node scripts/route-plan.js --origin=江苏路 --destination=马厂老火锅 --city=上海 --type=transfer
 *   node scripts/route-plan.js --origin=116.397428,39.90923 --destination=天安门 --city=北京 --type=walking
 *
 * 环境变量: AMAP_WEBSERVICE_KEY
 */

const { planRoute, formatRoutePlanText } = require('../index');

function parseArgs(argv) {
  const args = {};
  const positional = [];
  for (const arg of argv) {
    if (arg.startsWith('--')) {
      const eq = arg.indexOf('=');
      if (eq === -1) {
        args[arg.slice(2)] = true;
      } else {
        const key = arg.slice(2, eq);
        const value = arg.slice(eq + 1);
        args[key] = value;
      }
    } else {
      positional.push(arg);
    }
  }
  // 支持: route-plan.js 起点 终点 [type]
  if (!args.origin && positional[0]) args.origin = positional[0];
  if (!args.destination && positional[1]) args.destination = positional[1];
  if (!args.type && !args.mode && positional[2]) args.type = positional[2];
  return args;
}

function printHelp() {
  console.log(`
高德路径规划（地名/坐标）

用法:
  node scripts/route-plan.js --origin=起点 --destination=终点 [--city=城市] [--type=类型]

参数:
  --origin        起点：地名或 经度,纬度（必填）
  --destination   终点：地名或 经度,纬度（必填）
  --city          城市（可选）。不传则自动推测；推测失败用默认城市
  --defaultCity   本次调用的默认城市（覆盖环境变量/配置）
  --type, --mode  walking | driving | riding | transfer（默认 driving）
                  别名: bike/bicycling→riding, bus/transit/metro→transfer
  --waypoints     途经点，多个用 ; 或 | 分隔（地名或坐标）
  --strategy      策略（驾车/公交数值，可选）
  --nightflag     true 时考虑夜班车（公交）
  --pickNearest   true/false，终点多候选是否就近（默认 true）
  --json          输出 JSON（含 steps/summary）

城市解析顺序:
  1) --city 显式指定
  2) 从起终点文本/坐标/POI 分布自动推测
  3) --defaultCity 或环境变量 AMAP_DEFAULT_CITY 或 config.defaultCity

示例:
  node scripts/route-plan.js --origin=江苏路 --destination=马厂老火锅
  node scripts/route-plan.js --origin=江苏路 --destination=马厂老火锅 --type=transfer
  node scripts/route-plan.js --origin=北京站 --destination=天安门 --type=walking
  node scripts/route-plan.js --origin=江苏路 --destination=马厂老火锅 --city=上海
`.trim());
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help || args.h) {
    printHelp();
    process.exit(0);
  }

  if (!args.origin || !args.destination) {
    console.error('❌ 缺少 --origin 或 --destination\n');
    printHelp();
    process.exit(1);
  }

  if (!process.env.AMAP_WEBSERVICE_KEY && !process.env.AMAP_KEY) {
    // index 还会读 config.json；这里仅提示
    console.warn('⚠️  未检测到环境变量 AMAP_WEBSERVICE_KEY，将尝试读取 skill 目录 config.json');
  }

  try {
    const plan = await planRoute({
      origin: args.origin,
      destination: args.destination,
      city: args.city || '',
      defaultCity: args.defaultCity || args.defaultcity || undefined,
      type: args.type || args.mode || 'driving',
      waypoints: args.waypoints,
      strategy: args.strategy != null && args.strategy !== true ? parseInt(args.strategy, 10) : undefined,
      nightflag: args.nightflag === true || args.nightflag === 'true',
      pickNearest: !(args.pickNearest === false || args.pickNearest === 'false'),
      silent: false
    });

    if (args.json === true || args.json === 'true') {
      // 避免把完整 raw 打爆终端；需要可再加 --raw
      const payload = { ...plan };
      if (!(args.raw === true || args.raw === 'true')) {
        delete payload.raw;
      }
      console.log(JSON.stringify(payload, null, 2));
    } else {
      console.log(formatRoutePlanText(plan));
    }

    process.exit(plan.ok ? 0 : 1);
  } catch (error) {
    console.error('❌ 执行失败:', error.message);
    process.exit(1);
  }
}

main();
