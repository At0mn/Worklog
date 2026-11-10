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

echo "正在启动 工作记录 ..."
echo "（重复双击只会打开页面，不会重复启动；关闭窗口程序可能继续运行）"
sleep 1
python3 main.py
code=$?

if [ "$code" = "2" ]; then
  # 程序已在运行，本次只是打开了页面，直接关闭窗口
  sleep 2
  exit 0
fi

echo ""
echo "程序已退出。若需停止后台进程，可在终端执行: pkill -f main.py"
read -r -p "按回车关闭窗口..."
