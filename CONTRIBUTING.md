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
5. Open a PR with a clear title and a short description of **what** and **why**.

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
