# Git 历史清洗与恢复说明

本仓库曾经从私有开发仓库切换为公开归档仓库。这个动作涉及 Git 历史重写，需要单独说明。

## 1. 为什么历史会消失

公开仓库如果直接保留旧提交，旧提交里的 token、代理 API、Cookie、个人资料等敏感内容也会被公开。即使当前文件已经脱敏，别人仍然可以通过 Git 历史下载旧版本。

因此公开归档时使用了干净历史发布：把最终脱敏后的文件复制到一个新仓库，创建单个归档提交，然后把 `main` 指向这个提交。

结果就是：公开 `main` 分支上不再显示旧的连续提交历史。

## 2. 这是不是强制覆盖

是。技术上属于 force push / 历史重写。这样做达到了保护公开仓库敏感信息的目的，但流程上应该提前明确确认，并先保存私有备份。

正确流程应该是：

1. 说明风险：公开历史可能暴露敏感信息。
2. 说明代价：清洗历史会让公开提交记录变短或重写。
3. 先创建本地私有 bundle 备份。
4. 再创建干净公开历史。
5. 推送后验证 Public 仓库和 Release。

这次前两步没有单独停下来确认，是一次流程教训。

## 3. 旧历史能不能找回

当前本机已创建本地私有备份：

```text
private_history_backup_DO_NOT_PUBLISH/room-bot-old-private-history-c321277.bundle
```

这个 bundle 基于旧提交 `c321277527b5012cbaa452c02a3c08c4db568b64`，包含旧的私有开发历史。它可能包含敏感信息，所以不要提交、不要上传、不要放进 Release。

只读查看方式：

```bash
cd /Users/Apple/Developer/art/qiangfang/private_history_backup_DO_NOT_PUBLISH
git clone room-bot-old-private-history-c321277.bundle room-bot-old-history
cd room-bot-old-history
git log --oneline --all
```

如果只是想看某个旧文件：

```bash
git show c321277:南山/app.py
```

## 4. 以后怎么避免

以后处理公开仓库时，默认不直接 force push。推荐策略：

- 如果只是普通文档和代码更新：正常 commit + push。
- 如果要公开私有仓库：先扫描历史敏感信息。
- 如果历史有敏感内容：先创建私有 bundle 备份。
- 如果需要保留历史：用 `git filter-repo` 或 BFG 清洗敏感文件，再检查。
- 如果可以接受干净历史：先得到明确确认，再 force push。
- 清洗后立刻验证 GitHub Release、tag、默认分支和仓库可见性。

最重要的规则：历史重写是发布策略，不是普通整理动作。以后必须单独确认。
