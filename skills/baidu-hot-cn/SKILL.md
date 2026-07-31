---
name: baidu-hot-cn
description: 获取真实的百度热搜榜单。用户询问百度热搜、当前热点，或民生、财经、体育、文娱、国际、挑战、电影、电视剧、小说榜时使用。
metadata:
  cowagent:
    requires:
      bins: ["python"]
      env: ["QIANFAN_API_KEY"]
---

# 百度热搜

通过百度千帆官方 `baidu_trending` API 获取实时榜单，不使用模拟数据。

## 使用

在本 skill 目录下运行：

```powershell
python scripts/baidu_hot.py livelihood --limit 10 --json
```

可用榜单：

- `livelihood`：民生榜（默认）
- `finance`：财经榜
- `sports`：体育榜
- `new_entertainment`：文娱榜
- `internation_news`：国际榜（API 的正式枚举拼写如此）
- `challenge`：挑战榜
- `movie`：电影榜
- `teleplay`：电视剧榜
- `novel`：小说榜

优先使用 `--json`，根据 `items` 回答用户。每项包含排名、词条、热度、趋势、简介和百度链接。

## 约束

- 必须从环境变量 `QIANFAN_API_KEY` 读取凭据，禁止在回复、命令或日志中输出密钥。
- API 出错时明确说明失败，不得编造或回退到模拟热搜。
- 只展示用户需要的条数，避免原样转储冗长描述。
