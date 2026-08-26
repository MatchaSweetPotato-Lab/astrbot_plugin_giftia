# 协作开发规则

> **本文档是给 AI 编程助手（如 Gemini/DeepSeek/Claude）阅读和遵循的协作开发规则。**
> 把它作为项目上下文 / 系统提示喂给 AI，让 AI 在改动本仓库时严格按以下规则操作。
> 规则以「可执行、不二义」为目标：能照抄的命令就照抄，需要判断的地方给了明确判定条件。
---

## 0. 最高优先级规则（违反任何一条都算错误）

1. **任何改动开始前，先同步最新 `main`**：`git checkout main && git pull origin main`。基于旧代码开发是冲突的头号原因。
2. **不使用 `dev` 或任何长期分支。** 不要创建、不要维护、不要往 `dev` 提交。需要分支时，从最新 `main` 临时切出，合并后立即删除。
3. **删除或重命名任何函数 / 接口 / 字段前，必须全局搜索所有调用方并一并修改**：使用 grep 或 IDE 全局搜索。前后端、调用方与定义必须保持一致。
4. **推送前必须本地验证通过**（包含运行 `ruff`、单元测试 `pytest` 以及实际功能验证，见 [第 4 节](#4-推送前必须执行的自检)）。
5. **版本一致性**：发版时必须同步修改三处版本号（见 [第 5 节](#5-发版版本号三处必须同步)）。

---

## 1. 工作方式：什么时候直接推 main，什么时候开临时分支

为了保持提交历史整洁和便于追踪，建议采用以下规范。

### 路径 A —— 直接提交到 `main`（仅限「小修复」）

**同时满足以下全部条件**，才算「小修复」，可直接推 `main`：
- 改动集中在 1～2 个文件，且不超过几十行；
- **不**改动公共接口、函数签名、配置 schema（`_conf_schema.json`）、数据库表结构；
- 本地已编译 + 验证通过。

操作：
```bash
git checkout main
git pull --rebase origin main        # 先拉最新，避免冲突
# ...改代码...
# ...验证并执行 ruff 检查与单测...
git add <文件> && git commit -m "fix: 简短描述"
git pull --rebase origin main        # 推送前再同步一次
git push origin main
```

### 路径 B —— 开临时分支 + PR/合并（其余所有情况）

**只要不满足「小修复」全部条件，就走这里**：新功能、重构、改接口 / schema / 数据结构、改动较大。

```bash
git checkout main && git pull origin main
git checkout -b feat/简短英文描述         # 见第 2 节命名规范
# ...改代码 + 多次小步提交...
# ...自检并通过 ruff / pytest ...
git push -u origin feat/简短英文描述
# 然后在 GitHub 上向 main 发起 PR / 合并，写清改动内容
```

合并后清理临时分支：
```bash
git checkout main && git pull origin main
git branch -d feat/简短英文描述
git push origin --delete feat/简短英文描述
```

---

## 2. 提交信息与分支命名

### Commit 信息：`类型(模块): 简短描述` 或 `类型: 简短描述`

```
feat(memory): 新增长期记忆语义检索与定时自动清理
fix(caption): 修复纯文本图片转述主字段缺失导致的拦截
feat(webui): 表情包管理面板支持批量修改分类与导出
refactor(llm): 统一媒体转述提示词组装与容错解析机制
docs: 补充协作开发规则文档
```

- 常用类型：`feat`/`fix`/`docs`/`refactor`/`style`/`perf`/`chore`。
- ❌ 禁止：`更新`、`改了点东西`、`111` 这类无意义信息。
- ❌ **禁止在 commit 信息开头带 ` ``` ` 反引号**。

### 分支命名：`类型/简短英文描述`

`feat/relation-profile`、`fix/caption-json-parse`、`refactor/decision-engine`、`feat/webui-dashboard`、`docs/contributing`。

---

## 3. 同步 main 与解决冲突

功能分支落后于 `main` 需要更新时：
```bash
git fetch origin
git rebase origin/main
# 如有冲突，解决后：
git add <冲突文件> && git rebase --continue
git push --force-with-lease origin <你的分支>    # 仅对自己的临时分支使用
```

---

## 4. 推送前必须执行的自检

每次推送前，逐项确认：

- [ ] **代码格式与风格检查**：必须在插件目录下使用 AstrBot 虚拟环境的 ruff 格式化并检查所有 Python 代码。
  ```bash
  # 格式化
  <AstrBot_Path>/.venv/bin/ruff format .
  # 静态检查与自动修复
  <AstrBot_Path>/.venv/bin/ruff check --fix .
  ```
- [ ] **运行自动化测试**：确保本地所有单元测试绿色通过。
  ```bash
  pytest test/
  ```
- [ ] **功能已实际验证**：
  - 若改了 WebUI（`pages/giftia_dashboard/` 下的 HTML/JS/CSS）→ 在浏览器中打开对应仪表盘页签点选并测试 API 交互；
  - 若改了决策/记忆/转述/工具 → 在 AstrBot 聊天或测试场景中实测，确保回复、记忆落库与媒体识别符合预期。
- [ ] **关联处已同步**：改了函数/接口/字段名，调用方（含前端 JS / 模块间调用）已一并改动。
- [ ] **配置与前端管理页面已同步**：若修改了 `_conf_schema.json` 中的配置项，需确认机器人配置管理器与 WebUI 是否需要对应的交互适配。
- [ ] **未夹带**无关文件、调试 `print`、临时代码。

---

## 5. 发版：版本号三处必须同步

发布新版本时，以下三处版本号**必须一起修改**，漏一处就会导致版本不一致：

1. **`metadata.yaml`** 的 `version` 属性（如 `version: 0.1.9`）
2. **`README.md`** 顶部的 Giftia 版本 Badge（如 `[![Giftia](https://img.shields.io/badge/Giftia-v0.1.9-FFD700.svg)]`）
3. **`changelog.md`** 顶部新增对应版本号的变更日志块（如 `## [v0.1.9] - 2026-08-26`）

---

## 6. 禁止清单（高频翻车点，直接对照）

| ❌ 禁止 | ✅ 正确做法 |
|---|---|
| 基于旧代码开干 | 开工前先 `git pull origin main` |
| 直接修改/回退别人或上游已合并的改动 | 仔细确认，不擅自改回 |
| 删/改函数却漏改调用方 | 全局搜索找全调用方一起改 |
| 配置项变更漏改 schema/管理页面 | 同步修改 `_conf_schema.json` 及 WebUI 页面 |
| 只改一处版本号 | 三处同步（第 5 节：metadata.yaml, README.md badge, changelog.md） |
| commit 信息无意义 | 遵循 `类型(模块): 简短描述` 规范 |
| 冲突标记残留就提交 | 提交前搜索确认无 `<<<<<<<` |
