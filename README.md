# BiliSum 懒猫微服适配器

本仓库跟踪 [lycohana/BiliSum](https://github.com/lycohana/BiliSum) 的稳定 `vX.Y.Z` 标签，为已有应用 `cloud.lazycat.app.bilisum1` 构建、验证定制镜像并向懒猫官方商店提交更新。起点是 [1.19.2 商店包](https://dl.lazycatmicroserver.com/appstore/lpks/pkgs/cloud.lazycat.app.bilisum1/cloud.lazycat.app.bilisum1-v1.19.2.lpk)。应用按用户启动独立实例。

## 打开与升级

已登录微服的新用户点开应用即可进入。浏览器 inject 调用本应用的会话接口，网关在服务端提供实例专用凭据及已验证的用户身份。浏览器不保存访问密钥。手动输入原有 BiliSum 访问密钥的方式继续可用。

旧版 OIDC 登录入口仍是本站的 `/api/v1/auth/oidc/login`。升级用户在原实例可以沿用有效的旧会话。需要确认原身份时，用原来的懒猫账号登录一次，关联完成后就能通过 inject 继续访问原来的对话。手动输入旧共享密钥仍对应旧的共享身份。

数据库保存在 `/lzcapp/var/data/video_sum.db`，在容器中是 `/data/video_sum.db`。首次迁移前会在同一目录保存 SQLite 一致性快照。迁移保留对话 ID、所有者、消息顺序及原始消息 JSON，能重复启动。如果迁移报错，服务启动失败，数据库事务回滚。还原时应先停止应用、另外保全当前数据库及 WAL/SHM 文件，再将同版本快照作为旧版数据库恢复。个人网盘的导出目录继续使用 `BiliSum Exports`，需要授予文稿写权限，在微服系统 1.7.0 起须由管理员明确授权兼容挂载。[微服文件权限说明](https://developer.lazycat.cloud/advanced-file.html)

## 使用 GitHub Actions

打开 [Update BiliSum for LazyCat](https://github.com/lazycat-adapter/bilisum-adapter/actions/workflows/update.yml)，选择 **Run workflow** 和 **main**。勾选 **dry-run** 会验证镜像和升级流程；取消勾选后，如发现符合条件的新版本，会转存经过测试的镜像、生成 LPK 并送审。工作流每天北京时间 **12:37** 定时检查，GitHub 的实际排队时间可能有延迟。可以在 [Releases](https://github.com/lazycat-adapter/bilisum-adapter/releases) 查看已生成的版本包。

工作流通过 `secrets: inherit` 使用组织的 `LZC_API_TOKEN`。开发者需要在懒猫平台拥有该包的协作者权限。每次打包由共享执行器替换清单里的镜像为 `registry.lazycat.cloud` 的转存结果。仓库中的初始清单引用真实的旧镜像和旧包版本，正式打包会对照升级测试的镜像凭证，核对转存摘要与镜像 ID。缺少凭证时，直接运行 `lzc-cli project build` 会停止。生成候选版本请使用工作流。

## 本地验证

在仓库根目录执行，需要 Git、Python 3、Node.js 和能拉取镜像的 Docker Buildx。完整验证会下载上游镜像和旧商店镜像，并创建临时网络和数据卷。

```sh
node --test tests/login.test.cjs
LAZYCAT_SOURCE_VERSION=1.21.3 python3 scripts/prepare.py --verify-only
```

镜像内执行后端测试。随后启动真实旧商店容器，在模拟的 OIDC 服务下建立两个身份和多轮对话，让新容器接管相同数据卷，并检验重启、两种登录方式和用户隔离。真实微服的文件选择器还应手工测试本地文件、个人网盘和有写权限的共享目录。

可复用登录组件的接入说明见 [登录组件](docs/login-compat.md)。文件选择器脚本来自[懒猫官方的文件选择器文档](https://developer.lazycat.cloud/lazycat-file-picker-auto-intercept.html)，并配套同源文件桥接。登录注入遵循[懒猫官方免密登录文档](https://developer.lazycat.cloud/advanced-inject-passwordless-login.html)。
