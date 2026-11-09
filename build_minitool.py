#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把看板快照打包成符合小工具容器规范的离线 zip。

规范来源：.skill/minitool-zip-builder（SKILL.md + references/）

做四件事：
    1. 拆分模板：内联脚本外置为 assets/app.js，数据单独写成 assets/data.js
    2. 去掉联网能力：删除 /api/refresh 的 fetch 与自动刷新
    3. 兼容兜底：Chrome 61 的 flex gap / grid gap / inset，以及安全区
    4. 生成 dist/ 与 build/minitool-usage-dashboard.zip（index.html 在根）

用法:
    python build_minitool.py            # 构建 + 校验 + 打包
    python build_minitool.py --check    # 只校验已有的 dist/
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TPL_PATH = HERE / "template.html"
DATA_PATH = HERE / "dashboard-data.json"
VERSION_PATH = HERE / "VERSION"
DIST = HERE / "dist"
BUILD = HERE / "build"


def read_version():
    """与 dashboard.py 同一个来源：根目录 VERSION。产物名和页面页脚都用它。"""
    try:
        return VERSION_PATH.read_text(encoding="utf-8").strip().lstrip("vV") or "0.0.0"
    except OSError:
        return "0.0.0"


APP_VERSION = read_version()
ZIP_PATH = BUILD / ("minitool-usage-dashboard-%s.zip" % APP_VERSION)
AUDIT = HERE / ".skill" / "minitool-zip-builder" / "scripts" / "audit_artifact.py"

VIEWPORT = (
    '<meta name="viewport" content="width=device-width, initial-scale=1.0, '
    'maximum-scale=1.0, user-scalable=no, viewport-fit=cover">'
)

# Chrome 61 不认识 flex gap（84+ 才有），用 margin 兜底；
# 支持 gap 的内核直接跳过这整块，不影响现代浏览器。
GAP_FALLBACK = """
  /* ---- Chrome 61 兜底：flex gap 用 margin 模拟 ---- */
  @supports not (gap: 1px) {
    .brand > * + * { margin-left: 7px; }
    .brand-main > * + * { margin-left: 7px; }
    .top-actions > * + * { margin-left: 6px; }
    .tbtn > * + * { margin-left: 5px; }
    .filterbar > * + * { margin-left: 8px; }
    .pill > * + * { margin-left: 6px; }
    .kpi .label > * + * { margin-left: 5px; }
    .legend > * + * { margin-left: 5px; }
    .seg > * + * { margin-left: 2px; }
    .tbl-hd .left > * + * { margin-left: 10px; }
    .chips > * + * { margin-left: 6px; }
    .model > * + * { margin-left: 6px; }
    .share > * + * { margin-left: 6px; }
    .kv > * + * { margin-left: 14px; }
    .calc .line > * + * { margin-left: 10px; }
    .sk-top > * + * { margin-left: 10px; }
    .fam > * + * { margin-left: 9px; }
    .cmd-row > * + * { margin-left: 10px; }
  }

  /* ---- 容器安全区 ---- */
  body { -webkit-tap-highlight-color: transparent; }
  @supports (padding: env(safe-area-inset-bottom)) {
    .foot { padding-bottom: calc(28px + env(safe-area-inset-bottom)); }
    .drawer-body { padding-bottom: calc(30px + env(safe-area-inset-bottom)); }
    .toast { bottom: calc(28px + env(safe-area-inset-bottom)); }
  }
"""

OFFLINE_REFRESH = """function refresh() {
  toast('离线快照：数据来自打包时的统计，不会自动更新');
}
"""

OFFLINE_HINT = """function updateSyncHint() {
  const el = document.getElementById('syncHint');
  if (!el) return;
  const gen = META && META.generated ? String(META.generated).slice(5, 16) : '';
  el.textContent = gen ? '快照 ' + gen : '离线快照';
}
"""


def must_replace(text, old, new, label, count=1):
    found = text.count(old)
    if found != count:
        raise SystemExit("模板结构已变化：%s 期望 %d 处，实际 %d 处" % (label, count, found))
    return text.replace(old, new)


