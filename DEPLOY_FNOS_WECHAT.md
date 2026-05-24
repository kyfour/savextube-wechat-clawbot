# SaveXTube 微信 ClawBot 版飞牛 NAS 部署

## 目标

这个部署只启动微信 ClawBot 入口。

用户给微信机器人发链接后，容器会下载到 `/downloads`，再通过 ClawBot 的 `getuploadurl` + CDN 上传 + `sendmessage` 以微信视频消息回传。文件成功回传后，默认会删除容器里的本地下载文件；如果要留档，可以配置为保留到宿主机 `downloads/`。

一个容器可以同时运行多个 ClawBot session。每个 ClawBot 独立扫码登录，消息从各自 ClawBot 回传，不会串到另一个微信账号。

默认支持平台只保留：抖音、快手、微博、头条视频、小红书、B站、微信视频号。其中抖音支持视频和图文/Live Photo 笔记。

## 目录

建议在飞牛 NAS 上放到：

```bash
/vol1/1000/docker/savextube-wechat
```

如果是首次部署，可以直接克隆仓库：

```bash
git clone https://github.com/kyfour/savextube-wechat-clawbot.git /vol1/1000/docker/savextube-wechat
cd /vol1/1000/docker/savextube-wechat
```

目录内需要这些子目录：

```bash
config
cookies
downloads
logs
db
```

## 配置

复制示例配置：

```bash
cp savextube.example.toml config/savextube.toml
```

按需编辑 `config/savextube.toml`：

```toml
[wechat]
allowed_user_ids = ""
progress_interval = 20
max_send_files = 20
max_concurrent_downloads = 1
config_reload_interval = 15
supported_platforms = "douyin,kuaishou,weibo,toutiao,xiaohongshu,bilibili,wechat_channels"
cleanup_after_send = true
```

`allowed_user_ids` 留空表示任何给机器人发消息的人都可用。登录成功后，`config/wechat_session.json` 中会出现 `user_id`，可以把它填入 `allowed_user_ids` 做白名单。

`cleanup_after_send = true` 表示微信发送成功后删除本地下载文件。要保留文件就改成：

```toml
cleanup_after_send = false
```

保留后的文件在 NAS 部署目录下的 `downloads/`。

## 首次登录

构建镜像：

```bash
docker compose build
```

扫码登录微信 ClawBot：

```bash
docker compose run --rm savextube-wechat login
```

终端会输出二维码链接。用手机微信扫码确认后，会生成：

```bash
config/wechat_session.json
```

## 多 ClawBot

在 `config/savextube.toml` 里添加多个 bot profile：

```toml
[wechat]
max_concurrent_downloads = 1
config_reload_interval = 15

[[wechat.bots]]
name = "me"
enabled = true
session_file = "/app/config/wechat_session.json"

[[wechat.bots]]
name = "wife"
enabled = true
session_file = "/app/config/wechat_wife.json"
```

分别扫码登录：

```bash
docker compose run --rm savextube-wechat login --bot me
docker compose run --rm savextube-wechat login --bot wife
```

启动后会同时轮询所有 `enabled = true` 的 ClawBot，并共享一个下载队列。

运行中会按 `config_reload_interval` 定时重读配置。新增 profile 并扫码登录后，会自动启动新增 ClawBot，不需要重建镜像，也不影响原来的 `wechat_session.json` 登录态。

## 启动

```bash
docker compose up -d
```

查看日志：

```bash
docker logs -f savextube-wechat
```

这里使用的是 NAS 本机镜像 `savextube-wechat:local`，不需要 DockerHub 账号，也不需要 `docker push`。

## 使用

在微信里给 ClawBot 发抖音、快手、微博、头条视频、小红书、B站或微信视频号链接：

```text
https://www.bilibili.com/video/BVxxxxxxxxxx
```

机器人会先回文字进度，下载完成后以微信文件形式发送本地下载文件。

视频发送前会用 `ffmpeg` 快速整理为微信更容易播放的 MP4；源文件不兼容时才转为 H.264/AAC MP4，尽量让手机微信直接播放。

抖音普通视频和 `/note/` 图文/Live Photo 会优先走移动分享页解析器，yt-dlp 只作为备用；有音频或 Live Photo 素材时会合成为 MP4，只有静态图片时回传图片文件。

## Cookies

如需更高清晰度或登录可见内容，把 Netscape 格式 cookies 放到 `cookies/` 目录：

```text
cookies/bilibili_cookies.txt
cookies/douyin_cookies.txt
cookies/kuaishou_cookies.txt
cookies/xiaohongshu_cookies.txt
cookies/weibo_cookies.txt
cookies/toutiao_cookies.txt
cookies/wechat_channels_yuanbao_cookies.txt
```

获取方式：

1. 在本机 Chrome 里登录对应平台：B站、抖音、快手、微博、头条/西瓜、小红书。
2. 用浏览器扩展 `Get cookies.txt LOCALLY` 导出 Netscape 格式 cookies，按上面的文件名保存。
3. 也可以用 `yt-dlp --cookies-from-browser chrome --cookies cookies/<文件名>.txt --simulate --skip-download "<平台视频链接>"` 从本机浏览器导出。
4. 微信视频号不要用 yt-dlp 导元宝 cookie；在 Chrome 打开并登录 `https://yuanbao.tencent.com`，用扩展按 `tencent.com` 范围导出，确保包含父域 `.tencent.com` cookies，然后保存为 `cookies/wechat_channels_yuanbao_cookies.txt`。

替换 cookies 文件后不需要重建镜像；下一次下载会重新读取。

B站未登录时经常只能拿到 360P/480P。普通登录通常可拿 720P/1080P；高码率、4K、HDR 仍取决于大会员和视频源本身。

微信视频号的稳定入口是 `https://weixin.qq.com/sph/...` 分享短链。不要转发视频号小卡片，当前 ClawBot 不能稳定接收这类卡片消息，后端拿不到可解析 payload。默认不接第三方公开解析 API，容器会本地直连腾讯元宝和视频号接口解析；因此需要把自己的元宝 Web cookie 放到 `cookies/wechat_channels_yuanbao_cookies.txt`，或设置 `WECHAT_CHANNELS_YUANBAO_COOKIE`。如果自建了解析服务，可以在 `config/savextube.toml` 里显式配置 `[wechat_channels].resolver_url`。

## 注意

微信文件回传受 ClawBot/CDN/微信端限制影响，超大文件可能上传或接收失败。失败时机器人会回传本地保存路径，并保留本地文件便于人工处理。

不要把 `config/`、`cookies/`、`downloads/`、`logs/`、`db/` 提交到 Git。
