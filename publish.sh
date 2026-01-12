#!/bin/bash
# Longport MCP 发布脚本
# 使用方法: ./publish.sh [test|prod]

set -e

MODE=${1:-test}

echo "=== Longport MCP 发布脚本 ==="

# 清理旧的构建文件
echo "[1/4] 清理旧文件..."
rm -rf dist/ build/ *.egg-info/

# 构建
echo "[2/4] 构建包..."
uv build

# 检查构建结果
echo "[3/4] 构建完成，文件列表:"
ls -la dist/

# 发布
echo "[4/4] 发布到 PyPI..."
if [ "$MODE" = "prod" ]; then
    echo "发布到 PyPI (生产环境)..."
    uv publish
else
    echo "发布到 TestPyPI (测试环境)..."
    uv publish --publish-url https://test.pypi.org/legacy/
fi

echo ""
echo "=== 发布完成 ==="
echo ""
if [ "$MODE" = "prod" ]; then
    echo "安装命令: uvx longport-mcp"
    echo "或: pip install longport-mcp"
else
    echo "测试安装: uvx --index-url https://test.pypi.org/simple/ longport-mcp"
fi
