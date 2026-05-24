# SaveXTube WeChat ClawBot

微信 ClawBot 专用的视频下载机器人。用户在微信里给 ClawBot 发送视频链接，机器人下载后通过微信回传可播放的视频文件，发送成功后默认删除容器内本地文件，也可以配置为保留到本地 `downloads/`。

这个仓库是面向 NAS 自用部署的精简版，只保留微信入口和常用中文视频平台，不包含其他聊天机器人入口。一个容器可以同时运行多个 ClawBot session，适合家庭成员分别绑定自己的微信 ClawBot。

## 支持平台

| 平台 | 支持内容 | 登录 cookies 建议 |
| --- | --- | --- |
| B站 | BV/av/短链视频 | 建议配置，清晰度和会员内容依赖登录态 |
| 抖音 | 视频、图文/Live Photo 笔记、短链 | 建议配置，提高成功率 |
| 快手 | 分享链接、短链 | 建议配置，提高成功率 |
| 微博 | 视频微博、分享链接 | 建议配置，登录可见内容依赖登录态 |
| 头条视频 | 头条/西瓜常见链接 | 可选 |
| 小红书 | 视频笔记、图文笔记 | 建议配置，登录可见内容依赖登录态 |
| 微信视频号 | `https://weixin.qq.com/sph/...` 分享短链、`finder.video.qq.com` 直链 | sph 短链本地解析需要元宝 Web cookie |

## 功能特点

- 微信 ClawBot 扫码登录、轮询收消息、发送文本进度、上传并回传媒体文件。
- 支持单容器多 ClawBot session；每个 ClawBot 独立扫码登录，回传消息互不串号。
- 下载完成后通过 ClawBot CDN 以微信视频消息发送，手机微信更容易直接播放。
- 抖音视频和图文/Live Photo 会优先走移动分享页解析，减少 yt-dlp web 接口的 cookies/风控失败。
- 抖音图文/Live Photo 笔记会优先合成 H.264/AAC MP4；只有静态图片时回传图片文件。
- 回传前默认优先无损整理为微信更容易播放的 MP4；不兼容时才转为 H.264/AAC MP4，减少画质损失和 NAS 转码压力。
- B站短链和 H5 分享链接会规整为普通 BV 视频页，减少低清派生流影响。
- B站优先选择 AVC/MP4 原始流；最终清晰度取决于 cookies、账号权限和视频源本身。
- 微信视频号默认不接第三方解析 API；sph 分享短链由容器本地直连腾讯接口解析，需要配置自己的元宝 Web cookie。
- 已发送文件默认清理；需要留档时可关闭清理，文件保留在宿主机 `downloads/`。
- Docker Compose 本机构建镜像，不需要 DockerHub 账号，也不需要推送镜像。

## 工作流程

```text
微信消息
  -> 一个或多个 ClawBot getupdates
  -> 识别链接和平台
  -> yt-dlp / 抖音移动分享页解析器 / 小红书备用解析器 / 视频号本地解析器下载
  -> ffmpeg 快速整理或必要时转码为微信可播放 MP4
  -> ClawBot CDN 上传
  -> 微信视频消息回传
  -> 按配置清理或保留本地文件
```

## 目录结构

```text
.
├── Dockerfile
├── docker-compose.yml
├── savextube_wechat.py
├── clawbot_wechat.py
├── wechat_downloader.py
├── douyin_note_downloader.py
├── xiaohongshu_downloader.py
├── config_reader.py
├── requirements.txt
├── savextube.example.toml
└── DEPLOY_FNOS_WECHAT.md
```

运行时目录不进入 Git：

```text
config/
cookies/
downloads/
logs/
db/
```

## 快速开始

```bash
git clone https://github.com/kyfour/savextube-wechat-clawbot.git
cd savextube-wechat-clawbot
mkdir -p config cookies downloads logs db
cp savextube.example.toml config/savextube.toml
docker compose build
docker compose run --rm savextube-wechat login
docker compose up -d
```

首次 `login` 会输出 ClawBot 登录二维码链接。用手机微信扫码确认后，会在 `config/wechat_session.json` 保存登录态。

查看运行状态：

```bash
docker compose ps
docker logs -f savextube-wechat
```

## 配置

复制示例配置：

```bash
cp savextube.example.toml config/savextube.toml
```

常用配置：

```toml
[wechat]
allowed_user_ids = ""
progress_interval = 20
max_send_files = 20
max_concurrent_downloads = 1
config_reload_interval = 15
supported_platforms = "douyin,kuaishou,weibo,toutiao,xiaohongshu,bilibili,wechat_channels"
cleanup_after_send = true

[proxy]
# proxy_host = "http://192.168.1.2:7890"

[wechat_channels]
# 默认不填，不接第三方 API；容器本地直连腾讯接口解析。
# 如果你自建了 sph 解析服务，才显式设置 resolver_url。
# resolver_url = "https://your-worker.example.com/api/fetch_video_profile"
```

