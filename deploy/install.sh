#!/usr/bin/env bash
# 阿里云轻量服务器一键部署：安装 Docker + 启动 NapCat 和机器人
# 用法：sudo bash deploy/install.sh
set -euo pipefail

APP_DIR="/opt/qq-bot"
if [[ $EUID -ne 0 ]]; then echo "请用 root 或 sudo 运行"; exit 1; fi

echo "==> 1/5 检查 Docker"
if ! command -v docker >/dev/null 2>&1; then
  echo "未检测到 Docker，尝试安装（阿里云镜像源）…"
  if command -v dnf >/dev/null 2>&1; then
    dnf install -y dnf-plugins-core
    dnf config-manager --add-repo https://mirrors.aliyun.com/docker-ce/linux/centos/docker-ce.repo
    dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
  elif command -v yum >/dev/null 2>&1; then
    yum install -y yum-utils
    yum-config-manager --add-repo https://mirrors.aliyun.com/docker-ce/linux/centos/docker-ce.repo
    yum install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
  else
    apt-get update
    apt-get install -y ca-certificates curl gnupg
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://mirrors.aliyun.com/docker-ce/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc 2>/dev/null || true
    curl -fsSL https://mirrors.aliyun.com/docker-ce/linux/debian/gpg -o /etc/apt/keyrings/docker.asc 2>/dev/null || true
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://mirrors.aliyun.com/docker-ce/linux/$(. /etc/os-release && echo $ID) $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
      > /etc/apt/sources.list.d/docker.list
    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
  fi
fi
systemctl enable --now docker

echo "==> 2/5 配置 Docker 镜像加速（国内拉不动镜像时用）"
mkdir -p /etc/docker
if [[ ! -f /etc/docker/daemon.json ]]; then
  cat > /etc/docker/daemon.json <<'EOF'
{
  "registry-mirrors": [
    "https://docker.1ms.run",
    "https://docker.xuanyuan.me",
    "https://dockerpull.org"
  ],
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
EOF
  systemctl restart docker
fi

echo "==> 3/5 复制程序到 $APP_DIR"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$APP_DIR"
if [[ "$SRC_DIR" != "$APP_DIR" ]]; then
  # 只复制程序文件，避免把本地的虚拟环境/测试数据带上去
  tar -C "$SRC_DIR" \
      --exclude=.venv --exclude=data --exclude=napcat --exclude=__pycache__ \
      --exclude=_selftest*.py -cf - . | tar -C "$APP_DIR" -xf -
fi
cd "$APP_DIR"
mkdir -p data napcat/config napcat/qq
chmod -R 777 napcat data || true

echo "==> 4/5 构建并启动容器"
docker compose up -d --build

echo "==> 5/5 安装开机自启"
if [[ -d /etc/systemd/system ]]; then
  cp deploy/qq-bot.service /etc/systemd/system/qq-bot.service 2>/dev/null || true
  systemctl daemon-reload
  systemctl enable qq-bot.service || true
fi

IP=$(curl -s --max-time 3 ifconfig.me || echo "服务器IP")
cat <<EOF

======================== 部署完成 ========================
管理后台 / NapCat WebUI 都只监听 127.0.0.1，从外面访问需要 SSH 隧道：
  ssh -N -L 3000:127.0.0.1:3000 -L 8080:127.0.0.1:8080 root@$IP
然后在本机浏览器打开：
  NapCat 扫码登录：  http://127.0.0.1:3000/webui/
  机器人管理后台：  http://127.0.0.1:8080

先登录 QQ（扫码），再进管理后台“设置”，把管理密码设好。
OneBot 地址已通过 compose 环境变量预设为 http://napcat:3001，一般无需修改。

常用命令：
  cd $APP_DIR
  docker compose logs -f bot     # 机器人日志
  docker compose logs -f napcat  # QQ 日志
  docker compose restart         # 重启
  docker stats                   # 看内存占用
==========================================================
EOF
