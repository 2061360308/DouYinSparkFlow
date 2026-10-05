#!/usr/bin/env bash
# 组装并打包 Linux .deb。
#
# 用法：
#   build-deb.sh <arch> <version> <pyinstaller_dist> <cloak_dir> <gost_bin> <logo_png> <out_deb>
# 例：
#   build-deb.sh amd64 3.2.2 dist/DouyinSparkFlow cloakbrowser-linux-x64 gost app/logo.png DouyinSparkFlow_3.2.2_amd64.deb
set -euo pipefail

ARCH="${1:?arch}"
VERSION="${2:?version}"
DIST="${3:?pyinstaller dist dir}"
CLOAK="${4:?cloak dir}"
GOST="${5:?gost binary}"
LOGO="${6:?logo png}"
OUT="${7:?output deb}"

HERE="$(cd "$(dirname "$0")" && pwd)"
PKG=douyin-spark-flow
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
# dpkg-deb --build <dir> 把 <dir> 当包根：<dir>/DEBIAN 是控制信息，
# <dir> 下其余目录按 / 平铺安装。所以数据必须和 DEBIAN 平级，不能再套一层 $PKG，
# 否则会装到 /douyin-spark-flow/usr/... 而不是 /usr/...
ROOT="$STAGE"

mkdir -p \
  "$ROOT/opt/$PKG" \
  "$ROOT/usr/bin" \
  "$ROOT/usr/share/applications" \
  "$ROOT/usr/share/icons/hicolor/256x256/apps" \
  "$ROOT/usr/share/icons/hicolor/512x512/apps" \
  "$ROOT/usr/share/doc/$PKG" \
  "$STAGE/DEBIAN"

# 1. PyInstaller 产物（exe + _internal + 前端资源）
cp -a "$DIST/." "$ROOT/opt/$PKG/"

# 2. 自带隐身 Chromium（目录名要与 app/paths.py 的 BROWSER_DIR_NAME 一致）
cp -a "$CLOAK" "$ROOT/opt/$PKG/$(basename "$CLOAK")"

# 3. 配套代理客户端
mkdir -p "$ROOT/opt/$PKG/gost"
install -m 0755 "$GOST" "$ROOT/opt/$PKG/gost/gost"

# 4. 启动器 / 桌面项 / 图标 / 版权
install -m 0755 "$HERE/douyin-spark-flow" "$ROOT/usr/bin/$PKG"
install -m 0644 "$HERE/douyin-spark-flow.desktop" "$ROOT/usr/share/applications/$PKG.desktop"
install -m 0644 "$LOGO" "$ROOT/usr/share/icons/hicolor/256x256/apps/$PKG.png"
install -m 0644 "$LOGO" "$ROOT/usr/share/icons/hicolor/512x512/apps/$PKG.png"
install -m 0644 "$HERE/copyright" "$ROOT/usr/share/doc/$PKG/copyright"

# 5. control + 维护脚本
sed -e "s/@ARCH@/$ARCH/g" -e "s/@VERSION@/$VERSION/g" \
  "$HERE/control.in" > "$STAGE/DEBIAN/control"
install -m 0755 "$HERE/postinst" "$STAGE/DEBIAN/postinst"
install -m 0755 "$HERE/prerm" "$STAGE/DEBIAN/prerm"
install -m 0755 "$HERE/postrm" "$STAGE/DEBIAN/postrm"

# 6. Chromium 沙箱辅助程序：setuid 位（--root-owner-group 会强制 root:root）
for sb in "$ROOT/opt/$PKG"/cloakbrowser-linux-*/chrome-sandbox; do
  [ -e "$sb" ] || continue
  chmod 4755 "$sb" 2>/dev/null || true
done

dpkg-deb --build --root-owner-group "$STAGE" "$OUT"
echo "built: $OUT"