`allowed_user_ids` 留空表示所有能给机器人发消息的微信用户都可以使用。需要限制时，登录后从日志或 `config/wechat_session.json` 里取用户 ID 填入。

`cleanup_after_send = true` 表示微信发送成功后删除本地下载文件。想保留文件就改成：

```toml
cleanup_after_send = false
```

保留后文件会留在宿主机 `downloads/` 目录，后续需要手动清理。

## 多 ClawBot

如果要给多个人分别使用各自的微信 ClawBot，可以在同一个容器里配置多个 bot profile：

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
docker compose up -d
```

`run` 会启动所有 `enabled = true` 且已登录的 bot。运行中会按 `config_reload_interval` 定时重读 `savextube.toml`，新增 profile 并完成扫码登录后会自动启动，不需要重建镜像，也不需要重启当前 bot。

已有单 bot 登录不会丢。把第一个 profile 的 `session_file` 指向原来的 `/app/config/wechat_session.json`，就会继续使用原登录态；如果改成 `/app/config/wechat_me.json`，则需要重新扫码或手动复制原 session 文件。

多个 bot 共享同一个下载队列，`max_concurrent_downloads = 1` 时同一时间只跑一个下载任务，避免 NAS 同时转码过载。

## Cookies

Cookies 不是必需，但会影响清晰度、登录可见内容和解析成功率。文件放在宿主机 `cookies/` 目录，容器内路径为 `/app/cookies/`。

| 平台 | 文件名 | 先在浏览器确认登录 | 导出范围 |
| --- | --- | --- | --- |
| B站 | `bilibili_cookies.txt` | `https://www.bilibili.com` | `bilibili.com` |
| 抖音 | `douyin_cookies.txt` | `https://www.douyin.com` | `douyin.com` |
| 快手 | `kuaishou_cookies.txt` | `https://www.kuaishou.com` | `kuaishou.com` |
| 微博 | `weibo_cookies.txt` | `https://weibo.com` | `weibo.com` |
| 头条 | `toutiao_cookies.txt` | `https://www.toutiao.com` | `toutiao.com`；西瓜视频链接也可补导 `ixigua.com` |
| 小红书 | `xiaohongshu_cookies.txt` | `https://www.xiaohongshu.com` | `xiaohongshu.com` |
| 微信视频号 | `wechat_channels_yuanbao_cookies.txt` | `https://yuanbao.tencent.com` | `tencent.com`，需要包含父域 `.tencent.com` cookies |

推荐方式是用浏览器扩展 `Get cookies.txt LOCALLY` 导出：

1. 在本机 Chrome 里打开上表对应网站并确认已经登录。
2. 点击扩展，选择当前站点或上表的导出范围，导出 Netscape 格式 `cookies.txt`。
3. 按上表文件名保存到项目 `cookies/` 目录，例如 `cookies/bilibili_cookies.txt`。
4. 如果部署在 NAS，把这些 `.txt` 上传到 NAS 项目的 `cookies/` 目录；不需要重建镜像，下一次下载会重新读取。

视频号比较特殊：只打开 `yuanbao.tencent.com` 导出可能不够，必须让导出的文件里包含 `.tencent.com` 父域登录 cookies。最稳做法是在扩展里按 `tencent.com` 导出或启用子域导出，然后保存为 `cookies/wechat_channels_yuanbao_cookies.txt`。

也可以用本机浏览器的 yt-dlp 导出，适合 B站、抖音、快手、微博、头条、小红书。先安装：

```bash
python3 -m pip install -U yt-dlp
```

通用模板：

```bash
yt-dlp --cookies-from-browser chrome --cookies cookies/<文件名>.txt --simulate --skip-download "<该平台任意可访问的视频或笔记链接>"
```

B站示例：

```bash
yt-dlp --cookies-from-browser chrome --cookies cookies/bilibili_cookies.txt --simulate --skip-download "https://www.bilibili.com/video/BVxxxxxxxxxx"
```

如果使用 Edge，把 `chrome` 改为 `edge`。如果本机有多个 Chrome profile，可以用 `chrome:Profile 1` 这类写法指定 profile。yt-dlp 方式不适合微信视频号元宝 cookie，视频号优先用扩展按 `tencent.com` 导出。

不要把 cookies、微信 session、`.env`、日志或下载文件提交到 Git。

## 微信视频号

当前可验证的 NAS 方案是：在微信里复制视频号的 `https://weixin.qq.com/sph/...` 分享短链发给机器人。不要转发视频号小卡片，当前 ClawBot 不能稳定接收这类卡片消息，后端拿不到可解析 payload。机器人在容器内直连腾讯元宝解析接口拿到 `token/eid`，再直连腾讯视频号预览接口解析出 `finder.video.qq.com` MP4 地址并下载。