def add_grid_gap(css):
    """Chrome 61 只有 grid-gap，没有 gap 别名（66+），给 grid 规则补一份。"""

    def fix_rule(match):
        rule = match.group(0)
        if "display: grid" not in rule:
            return rule
        return re.sub(
            r"gap: (\d+)px;",
            lambda m: "gap: %spx; grid-gap: %spx;" % (m.group(1), m.group(1)),
            rule,
            count=1,
        )

    return re.sub(r"\{[^{}]*\}", fix_rule, css)


def sanitize(data, keep_titles=False):
    """打包进可上传产物的数据要脱敏：本机绝对路径、会话标题都不该跟着包走。

    标题换成「会话 #N」，既保留可区分的身份，又不把对话主题带出去；
    本地预览需要真实标题时用 --with-titles。
    """
    stats = {"cwd": 0, "title": 0, "live": 0}
    for sid, meta in data.get("sessions", {}).items():
        if meta.get("cwd"):
            meta["cwd"] = ""
            stats["cwd"] += 1
        if not keep_titles and meta.get("t"):
            meta["t"] = "会话 " + str(sid)[:4]
            stats["title"] += 1

    for row in data.get("rows", []):
        if row.get("live"):
            row["live"] = False
            stats["live"] += 1
    return stats


def build(keep_titles=False):
    tpl = TPL_PATH.read_text(encoding="utf-8")
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))

    head_end = tpl.index("<body>")
    body_start = head_end + len("<body>")
    script_start = tpl.index("<script>")
    script_end = tpl.index("</script>") + len("</script>")

    head = tpl[:head_end]
    body = tpl[body_start:script_start]
    script = tpl[script_start:script_end]

    # --- head：viewport + 样式兜底 ---
    assert '<meta name="viewport"' in head
    head = re.sub(r'<meta name="viewport"[^>]*>', VIEWPORT, head, count=1)

    css_start = head.index("<style>") + len("<style>")
    css_end = head.index("</style>")
    css = head[css_start:css_end]
    css = must_replace(
        css,
        ".drawer-mask { position: fixed; inset: 0;",
        ".drawer-mask { position: fixed; top: 0; right: 0; bottom: 0; left: 0;",
        "drawer-mask inset",
    )
    css = add_grid_gap(css) + GAP_FALLBACK
    head = head[:css_start] + css + head[css_end:]

    # --- script：外置 + 去联网 ---
    js = script[len("<script>") : -len("</script>")]
    js = must_replace(
        js,
        "const DATA = __DATA__;",
        "const DATA = window.__DASHBOARD_DATA__ || {};",
        "数据占位符",
    )

    start = js.index("async function refresh(silent) {")
    end = js.index("(function init() {")
    js = js[:start] + OFFLINE_REFRESH + "\n" + js[end:]

    start = js.index("function updateSyncHint() {")
    end = js.index("async function refresh") if "async function refresh" in js else js.index("function refresh() {")
    js = js[:start] + OFFLINE_HINT + "\n" + js[end:]

    start = js.index("  document.getElementById('syncBtn').addEventListener")
    end = js.index("  setInterval(updateSyncHint, 15000);") + len("  setInterval(updateSyncHint, 15000);")
    js = js[:start] + """  const syncBtn = document.getElementById('syncBtn');
  syncBtn.textContent = '离线快照';
  syncBtn.addEventListener('click', () => refresh(true));
  const autoBtn = document.getElementById('autoBtn');
  autoBtn.textContent = '离线版';
  autoBtn.style.opacity = '.55';
  autoBtn.addEventListener('click', () => toast('离线版不支持自动刷新，换新数据请重新打包'));
  const syncDot = document.getElementById('syncDot');
  if (syncDot) syncDot.style.background = '#d1d5db';
  updateSyncHint();""" + js[end:]

    start = js.index("  const ageMin = gts ?")
    end = js.index("  document.getElementById('drawerClose')")
    js = js[:start] + js[end:]

    js = js.strip() + "\n"

    # --- 数据：脱敏 ---
    stats = sanitize(data, keep_titles=keep_titles)
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("</", "<\\/")
    data_js = "window.__DASHBOARD_DATA__ = %s;\n" % payload

    # --- 落盘 ---
    if DIST.exists():
        shutil.rmtree(DIST)
    (DIST / "assets").mkdir(parents=True)
    (DIST / "assets" / "app.js").write_text(js, encoding="utf-8")
    (DIST / "assets" / "data.js").write_text(data_js, encoding="utf-8")

    page = (
        head
        + "<body>"
        + body.rstrip()
        + '\n<script src="./assets/data.js"></script>\n'
        + '<script src="./assets/app.js"></script>\n'
        + "</body>\n</html>\n"
    )
    page = page.replace("__VERSION__", APP_VERSION)
    (DIST / "index.html").write_text(page, encoding="utf-8")
    return stats


