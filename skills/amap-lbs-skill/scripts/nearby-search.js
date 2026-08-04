#!/usr/bin/env node

/**
 * 周边搜索（中心支持地名或坐标）
 *
 * 用法:
 *   node scripts/nearby-search.js --around=江苏路 --keywords=火锅 --city=上海 --radius=1500
 *   node scripts/nearby-search.js --around=116.397428,39.90923 --keywords=咖啡 --radius=1000
 *
 * 环境变量: AMAP_WEBSERVICE_KEY
 */

const { planNearby, formatNearbyText } = require('../index');

function parseArgs(argv) {
  const args = {};
  const positional = [];
  for (const arg of argv) {
    if (arg.startsWith('--')) {
      const eq = arg.indexOf('=');
      if (eq === -1) {
        args[arg.slice(2)] = true;
      } else {
        args[arg.slice(2, eq)] = arg.slice(eq + 1);
      }
    } else {
      positional.push(arg);
    }
  }
  // 支持: nearby-search.js 中心 关键词 [city]
  if (!args.around && !args.center && positional[0]) args.around = positional[0];
  if (!args.keywords && positional[1]) args.keywords = positional[1];
  if (!args.city && positional[2]) args.city = positional[2];
  return args;
}

function printHelp() {
  console.log(`
高德周边搜索（中心可为地名或坐标）

用法:
  node scripts/nearby-search.js --around=中心地点 --keywords=类别 [--city=城市] [--radius=米]

参数:
  --around, --center  中心点：地名或 经度,纬度（必填）
  --keywords          搜索关键词/类别，如 美食、酒店、加油站（必填）
  --city              城市（可选）。不传则自动推测；失败用默认城市
  --defaultCity       本次默认城市（覆盖环境变量/配置）
  --radius            半径米，默认 1000
  --types             POI 类型编码（可选）
  --page              页码，默认 1
  --offset            每页数量，默认 10，最大 25
  --json              输出 JSON

城市解析顺序: --city → 从中心点/关键词推测 → defaultCity / AMAP_DEFAULT_CITY / config.defaultCity

示例:
  node scripts/nearby-search.js --around=西直门 --keywords=美食 --radius=1000
  node scripts/nearby-search.js --around=江苏路 --keywords=火锅 --radius=1500
  node scripts/nearby-search.js --around=江苏路 --keywords=火锅 --city=上海 --radius=1500
`.trim());
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help || args.h) {
    printHelp();
    process.exit(0);
  }

  const around = args.around || args.center || args.location;
  const keywords = args.keywords || args.query;

  if (!around || !keywords) {
    console.error('❌ 缺少 --around 或 --keywords\n');
    printHelp();
    process.exit(1);
  }

  try {
    const plan = await planNearby({
      around,
      keywords,
      city: args.city || '',
      defaultCity: args.defaultCity || args.defaultcity || undefined,
      radius: args.radius || 1000,
      types: args.types,
      page: args.page || 1,
      offset: args.offset || 10,
      silent: false
    });

    if (args.json === true || args.json === 'true') {
      console.log(JSON.stringify(plan, null, 2));
    } else {
      console.log(formatNearbyText(plan));
    }

    process.exit(plan.ok ? 0 : 1);
  } catch (error) {
    console.error('❌ 执行失败:', error.message);
    process.exit(1);
  }
}

main();
