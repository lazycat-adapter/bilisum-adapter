# 复用登录组件

`content/login-compat` 是一份可独立打进 LPK 的无依赖浏览器组件。本仓库直接随包分发。其他 adapter 可以从本仓库固定的提交或标签复制这个目录，并用 `file:///lzcapp/pkg/content/login-compat/session.js` 引用。升级组件由各 adapter 自行选择版本。

浏览器 inject 的 `params` 传入三个同源绝对路径。

| 参数 | 应用侧约定 |
| --- | --- |
| `statusPath` | GET 返回 JSON，`authenticated` 为布尔值。已有稳定登录态时继续使用当前页面。可选的 `canAutoBind: true` 表示后端已验证旧身份与当前身份一致，可以在当前页面后台绑定。 |
| `sessionPath` | POST，携带浏览器 Cookie 与 `X-Requested-With: LazyCat-Login`。应用验证当前用户及请求凭据后签发 HttpOnly 会话，返回 2xx。需要人工用原账号确认时返回 409。 |
| `oidcPath` | 用户主动点击后使用的原应用 OIDC 登录入口。可选 `providerName` 修改链接文字。 |

会话接口接收来自已认证网关的用户身份。请求阶段必须先覆盖浏览器提供的内部身份头，服务端必须校验凭据及用户归属。给每个安装实例使用独立的稳定密钥，只在服务端 request inject 和应用进程之间传递。BiliSum 的规则见根目录的 `lzc-manifest.yml`。其他应用用自己的会话格式、身份绑定和迁移代码实现上述接口。

使用普通 HTML 用户名密码表单的应用也可以直接采用官方 `builtin://simple-inject-password`。把应用自己的字段选择器和按当前用户持久化的凭据传给内置脚本。需要一次性确认旧 OIDC 身份时，先完成可信账号关联，再开启表单自动提交。[参数和改密示例](https://developer.lazycat.cloud/advanced-inject-passwordless-login.html)

一次迁移至少验证新安装免登录、旧 Cookie、旧 OIDC 回调、登录后可继续读写原数据、第二用户隔离，以及重启后的重复迁移。这里的浏览器模块不会自动推断历史记录属于哪个用户。