def make_zip():
    BUILD.mkdir(exist_ok=True)
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for path in sorted(DIST.rglob("*")):
            if path.is_file():
                z.write(path, path.relative_to(DIST).as_posix())
    return ZIP_PATH


def check():
    """静态门禁：结构、CSP、语法、体积。返回 (errors, warnings, notes)。"""
    errors, warnings, notes = [], [], []

    index = (DIST / "index.html").read_text(encoding="utf-8")
    app = (DIST / "assets" / "app.js").read_text(encoding="utf-8")
    data_js = (DIST / "assets" / "data.js").read_text(encoding="utf-8")

    if not index.lstrip().startswith("<!DOCTYPE html>"):
        errors.append("index.html 缺少 <!DOCTYPE html>")
    if 'lang="zh-CN"' not in index:
        errors.append("index.html 缺少 lang=zh-CN")
    if "viewport-fit=cover" not in index:
        errors.append("viewport 缺少 viewport-fit=cover")
    if "__VERSION__" in index:
        errors.append("index.html 里 __VERSION__ 没被替换（版本号来源断了）")
    elif APP_VERSION not in index:
        errors.append("index.html 页脚缺少版本号 %s" % APP_VERSION)
    if "<base" in index.lower():
        errors.append("禁止使用 <base>")
    if "<iframe" in index.lower() or "<object" in index.lower():
        errors.append("禁止 iframe / object")

    inline = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", index, flags=re.I)
    if inline:
        errors.append("存在内联 <script>：%d 处" % len(inline))
    if re.search(r"<script[^>]*type=\"module\"", index, flags=re.I):
        errors.append("存在 type=\"module\" 脚本")

    for name, text in (("index.html", index), ("app.js", app)):
        if re.search(r"\son[a-z]+\s*=", text, flags=re.I):
            errors.append("%s 存在行内事件 on*=" % name)
        if "javascript:" in text.lower():
            errors.append("%s 存在 javascript: URI" % name)
        if re.search(r"\b(eval|new Function)\s*\(", text):
            errors.append("%s 使用了 eval / new Function" % name)
        for url in re.findall(r"https?://[^\s\"'<>)]+", text):
            if "www.w3.org" in url:  # 内联 SVG 的 xmlns 声明，不是网络请求
                continue
            errors.append("%s 存在外部链接：%s" % (name, url))
        if re.search(r"\b(fetch|XMLHttpRequest|WebSocket|EventSource)\b", text):
            errors.append("%s 存在联网调用" % name)
        if re.search(r"\bimport\s+[\w{*]|\bexport\s+(default|const|function|{)", text):
            errors.append("%s 存在 ES module 语法" % name)

    for api in ("localStorage", "sessionStorage", "indexedDB", "navigator.geolocation",
                "navigator.clipboard", "serviceWorker", "SharedWorker"):
        if api in app:
            warnings.append("app.js 引用了受限能力：%s" % api)

    try:
        out = subprocess.run(
            ["node", "--check", str(DIST / "assets" / "app.js")],
            capture_output=True, text=True, timeout=60,
        )
        if out.returncode != 0:
            errors.append("node --check 失败：" + (out.stderr or "").strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError):
        warnings.append("未找到 node，跳过 JS 语法校验")

    for name, text in (("index.html", index), ("app.js", app), ("data.js", data_js)):
        size = len(text.encode("utf-8"))
        if size > 2 * 1024 * 1024:
            warnings.append("%s 单文件 %.2f MiB，超过 2 MiB 建议值" % (name, size / 1048576))

    if AUDIT.exists():
        out = subprocess.run([sys.executable, str(AUDIT), str(DIST)], capture_output=True, text=True)
        line = (out.stdout or out.stderr).strip().splitlines()[-1]
        if out.returncode == 0:
            notes.append("审计脚本(dist)：%s" % line)
        else:
            errors.append("审计脚本(dist)：%s" % line)
    else:
        warnings.append("未找到 audit_artifact.py，跳过体积审计")

    return errors, warnings, notes


