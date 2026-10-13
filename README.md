# WorkBuddy BOSS 直聘技能使用版

这是本机正在使用的修复版快照，来源：
`C:\Users\wxj20\.workbuddy\skills\boss-zhipin-deliver`

仓库位置：`D:\Dev\skill\boss-zhipin-deliver-workbuddy`
初始回滚基线标签：`baseline-2026-10-13`

## 保存后续修改

先确认 WorkBuddy 的修改可用，再执行：

```powershell
cd D:\Dev\skill\boss-zhipin-deliver-workbuddy
python .\tools\manage_skill.py sync
git diff --stat
git diff
git add .
git commit -m "保存 WorkBuddy 技能更新"
```

`sync` 会把已安装技能复制到仓库供审阅，不自动提交。仓库已有未提交修改时会停止，以免覆盖尚未保存的工作。原始基线标签一直保留。

## 恢复当前修复版

先结束正在修改或运行该技能的 WorkBuddy 任务，再执行：

```powershell
cd D:\Dev\skill\boss-zhipin-deliver-workbuddy
python .\tools\manage_skill.py restore baseline-2026-10-13
```

恢复前会把当时的技能文件备份到 `.restore-backups/时间戳/`。默认保留已安装目录里的 `user_profile.json` 和 `boss_applied.json`，仅恢复技能说明、代码、参考文档及资源。仓库里仍保存这两个配置文件的基线版本。

如需连同上述配置一起恢复，显式加参数：

```powershell
python .\tools\manage_skill.py restore baseline-2026-10-13 --include-profile
```

也可以把标签替换为任意已提交的版本：

```powershell
git log --oneline --decorate
python .\tools\manage_skill.py restore <提交ID>
```

恢复只覆盖选定提交包含的技能文件，不删除安装目录里额外新增的文件；不会移动本仓库的 HEAD。

## 范围

- 这是独立的本地 Git 仓库，没有配置远程地址。
- 保留当前使用版全部21个技能文件（含本机求职配置），排除Python缓存和临时文件。
- `D:\Dev\code\my_github\boss-zhipin-assistant\workbuddy` 的原仓库副本独立保留。
- 实际投递记录与HTML在 `D:\Dev\code\my_github\boss-zhipin-assistant\boss-data`，不在此技能仓库的恢复范围内。
