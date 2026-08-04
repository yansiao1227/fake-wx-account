#!/usr/bin/env node

/**
 * 路径规划脚本（兼容旧坐标接口；地名请优先用 route-plan.js）
 *
 * 坐标:
 *   node scripts/route-planning.js --type=walking --origin=116.397428,39.90923 --destination=116.427281,39.903719
 *
 * 地名（自动转发到高级规划）:
 *   node scripts/route-planning.js --type=driving --origin=江苏路 --destination=马厂老火锅 --city=上海
 */

const {
  isCoordinate,
  planRoute,
  formatRoutePlanText,
  walkingRoute,
  drivingRoute,
  ridingRoute,
  transitRoute,
  generateMapLink
} = require('../index');

// 解析命令行参数
function parseArgs() {
  const args = {};
  process.argv.slice(2).forEach(arg => {
    if (arg.startsWith('--')) {
      const eq = arg.indexOf('=');
      if (eq === -1) {
        args[arg.slice(2)] = true;
      } else {
        args[arg.slice(2, eq)] = arg.slice(eq + 1);
      }
    }
  });
  return args;
}

// 主函数
async function main() {
  const args = parseArgs();
  
  // 检查必需参数
  if (!args.type || !args.origin || !args.destination) {
    console.error('❌ 缺少必需参数');
    console.log('\n使用方法:');
    console.log('node scripts/route-planning.js --type=路线类型 --origin=起点 --destination=终点 [其他参数]');
    console.log('\n路线类型:');
    console.log('  walking  - 步行');
    console.log('  driving  - 驾车');
    console.log('  riding   - 骑行');
    console.log('  transfer - 公交（需要额外提供 --city 参数）');
    console.log('\n说明: 起点/终点支持坐标或地名；地名时请带 --city');
    console.log('更推荐: node scripts/route-plan.js --origin=... --destination=... --city=...');
    console.log('\n示例:');
    console.log('# 步行路线（坐标）');
    console.log('node scripts/route-planning.js --type=walking --origin=116.397428,39.90923 --destination=116.427281,39.903719');
    console.log('\n# 驾车路线（地名）');
    console.log('node scripts/route-planning.js --type=driving --origin=江苏路 --destination=马厂老火锅 --city=上海');
    console.log('\n# 公交路线');
    console.log('node scripts/route-planning.js --type=transfer --origin=116.397428,39.90923 --destination=116.427281,39.903719 --city=北京');
    process.exit(1);
  }
  
  const { type, origin, destination } = args;

  // 地名或混合输入 → 高级规划（含 steps）
  if (!isCoordinate(origin) || !isCoordinate(destination) || args.city) {
    if ((type === 'transfer' || type === 'transit') && !args.city) {
      console.error('❌ 公交路线规划需要提供 --city 参数');
      process.exit(1);
    }
    try {
      const plan = await planRoute({
        origin,
        destination,
        type: type === 'transit' ? 'transfer' : type,
        city: args.city || '',
        waypoints: args.waypoints,
        strategy: args.strategy ? parseInt(args.strategy, 10) : undefined,
        nightflag: args.nightflag === 'true' || args.nightflag === true,
        silent: false
      });
      console.log(formatRoutePlanText(plan));
      process.exit(plan.ok ? 0 : 1);
    } catch (error) {
      console.error('\n❌ 执行失败:', error.message);
      process.exit(1);
    }
    return;
  }
  
  try {
    let result = null;
    let mapTaskData = [];
    
    // 解析起点和终点坐标
    const [originLng, originLat] = origin.split(',').map(Number);
    const [destLng, destLat] = destination.split(',').map(Number);
    
    // 根据类型调用不同的路径规划API
    switch (type) {
      case 'walking':
        result = await walkingRoute({ origin, destination });
        if (result && result.route && result.route.paths && result.route.paths[0]) {
          const p0 = result.route.paths[0];
          mapTaskData.push({
            type: 'route',
            routeType: 'walking',
            start: [originLng, originLat],
            end: [destLng, destLat],
            remark: `步行路线，距离约 ${(p0.distance / 1000).toFixed(2)} 公里`
          });
          
          console.log('📊 路线信息:');
          console.log(`   距离: ${(p0.distance / 1000).toFixed(2)} 公里`);
          console.log(`   预计时间: ${Math.round(p0.duration / 60)} 分钟\n`);
          if (p0.steps && p0.steps.length) {
            console.log('🧭 怎么走:');
            p0.steps.slice(0, 20).forEach((s, i) => {
              console.log(`   ${i + 1}. ${s.instruction || ''}`);
            });
            console.log('');
          }
        }
        break;
        
      case 'driving':
        const drivingParams = { origin, destination, extensions: 'all' };
        if (args.waypoints) {
          drivingParams.waypoints = args.waypoints;
        }
        if (args.strategy) {
          drivingParams.strategy = parseInt(args.strategy);
        }
        
        result = await drivingRoute(drivingParams);
        if (result && result.route && result.route.paths && result.route.paths[0]) {
          const path = result.route.paths[0];
          mapTaskData.push({
            type: 'route',
            routeType: 'driving',
            start: [originLng, originLat],
            end: [destLng, destLat],
            remark: `驾车路线，距离约 ${(path.distance / 1000).toFixed(2)} 公里`
          });
          
          console.log('📊 路线信息:');
          console.log(`   距离: ${(path.distance / 1000).toFixed(2)} 公里`);
          console.log(`   预计时间: ${Math.round(path.duration / 60)} 分钟`);
          console.log(`   过路费: ${path.tolls || 0} 元`);
          console.log(`   红绿灯: ${path.traffic_lights || 0} 个\n`);
          if (path.steps && path.steps.length) {
            console.log('🧭 怎么走:');
            path.steps.slice(0, 20).forEach((s, i) => {
              console.log(`   ${i + 1}. ${s.instruction || ''}`);
            });
            console.log('');
          }
        }
        break;
        
      case 'riding':
        result = await ridingRoute({ origin, destination });
        if (result && result.data && result.data.paths && result.data.paths[0]) {
          const path = result.data.paths[0];
          mapTaskData.push({
            type: 'route',
            routeType: 'riding',
            start: [originLng, originLat],
            end: [destLng, destLat],
            remark: `骑行路线，距离约 ${(path.distance / 1000).toFixed(2)} 公里`
          });
          
          console.log('📊 路线信息:');
          console.log(`   距离: ${(path.distance / 1000).toFixed(2)} 公里`);
          console.log(`   预计时间: ${Math.round(path.duration / 60)} 分钟\n`);
        }
        break;
        
      case 'transfer':
      case 'transit':
        if (!args.city) {
          console.error('❌ 公交路线规划需要提供 --city 参数');
          process.exit(1);
        }
        
        const transitParams = {
          origin,
          destination,
          city: args.city,
          strategy: args.strategy ? parseInt(args.strategy) : 0,
          nightflag: args.nightflag === 'true'
        };
        
        result = await transitRoute(transitParams);
        if (result && result.route && result.route.transits) {
          mapTaskData.push({
            type: 'route',
            routeType: 'transfer',
            start: [originLng, originLat],
            end: [destLng, destLat],
            city: args.city,
            remark: `公交路线，共 ${result.route.transits.length} 个方案`
          });
          
          console.log('📊 路线信息:');
          console.log(`   方案数量: ${result.route.transits.length} 个`);
          if (result.route.transits.length > 0) {
            const transit = result.route.transits[0];
            console.log(`   预计时间: ${Math.round(transit.duration / 60)} 分钟`);
            console.log(`   费用: ${transit.cost} 元`);
            console.log(`   步行距离: ${transit.walking_distance} 米\n`);
          }
        }
        break;
        
      default:
        console.error(`❌ 不支持的路线类型: ${type}`);
        process.exit(1);
    }
    
    if (result && mapTaskData.length > 0) {
      // 生成地图链接
      const mapLink = generateMapLink(mapTaskData);
      console.log('🗺️  地图可视化链接:');
      console.log(mapLink);
      console.log('\n💡 提示: 复制链接到浏览器打开即可查看路线详情');
      console.log('💡 地名规划更推荐: node scripts/route-plan.js --origin=... --destination=... --city=...\n');
    } else {
      console.log('\n❌ 路线规划失败，请检查参数是否正确');
      process.exit(1);
    }
  } catch (error) {
    console.error('\n❌ 执行失败:', error.message);
    process.exit(1);
  }
}

// 执行主函数
main();
