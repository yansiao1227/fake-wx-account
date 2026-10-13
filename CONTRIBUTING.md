# Contributing to CowAgent

Thanks for taking the time to contribute! 🎉 CowAgent is built by a global
community, and contributions of all sizes are welcome — from typo fixes to new
features.

## Language policy

To keep the project accessible to a global community, **please write issues,
pull requests, and code comments in English.** Commit messages follow the
repository-specific format in [AGENTS.md](AGENTS.md): a lowercase English type
prefix followed by a Chinese description, with any optional body in Chinese.

> 为方便全球开发者协作，请尽量使用**英文**提交 issue、PR 与代码注释。
> commit message 必须遵循 `type: 中文说明` 格式，类型前缀使用英文小写，正文使用中文。

## Reporting issues

Found a bug or have an idea? [Open an issue](https://github.com/zhayujie/CowAgent/issues/new/choose).

Before opening one, please search existing issues (including closed ones) to
avoid duplicates, and make sure you're on the latest version.

## Submitting a pull request

1. **Fork** the repo and create a branch from `master`
   (e.g. `feat/web-search`, `fix/wechat-reconnect`).
2. Make your change. Keep it focused — one logical change per PR.
3. Follow the existing code style. Write comments and docstrings in English.
4. Run the app locally to confirm your change works.
5. 按北京时间的合并日期，新建或追加 `changelogs/YYYY-MM-DD.md`，用几条精简中文列表
   记录本次用户可感知的变化；同一天共用一个文件，跨日合并时更新本次条目的日期。
6. 提交改动后执行 `.\scripts\check_changelog.ps1 -BaseRef origin/master`
   （合并到 `main` 时替换基准分支）。检查比较已提交变更，要求当日文件新增有效列表项。
7. Open a PR with a clear title and a short description of **what** and **why**.

每次合并到 `master` / `main` 前都需要完成这项记录，包括文档及维护改动。
日志不包含真实密钥、内部日志或测试输出。主分支 PR 会自动检查当日条目；
跨日待合并的 PR 需更新日志并重新检查。格式和每天 18:00 的群广播行为见
[changelogs/README.md](changelogs/README.md)。
维护者可在主分支保护中将 `Changelog check` 设为必需检查，强制执行合并前校验。

We keep the bar friendly: clear, focused, and working is enough. Maintainers are
happy to help polish details during review.

### Commit & PR titles

Use a short, clear summary for PR titles. Commit titles must use `type: 中文说明`,
with a lowercase English type such as `feat`, `fix`, `docs`, `refactor`, `perf`,
or `test`, followed by a space and a Chinese description:

```
feat: 新增联网搜索工具
fix: 修复微信桌面连接超时
docs: 补充 Docker 配置说明
```

## Development setup

See the [Install from Source](https://docs.cowagent.ai/guide/manual-install)
guide. In short:

```bash
git clone https://github.com/zhayujie/CowAgent.git
cd CowAgent
pip install -r requirements.txt
pip install -e .
cow start
```

## Code of conduct

Be respectful and constructive. We want CowAgent to be a welcoming place for
everyone.
