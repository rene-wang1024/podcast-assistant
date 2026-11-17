# 部署到新加坡服务器

团队成员分别在国内和海外时，新加坡的服务器两边都能打开，不需要 ICP 备案，也能直接调用 OpenAI、Anthropic 的 API（中国内地和香港不在这两家的服务地区内，所以不选香港）。下面以阿里云「轻量应用服务器 · 新加坡」为例，腾讯云 Lighthouse 新加坡也是一样的步骤。

## 1. 买服务器（约 10 分钟）
1. 打开阿里云轻量应用服务器的购买页，地域选 **新加坡**。
2. 镜像选 **Ubuntu 22.04**，配置选 **2 核 4 GB** 以上（服务器要做语音转写，2 GB 内存不够）。价格以官网为准。
3. **硬盘要够放原片**：原片、粗剪视频和成品都存在服务器上。一期单机位 1080p 原片大约 20–60 GB，建议系统盘之外再挂一块 200 GB 以上的数据盘，挂到 `podcast-assistant/data`。以后接入阿里云 OSS 后，大文件改存 OSS，就不需要大硬盘了。
4. 买好后进入控制台 → 防火墙，放行 **80** 和 **443** 端口（22 端口默认是开的）。
5. 记下服务器的 **公网 IP**，并在控制台设置或重置 root 密码。

## 2. 安装并启动（约 10 分钟）
在电脑的终端里登录服务器：

```bash
ssh root@你的公网IP
```

然后依次运行：

```bash
# 安装 Docker
curl -fsSL https://get.docker.com | sh

# 下载工作台
git clone https://github.com/rene-wang1024/podcast-assistant.git
cd podcast-assistant

# 填配置
cp .env.example .env
sed -i "s/^SECRET_KEY=.*/SECRET_KEY=$(openssl rand -hex 32)/" .env
nano .env      # 填 OPENAI_API_KEY、ANTHROPIC_API_KEY；Ctrl+O 保存，Ctrl+X 退出

# 启动（第一次会下载约 230 MB 的语音转写模型）
docker compose up -d --build
```

不需要服务器做语音转写（比如内存只有 2 GB）时，改用 `docker compose build --build-arg ASR=0 && docker compose up -d`。音频照样会自动提取，只是不出转写稿。

在浏览器打开 `http://你的公网IP`，创建管理员账号。

## 3. 绑定域名并开启 HTTPS（推荐）
1. 在域名服务商那里加一条 **A 记录**，例如 `studio.你的域名.com` 指向服务器公网 IP。
2. 修改 `.env`：
   ```
   DOMAIN=studio.你的域名.com
   COOKIE_SECURE=1
   ```
3. 运行 `docker compose up -d`。Caddy 会自动申请 HTTPS 证书，几分钟后就能用 `https://studio.你的域名.com` 打开。
4. 在工作台的「设置」里把「工作台网址」填成这个地址，企业微信通知里的链接就能直接点开。

## 4. 接企业微信群机器人
1. 建一个企业微信群，把团队成员都拉进来。用个人微信的伙伴也可以加入企业微信群。
2. 群设置 → 群机器人 → 添加机器人 → 复制 Webhook 地址。
3. 粘贴到工作台「设置 → 企业微信通知」，然后点「发一条测试消息」确认能收到。
4. 在「成员」页给每个人填手机号，必须是对方企业微信绑定的手机号，轮到谁时才能 @ 到他。

## 日常维护
| 要做什么 | 命令（在 `podcast-assistant` 目录里运行） |
|---|---|
| 更新到最新版 | `git pull && docker compose up -d --build` |
| 查看日志 | `docker compose logs -f app` |
| 备份 | 打包 `data/` 目录，里面是数据库和所有上传的文件：`tar czf backup-$(date +%F).tgz data` |
| 重启 | `docker compose restart` |

## 不想马上买服务器：先在自己电脑上试
```bash
git clone https://github.com/rene-wang1024/podcast-assistant.git && cd podcast-assistant
pip install -r requirements.txt
PIPELINE_PROVIDER_OVERRIDE=mock uvicorn app.main:app --port 8000
```
需要先装好 FFmpeg（Mac：`brew install ffmpeg`）。想在本机试语音转写，下载 [SenseVoice 模型](https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2)，解压后启动时加 `ASR_MODEL_DIR=解压后的目录`。
然后打开 http://127.0.0.1:8000。加了 `mock` 时 AI 步骤不会调用真实模型，也不会产生费用。