def check_zip():
    errors, warnings, notes = [], [], []
    with zipfile.ZipFile(ZIP_PATH) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
    if "index.html" not in names:
        errors.append("zip 根目录没有 index.html")
    nested = [n for n in names if n.count("/") == 0 and "." not in n]
    if nested:
        errors.append("zip 内含多余顶层目录：%s" % ", ".join(nested))
    allowed = {".html", ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
               ".woff", ".woff2", ".json"}
    for n in names:
        suffix = Path(n).suffix.lower()
        if suffix not in allowed:
            errors.append("不支持的文件类型：%s" % n)
        if any(part in (".git", "node_modules", "__MACOSX") for part in Path(n).parts):
            errors.append("打包了开发垃圾：%s" % n)
        if suffix == ".map":
            errors.append("打包了 sourcemap：%s" % n)
    size = ZIP_PATH.stat().st_size
    if size > 10 * 1024 * 1024:
        errors.append("zip %.2f MiB 超过 10 MiB 上限" % (size / 1048576))
    elif size > 2 * 1024 * 1024:
        warnings.append("zip %.2f MiB 超过 2 MiB 建议值" % (size / 1048576))
    if AUDIT.exists():
        out = subprocess.run([sys.executable, str(AUDIT), str(ZIP_PATH)], capture_output=True, text=True)
        line = (out.stdout or out.stderr).strip().splitlines()[-1]
        if out.returncode == 0:
            notes.append("审计脚本(zip)：%s" % line)
        else:
            errors.append("审计脚本(zip)：%s" % line)
    return errors, warnings, notes, names


def main():
    ap = argparse.ArgumentParser(description="打包看板为小工具离线 zip")
    ap.add_argument("--check", action="store_true", help="只校验已有的 dist/，不重新构建")
    ap.add_argument("--with-titles", action="store_true", help="保留会话标题（默认脱敏为「会话 #N」）")
    args = ap.parse_args()

    if not args.check:
        stats = build(keep_titles=args.with_titles)
        print("构建 dist/ 完成：隐去路径 %d 条 · 标题脱敏 %d 条 · 清除「进行中」标记 %d 条"
              % (stats["cwd"], stats["title"], stats["live"]))

    errors, warnings, notes = check()
    for n in notes:
        print("  · %s" % n)
    for w in warnings:
        print("WARN:", w)
    for e in errors:
        print("ERROR:", e)
    if errors:
        print("校验未通过：%d 个错误" % len(errors))
        return 1

    zip_path = make_zip()
    z_errors, z_warnings, z_notes, names = check_zip()
    for n in z_notes:
        print("  · %s" % n)
    for w in z_warnings:
        print("WARN:", w)
    for e in z_errors:
        print("ERROR:", e)
    if z_errors:
        print("zip 校验未通过：%d 个错误" % len(z_errors))
        return 1

    print("文件清单：")
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            print("  %-22s %7d B  压缩后 %d B" % (info.filename, info.file_size, info.compress_size))
    print("PASS  v%s 产物：%s（%.1f KiB）" % (APP_VERSION, zip_path, zip_path.stat().st_size / 1024))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
