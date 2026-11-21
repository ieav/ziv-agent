# agent (ziv) 项目规则

## dist/ 产物不进 git(2026-11-22 用户裁定)

`dist/`(release.sh 的打包产物)**不跟踪 git**——在 .gitignore 里,发版后工作区的增删一律不用管、也不提交。原因:strip 二进制 zlib 压缩率仅 ~20% 且版本间无 delta,每发一版 .git 线性增长 ~55MB(实测 8 版累积到 488MB,已 filter-repo 清理)。

- 分发走 GitHub Releases(或其它制品渠道),git 只管源码
- `release.sh` / `install.sh` 行为不变,产物照常落在 dist/ 工作区
- 历史 commit 里若仍残留 dist 对象,属既成事实,不动