默认不会调用第三方公开解析 API。由于腾讯元宝解析接口需要登录态，请把你自己的元宝 Web cookie 保存到：

```text
cookies/wechat_channels_yuanbao_cookies.txt
```

也可以设置环境变量 `WECHAT_CHANNELS_YUANBAO_COOKIE`。这个 cookie 可以是 Netscape cookies.txt，也可以是一行原始 `a=b; c=d` Cookie header。

如果你自建了兼容 `/api/fetch_video_profile` 的解析服务，可以在 `config/savextube.toml` 里显式配置：

```toml
[wechat_channels]
resolver_url = "https://your-worker.example.com/api/fetch_video_profile"
```

视频号入口只按复制分享链接维护；小卡片不作为可用输入承诺。

## 抖音视频和图文

抖音普通视频和 `/note/` 图文笔记都会优先走移动分享页解析器，yt-dlp 只作为备用。这样可以避开部分 `aweme/detail` 接口返回空 JSON 导致的 `Fresh cookies` 错误。

图文笔记会下载非水印图片；如果笔记带音频或 Live Photo 素材，会用 `ffmpeg` 合成为手机微信可直接播放的 MP4。

如果抖音返回验证码/风控页面，机器人会返回明确错误。这种情况通常需要刷新 `cookies/douyin_cookies.txt`，或稍后重试。

## B站清晰度

B站未登录时经常只能拿到 360P/480P。配置有效登录 cookies 后，一般可以拿到 720P/1080P；高码率、4K、HDR 和大会员内容仍取决于账号权限和视频本身是否提供。

如果水印已经压进原视频画面，下载器无法无损去除。如果水印来自 Story/H5 分享页，本项目会先规整为普通 BV 视频页来尽量避开分享页派生流。

## 微信回传

视频会通过 ClawBot CDN 上传后以 `video_item` 发送。发送前默认先检查格式，兼容的 H.264/AAC MP4 只做快速整理和 `faststart`，不兼容时再转为 H.264/AAC MP4，以提高手机微信直接播放概率。

可选环境变量：

```yaml
WECHAT_FORCE_TRANSCODE_VIDEO: "false"
WECHAT_VIDEO_CRF: "20"
WECHAT_VIDEO_TRANSCODE_TIMEOUT: "1800"
WECHAT_MAX_CONCURRENT_DOWNLOADS: "1"
WECHAT_CLEANUP_AFTER_SEND: "true"
```

如果想强制每个视频都重新转码，把 `WECHAT_FORCE_TRANSCODE_VIDEO` 改为 `"true"`。如果想微信发送成功后保留本地原文件，把 `WECHAT_CLEANUP_AFTER_SEND` 改为 `"false"` 或在 `config/savextube.toml` 里设置 `cleanup_after_send = false`。

如果文件过大，可能受 ClawBot、CDN 或微信客户端限制。发送失败时，机器人会返回本地保存路径，并保留文件便于人工处理。

## Docker 和镜像

默认使用本机 Docker 构建镜像：

```bash
docker compose build
docker compose up -d
```

`docker-compose.yml` 里的 `image: savextube-wechat:local` 只是本地镜像名，不需要 DockerHub，也不会执行 `docker push`。

默认基础镜像：

```dockerfile
ARG PYTHON_IMAGE=docker.1ms.run/library/python:3.11-slim-bookworm
```

如环境可以直接访问 Docker Hub，可覆盖为官方镜像：

```bash
docker compose build --build-arg PYTHON_IMAGE=python:3.11-slim-bookworm
```

## 测试

不需要联网的最小自检：

```bash
python3 -m unittest discover -s tests -v
```

部署到 NAS 前建议至少跑一次语法和单元测试：

```bash
python3 -m py_compile savextube_wechat.py clawbot_wechat.py wechat_downloader.py douyin_note_downloader.py xiaohongshu_downloader.py config_reader.py
python3 -m unittest discover -s tests -v
```

## 飞牛 NAS

飞牛 NAS 部署步骤见 [DEPLOY_FNOS_WECHAT.md](DEPLOY_FNOS_WECHAT.md)。

建议部署目录：

```bash
/vol1/1000/docker/savextube-wechat
```

首次部署可以在 NAS 上直接克隆：

```bash
git clone https://github.com/kyfour/savextube-wechat-clawbot.git /vol1/1000/docker/savextube-wechat
cd /vol1/1000/docker/savextube-wechat
```

## 安全

- 不提交 `config/`、`cookies/`、`downloads/`、`logs/`、`db/`。
- 不提交微信 ClawBot token、平台 cookies、NAS 密码、代理账号或任何 `.env` 文件。
- 发布前执行敏感关键字扫描和 `git status --ignored`。
- GitHub 仓库只保存程序代码、示例配置和部署文档。

## 致谢

下载核心基于 [renlixing87/savextube](https://github.com/renlixing87/savextube) 改造。本仓库聚焦微信 ClawBot + NAS 部署场景。
