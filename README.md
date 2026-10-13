# WorkBuddy BOSS 直聘技能的 Git 管理

实际运行目录也是主维护仓库：
`C:\Users\wxj20\.workbuddy\skills\boss-zhipin-deliver`

原开发副本：`D:\Dev\skill\boss-zhipin-deliver-workbuddy`
原始回滚基线标签：`baseline-2026-10-13`

## 保存修改

在实际安装目录执行 `git status`、`git diff`，核对后按需 `git add` 和 `git commit`。
修改该目录中的技能代码会直接影响 WorkBuddy 后续运行；回滚或切换版本应避开运行中的任务。
不要同时在两个副本修改同一文件后直接覆盖同步。

个人配置 `user_profile.json`、记录 `boss_applied.json`、Python 缓存及临时备份不再纳入新提交。
个人文件保留在本地；原始基线提交仍包含当时的配置，尚未改写历史。
投递运行数据和 HTML 仍位于 `D:\Dev\code\my_github\boss-zhipin-assistant\boss-data`。
未配置远程仓库。

## 旧同步与恢复工具

`tools/manage_skill.py` 仍供原开发副本使用，不应在实际安装目录运行（工具会拒绝源和目标相同）。
原开发副本有未提交修改时，`sync` 会停止，防止覆盖工作。
恢复前工具会备份安装目录，默认保留个人配置与投递记录。
