#!/bin/bash
# 双击这个文件即可启动创业沙盒（macOS）
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo ""
  echo "  这台 Mac 上没有找到 python3。"
  echo "  请先安装 Python 3：https://www.python.org/downloads/"
  echo "  （装完把这个文件重新双击一次即可）"
  echo ""
  read -n 1 -s -r -p "  按任意键关闭…"
  exit 1
fi

python3 start.py
echo ""
read -n 1 -s -r -p "  服务已停止，按任意键关闭窗口…"
