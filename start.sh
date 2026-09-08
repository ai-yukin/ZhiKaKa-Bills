#!/bin/bash
# 知卡卡账单 - 本地信用卡管理工具启动脚本（macOS/Linux）

echo "================================================"
echo "  知卡卡账单 - 本地信用卡管理工具"
echo "================================================"
echo ""

cd "$(dirname "$0")"

# 检查 Python 是否安装
if ! command -v python3 &> /dev/null; then
    echo "[错误] 未检测到 Python3，请先安装 Python 3.8+"
    echo "下载地址: https://www.python.org/downloads/"
    exit 1
fi

# 检查依赖是否安装
if ! python3 -c "import flask" &> /dev/null; then
    echo "[提示] 首次运行，正在安装依赖..."
    pip3 install -r requirements.txt
    if [ $? -ne 0 ]; then
        echo "[错误] 依赖安装失败，请检查网络连接"
        exit 1
    fi
fi

echo ""
echo "启动中..."
echo "访问地址: http://localhost:5000"
echo "按 Ctrl+C 停止服务"
echo ""

python3 server.py
