# 部署到香港 / 新加坡服务器

团队成员分别在国内和海外时，香港或新加坡的服务器两边都能打开，而且不需要 ICP 备案。下面以阿里云「轻量应用服务器 · 中国香港」为例，腾讯云 Lighthouse 香港、新加坡也是一样的步骤。

## 1. 买服务器（约 10 分钟）
1. 打开阿里云轻量应用服务器的购买页，地域选 **中国香港**（或新加坡）。
2. 镜像选 **Ubuntu 22.04**，配置选 **2 核 2 GB** 就够用（V2 跑语音转写时建议 4 GB）。价格以官网为准，一般每月几十元。
3. 买好后进入控制台 → 防火墙，放行 **80** 和 **443** 端口（22 端口默认是开的）。
4. 记下服务器的 **公网 IP**，并在控制台设置或重置 root 密码。

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

# 启动
docker compose up -d --build
```

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
然后打开 http://127.0.0.1:8000。加了 `mock` 时 AI 步骤不会调用真实模型，也不会产生费用。
