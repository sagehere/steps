# Zepp Life 多账户步数计划

## 管理员登录与今日时间轴

- 登录页可勾选“记住我（30 天）”。普通登录使用会话 Cookie；记住我使用独立的持久 Cookie，浏览器中不保存管理员密码，数据库只保存随机令牌的摘要。
- 当普通会话结束后，系统可使用有效凭据自动登录。每次自动登录成功都会换发新凭据，旧凭据立即失效，并从这次登录重新计算 30 天期限。普通页面访问不会不断轮换持久凭据。
- 退出登录会撤销当前浏览器的持久凭据；其他浏览器不受影响。更改 `ADMIN_PASSWORD` 并重启服务后，所有旧会话和持久凭据失效。Cookie 的 HTTPS 要求沿用 `COOKIE_SECURE` 配置。升级时自动创建凭据表，无需手动迁移；旧版本会话需重新登录一次。
- 仪表盘时间轴按 `TZ` 展示当天 `00:00—24:00`。每个圆点代表一个账号的一个计划时间点，采用当天随机偏移后的计划时间，执行后只改变颜色，不移动位置。
- 绿色为成功、红色为最终失败；其他状态为灰色。悬停、键盘聚焦或点击圆点可查看完整备注、脱敏账号、计划时间和具体状态；再次点击、按 Escape 或点击外部可关闭固定详情。
- 默认“全部”展示所有已分配计划账号，包含停用账号和停用计划；切换“按计划查看”可选择单个计划。手动任务和登录测试仍在执行记录查看。相同或接近时间的圆点向上错层，密集数据在图表内滚动查看。
- 时间轴每 30 秒局部刷新，保留筛选；后台标签页暂停刷新，回到页面时立即更新。跨天自动切换当天数据；刷新失败保留上次结果并提示重试。
- 首次打开图表可能创建账号的当天随机时间快照，与“今日计划”和后台调度共用。同一天刷新不会重新抽取偏移；快照生成后新增时间点次日生效，删除时间点立即生效。查看图表本身不会触发执行任务。

## 账户与记录管理

- 在“账户”页勾选一个或多个账户，选择计划后点击“批量配置计划”；选择“取消计划分配”可一次移除选中账户的计划。停用账户同样可以配置计划。
- “执行记录”页顶部可按账户筛选；默认显示所有账户。
- 系统仅保留最近 7×24 小时的任务记录。应用启动及后台运行时会自动清理过期的成功、失败、排队和运行任务。
- 页面会根据屏幕宽度切换为移动端单列布局和底部导航，无需额外配置。

一个适合部署在 VPS 上的私有管理网站。支持多个 Zepp Life 账户、批量执行、固定步数加全局随机偏移、每日多时间点计划、计划时间随机偏移、失败重试和执行记录。

Docker 镜像只发布到 GitHub Container Registry：

```text
ghcr.io/sagehere/zepp-steps:latest
```

镜像同时支持常见的 `linux/amd64`（Intel/AMD VPS）和 `linux/arm64`（ARM VPS、甲骨文 ARM 等）平台，Docker 会自动选择正确版本。

> 本项目仅支持 Zepp Life 账号密码，不支持小米账号 SSO 或验证码登录。新账号通常需要先在 Zepp Life 中绑定有效设备。上游接口可能变更或限流，请勿用于公开服务。

## 一、准备 VPS

以下教程以 Ubuntu/Debian 为例。建议至少准备：

- 1 核 CPU、512 MB 内存。
- 已安装 Docker 和 Docker Compose。
- 一个未被其他程序占用的端口，默认使用 `101`。

先登录 VPS，检查 Docker：

```bash
docker --version
docker compose version
```

