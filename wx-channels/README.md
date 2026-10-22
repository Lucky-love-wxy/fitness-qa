# 第 1 步：下载视频号视频

视频号视频用开源工具 [ltaoo/wx_channels_download](https://github.com/ltaoo/wx_channels_download) 下载，本仓库不附带它的程序，请到上游下载。它在本机起一个代理，给微信 PC 端的视频号页面注入下载按钮。

## 做法（macOS）
1. 从上游 [Releases](https://github.com/ltaoo/wx_channels_download/releases) 下载对应平台的包（Apple 芯片选 `darwin_arm64`），解压到任意目录，比如本目录。
2. 按上游 README 启动：首次运行需要管理员权限，它会安装自己的根证书，然后在本机起代理（默认接口 2022、代理 2023）。终端提示「代理服务启动成功」就可以用了。
3. 用 ⌘Q 完全退出微信再重新打开，进视频号播放一条视频，然后暂停。视频下方的操作栏会多出下载按钮，如果没有，页面侧边或底部会有悬浮按钮。下载菜单里可以选画质。
4. 下好的视频默认在系统「下载」文件夹，可以在工具目录的 `config.yaml` 里用 `download.dir` 改到别处。

## 作者实际用的几处配置（`config.yaml`）
- `channels.download.defaultHighest: true`：默认下最高画质。上游默认是当前播放画质，一般是最小的那档。
- `download.resourceConcurrency: 3`、`segmentConcurrency: 2`：并发调低，减少被限流。
- `proxy.upstreamProxy`：如果平时开着 Clash 之类的代理，这里填它的地址（如 `http://127.0.0.1:7890`），并在代理软件里关掉「系统代理」开关，不然它会把系统代理抢回去，微信流量就不经过下载器了。
- `download.dir` 指向工具目录下的 `downloads/`，这样第 2 步转写时能自动找到封面（见下文）。

## 和第 2 步的衔接
工具把每条视频的标题和封面地址记在它目录下的 `data.db`。如果视频在 `<工具目录>/downloads/` 里，`transcribe/transcribe.py` 会自动去上一级读这个库，给逐字稿文件夹配上原始封面。放在别处时用环境变量 `VT_WX_DB=/path/to/data.db` 指定，找不到就截视频第 1 秒当封面，不影响转写。

```bash
python3 transcribe/transcribe.py /path/to/wx_channels_download/downloads
```

## 注意
- 这个工具靠安装根证书、拦截 HTTPS 流量来工作。只在自己电脑上用，用完停掉服务、把系统代理改回去；不再使用时，在「钥匙串访问」里删掉它装的证书。
- 上游许可证是 MIT 加 Commons Clause，不能用于售卖。
- 只下载你有权使用的内容，下载的视频和逐字稿不要二次公开分发。
