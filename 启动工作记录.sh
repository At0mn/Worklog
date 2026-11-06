#!/bin/bash
# 工作记录 启动脚本（麒麟/Linux/macOS 通用）
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo "=================================================="
  echo "  未找到 python3。"
  echo "  麒麟 V10 一般系统自带；若没有，请用系统软件商店安装 python3。"
  echo "=================================================="
  read -r -p "按回车退出..."
  exit 1
fi

echo "正在启动 工作记录 ...（关闭本窗口即退出程序）"
sleep 1
python3 main.py

echo ""
read -r -p "程序已退出，按回车关闭窗口..."