如果提示找不到命令，请按照 [Docker 官方安装教程](https://docs.docker.com/engine/install/) 安装 Docker Engine，再继续下面步骤。

## 二、下载部署配置

创建目录并下载两个配置文件：

```bash
sudo mkdir -p /opt/steps
sudo chown "$USER":"$USER" /opt/steps
cd /opt/steps

curl -O https://raw.githubusercontent.com/sagehere/steps/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/sagehere/steps/main/.env.example
mkdir -p data
```

如果没有 `curl`，可以先安装：

```bash
sudo apt update
sudo apt install -y curl
```

## 三、生成密钥并修改配置

生成随机应用密钥：

```bash
openssl rand -hex 32
```

复制输出结果，然后编辑 `.env`：

```bash
nano .env
```

配置示例：

```dotenv
ADMIN_PASSWORD=请换成一个强管理密码
APP_SECRET=粘贴刚才生成的64位随机字符串
TZ=Asia/Shanghai
REQUEST_INTERVAL_SECONDS=5
COOKIE_SECURE=false
```

保存方法：按 `Ctrl+O`、回车，再按 `Ctrl+X`。

配置项说明：

| 配置 | 说明 |
| --- | --- |
| `ADMIN_PASSWORD` | 网站管理员登录密码，必须修改。 |
| `APP_SECRET` | 加密账户密码和登录状态的密钥，至少 32 字符，必须备份且不能随意更换。 |
| `TZ` | 计划时区，国内用户保持 `Asia/Shanghai`。 |
| `REQUEST_INTERVAL_SECONDS` | 多账户提交间隔，默认 5 秒；账户很多或出现 429 时适当增大。 |
| `COOKIE_SECURE` | 直接使用 IP 和 HTTP 时设为 `false`；配置 HTTPS 后改为 `true`。 |

## 四、启动网站

在 `/opt/steps` 目录运行：

```bash
docker compose pull
docker compose up -d
```

查看状态：

```bash
docker compose ps
docker compose logs --tail=100
```

浏览器访问：

```text
http://你的VPS公网IP:101
```

使用 `.env` 中的 `ADMIN_PASSWORD` 登录。如果 VPS 启用了防火墙，需要放行端口：

```bash
sudo ufw allow 101/tcp
```

建议正式使用时通过 Nginx、Caddy 或宝塔反向代理配置 HTTPS，然后把 `COOKIE_SECURE` 改成 `true` 并重启容器。

## 五、第一次使用

1. 打开“计划”，新建一个每日计划。
2. 添加时间点，例如 `08:00 → 3000`、`12:00 → 7000`、`22:00 → 18000`。
3. 打开“账户”，逐个添加或按每行 `账号,密码,备注` 批量导入。
4. 给账户分配计划，先点击“测试”确认可以登录。
5. 可勾选多个账户手动执行；自动计划会在后台每日运行。

时间点填写的是“当天累计总步数”，不是增加多少步。后一时间点不能低于前一时间点，防止步数倒退。VPS 错过执行时间后，只补跑当天最新的到期目标。

每个账户行的“今日计划”可查看当天实际采用的时间点、偏移和执行状态。打开页面会在当天尚未生成时创建该账户的计划快照；同一账户当天再次查看或后台调度都会使用同一份结果。

## 代理与随机步数

- 在“设置”页面可配置全局 SOCKS5 代理。代理格式为 `socks5h://[用户名:密码@]主机:端口`，也支持 `socks5://`；推荐使用 `socks5h://` 让域名解析同样经过代理。保存后点击“验证已保存代理”，成功会显示代理出口 IP。
- 设置代理后，Zepp 登录、令牌验证、设备查询和步数上传都会通过该代理完成；代理不可用时任务会失败，不会改为直连。
- 每个计划时间点和手动执行只填写一个固定步数。随机开关开启时，系统为每个账户、每个新任务独立计算“固定步数 + 随机偏移”；失败任务重试时保留首次生成的目标步数。
- 默认随机偏移范围是 `-100` 至 `100`，默认关闭。启用时固定步数加随机下限必须至少为 1；若随机后的目标低于该账户当天已成功步数，任务会记录为“已跳过”。
- 每个计划详情页可单独启用“随机时间”，默认范围为 `-10` 至 `10` 分钟且默认关闭。启用后，账户每天首次调度或首次查看今日计划时，按原时间顺序为所有时间点分别抽取偏移；当天固定，次日重新抽取。
- 随机时间必须让全部时间点留在当天内。独立偏移可能让两个时间点同刻或顺序颠倒；系统按最终时间执行，发生步数倒退时会按原有规则跳过较低目标。当天修改随机时间配置或新增时间点从次日开始采用，删除时间点立即失效。
- 升级后，旧版“最小值–最大值”计划会自动转换为原最小值对应的固定步数。



## 数据目录权限错误

如果旧镜像日志出现 `sqlite3.OperationalError: unable to open database file`，这是宿主机的 `./data` 挂载目录覆盖了镜像内权限设置所致。升级到包含此修复的镜像即可，**不要删除 `data`，也不需要手动修改已有数据库**：

```bash
cd /opt/steps
docker compose pull
docker compose up -d
docker compose logs --tail=100
```

新镜像会在启动时仅以 root 修复 `/data` 及其中已有文件的所有者为 UID/GID `10001`，随后立即降权为 `app` 用户运行网站。`/data` 必须是本应用专用的挂载目录；不要将其他重要目录挂载到这里。
## 端口无法访问

如果容器显示 `Up/healthy`，但访问 `http://VPS地址:101` 被拒绝，先检查本地 Compose 文件是否真的发布了 101 端口。**`docker compose pull` 只更新镜像，不会更新本地 `docker-compose.yml`。** 不要删除 `.env` 或 `data`，只需把端口改为下面的内容并重建容器：

```yaml
ports:
  - "101:8000"
```

```bash
cd /opt/steps
nano docker-compose.yml
docker compose pull
docker compose up -d --force-recreate
docker compose ps
curl http://127.0.0.1:101/healthz
docker compose logs --tail=100
```

`docker compose ps` 应显示 `0.0.0.0:101->8000/tcp`，本机健康检查应返回 `{"ok":true}`。如果本机健康检查已成功、但公网仍不能访问，再放行 VPS 防火墙和云厂商安全组的 TCP `101` 端口。

## 六、更新、备份和卸载

更新到最新镜像：

```bash
cd /opt/steps
docker compose pull
docker compose up -d
```

数据库保存在 `/opt/steps/data/app.db`。备份前先停止服务，复制数据库，再启动：

```bash
cd /opt/steps
docker compose down
cp data/app.db "data/app.db.backup-$(date +%F)"
docker compose up -d
```

恢复时停止服务，用备份文件覆盖 `data/app.db` 后重新启动。恢复数据库时必须同时使用原来的 `APP_SECRET`，否则已保存的账户密码无法解密。

停止或卸载：

```bash
cd /opt/steps
docker compose down
```

该命令不会删除 `data` 目录。确认不再需要数据后再手动删除 `/opt/steps`。

## 七、镜像发布说明

每次代码推送到 `main` 后，GitHub Actions 会先运行测试，再构建 `linux/amd64` 和 `linux/arm64` 镜像，只推送到 GHCR。发布标签包括：

- `latest`：`main` 分支最新成功版本。
- `sha-xxxxxxx`：与某次 Git 提交对应，适合固定版本。
- `1.2.3`、`1.2`：推送 `v1.2.3` Git 标签时生成。

仓库管理员首次构建完成后，需要进入 GitHub 仓库右侧 **Packages → steps → Package settings → Change visibility**，将镜像设为 **Public**。否则 VPS 拉取私有镜像时需要额外配置 GitHub Token。

## 开发验证

```bash
python -m pip install -r requirements.txt
python -m unittest -v
```

Zepp Life 协议工具源自 [TonyJiangWJ/mimotion](https://github.com/TonyJiangWJ/mimotion)，按 Apache-2.0 许可证使用，完整许可见 [LICENSE](LICENSE)。
