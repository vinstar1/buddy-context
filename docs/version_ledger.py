#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""版本台账工具：一个仓库里维护「代码版本 / 发布版本 / 业务版本」三本账。

用法（在本项目任意位置）：
    python docs/version_ledger.py init --product "产品名"
    python docs/version_ledger.py new 0.4.0 "这一版的主题"
    python docs/version_ledger.py build
    python docs/version_ledger.py check [--remote] [--strict] [--json]
    python docs/version_ledger.py notes 0.4.0 [--out notes.md]
    python docs/version_ledger.py set-status 0.4.0 demo --date 2026-10-30 --tag v0.4.0

只依赖标准库，Python 3.8+ 可用。台账数据只有 docs/versions.json 一个来源，
看板 docs/version-board.html 是生成物，永远不要手改。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
REQUIRED = ("version", "status", "theme", "business", "changes")
DEFAULT_STATUSES = {"released": "已交付", "demo": "已演示", "iterating": "开发中"}
DEFAULT_GROUPS = "用户端 / 管理端 / 后端 / 部署"

# 发布说明的固定章节：分组名写成这些 key，中英文各自翻译；写别的名字就原样保留。
CANON_SECTIONS = {
    "breaking": {"en": "Breaking Changes", "zh": "不兼容改动"},
    "features": {"en": "Features", "zh": "新功能"},
    "fixes": {"en": "Fixes", "zh": "修复"},
    "perf": {"en": "Performance", "zh": "性能"},
    "changes": {"en": "Changes", "zh": "其他改动"},
    "docs": {"en": "Docs", "zh": "文档"},
}
SECTION_ORDER = ["breaking", "features", "fixes", "perf", "changes", "docs"]

LABELS = {
    "en": {
        "install": "Install / Update",
        "install_cmd": "Install",
        "update_cmd": "Update",
        "deployed": "Deployed at",
        "whats_new": "What's New",
        "business": "Why this release",
        "commits": "Commits",
        "contributors": "Contributors",
        "feedback": "Feedback",
        "note": "Notes",
        "rollback": "Roll back",
        "empty": "_No user-visible changes recorded for this version._",
        "commits_unit": "commits",
        "commit_unit": "commit",
    },
    "zh": {
        "install": "安装 / 升级",
        "install_cmd": "安装",
        "update_cmd": "升级",
        "deployed": "线上地址",
        "whats_new": "本版更新",
        "business": "为什么发这一版",
        "commits": "提交",
        "contributors": "贡献者",
        "feedback": "业务反馈",
        "note": "说明",
        "rollback": "回滚",
        "empty": "_这一版没有记录面向用户的改动。_",
        "commits_unit": "个提交",
        "commit_unit": "个提交",
    },
}
LANGS = ("en", "zh")

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 基础
# --------------------------------------------------------------------------- #

def die(msg: str) -> "None":
    print(f"[x] {msg}", file=sys.stderr)
    sys.exit(1)


def warn(msg: str) -> None:
    print(f"[!] {msg}")


def ok(msg: str) -> None:
    print(f"[+] {msg}")


def info(msg: str) -> None:
    print(f"    {msg}")


def vkey(v: str) -> tuple:
    return tuple(int(x) for x in v.split("."))


def pick(val, lang: str = "zh") -> str:
    """双语字段取值：字符串原样返回，{"en","zh"} 取指定语言，缺则回退另一语言。"""
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        for k in (lang, "zh", "en"):
            got = val.get(k)
            if isinstance(got, str) and got.strip():
                return got
        for k in val:
            if isinstance(val[k], str) and val[k].strip():
                return val[k]
    return ""


def bilingual(val) -> bool:
    return isinstance(val, dict) and any(isinstance(val.get(k), str) for k in LANGS)


def section_name(key: str, lang: str, overrides: dict | None = None) -> str:
    spec = (overrides or {}).get(key) or CANON_SECTIONS.get(key)
    if isinstance(spec, dict):
        return pick(spec, lang) or key
    if isinstance(spec, str):
        return spec
    return key


def ordered_changes(changes: dict) -> list:
    """按固定章节顺序排，自定义分组保持写入顺序排在后面。"""
    keys = [k for k in SECTION_ORDER if k in changes]
    keys += [k for k in changes if k not in SECTION_ORDER]
    return [(k, changes[k] or []) for k in keys if (changes.get(k) or [])]


class Ctx:
    def __init__(self, root: str | None, docs: str | None):
        self.root = Path(root).resolve() if root else find_root()
        if docs:
            d = Path(docs)
            self.docs = d.resolve() if d.is_absolute() else (self.root / d).resolve()
        else:
            self.docs = self.root / "docs"
        self.json_path = self.docs / "versions.json"
        self.tpl_path = self.docs / "board-template.html"
        self.board_path = self.docs / "version-board.html"
        self.doc_path = self.docs / "VERSIONING.md"
        self.installed_script = self.docs / "version_ledger.py"
        self.data: dict = {}

    def rel(self, p: Path) -> str:
        try:
            return p.relative_to(self.root).as_posix()
        except ValueError:
            return p.as_posix()

    def load(self) -> dict:
        if not self.json_path.exists():
            die(
                f"找不到 {self.rel(self.json_path)}。先在本仓库跑一次：\n"
                f'    python <skill>/scripts/version_ledger.py init --root "{self.root}" --product "产品名"'
            )
        try:
            self.data = json.loads(self.json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            die(f"{self.rel(self.json_path)} 格式错误：第 {e.lineno} 行第 {e.colno} 列，{e.msg}")
        if not isinstance(self.data.get("versions"), list):
            die(f"{self.rel(self.json_path)} 缺少 versions 数组")
        return self.data

    def save(self) -> None:
        self.data["updated"] = date.today().isoformat()
        self.json_path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def version_file(self) -> Path:
        return self.root / (self.data.get("version_file") or "VERSION")

    def max_version(self) -> str | None:
        vs = [v["version"] for v in self.data["versions"] if SEMVER.match(v.get("version", ""))]
        return max(vs, key=vkey) if vs else None


def find_root() -> Path:
    cur = Path.cwd()
    for p in [cur, *cur.parents]:
        if (p / ".git").exists():
            return p
    return cur


def git(root: Path, *args: str) -> tuple[int, str]:
    try:
        r = subprocess.run(
            ["git", *args], cwd=str(root), capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=20,
        )
        out = (r.stdout or "").strip()
        if r.returncode != 0 and not out:
            out = (r.stderr or "").strip()
        return r.returncode, out
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def detect_platform(root: Path) -> tuple[str, str]:
    """返回 (平台, 规范化后的仓库地址)。"""
    _, url = git(root, "remote", "get-url", "origin")
    if not url:
        return "other", ""
    low = url.lower()
    if "github.com" in low:
        plat = "github"
    elif "gitlab" in low:
        plat = "gitlab"
    elif "gitea" in low or "forgejo" in low or "codeberg.org" in low:
        plat = "gitea"
    else:
        plat = "other"
    return plat, normalize_url(url)


def normalize_url(url: str) -> str:
    url = url.strip()
    m = re.match(r"^[\w.-]+@([\w.-]+):(.+?)(?:\.git)?$", url)
    if m:
        return f"https://{m.group(1)}/{m.group(2)}"
    return re.sub(r"\.git$", "", url)


def detect_version(root: Path) -> tuple[str, str] | None:
    """从常见的版本声明处读出当前版本，返回 (版本, 来源文件)。"""
    pj = root / "package.json"
    if pj.exists():
        try:
            v = str(json.loads(pj.read_text(encoding="utf-8")).get("version") or "")
            if SEMVER.match(v):
                return v, "package.json"
        except (json.JSONDecodeError, OSError):
            pass
    for fname, pattern in (
        ("pyproject.toml", r'(?m)^\s*version\s*=\s*"(\d+\.\d+\.\d+)"'),
        ("Cargo.toml", r'(?m)^\s*version\s*=\s*"(\d+\.\d+\.\d+)"'),
        ("setup.py", r'version\s*=\s*["\'](\d+\.\d+\.\d+)["\']'),
    ):
        f = root / fname
        if f.exists():
            m = re.search(pattern, f.read_text(encoding="utf-8", errors="replace"))
            if m:
                return m.group(1), fname
    return None


# --------------------------------------------------------------------------- #
# 校验 / 渲染
# --------------------------------------------------------------------------- #

def validate(data: dict) -> tuple[list, list]:
    """返回 (errors, warnings)。不写文件。"""
    errors, warnings = [], []
    statuses = data.get("statuses") or DEFAULT_STATUSES
    if not isinstance(data.get("statuses"), dict):
        errors.append("statuses 必须是对象，例如 {\"released\": \"已交付\"}")

    versions = data.get("versions") or []
    if not versions:
        errors.append("versions 为空，至少需要一个版本")

    seen = set()
    for i, v in enumerate(versions, 1):
        name = v.get("version", f"第 {i} 条")
        miss = [k for k in REQUIRED if k not in v]
        if miss:
            errors.append(f"版本 {name} 缺字段：{', '.join(miss)}")
        if not SEMVER.match(str(v.get("version", ""))):
            errors.append(f"版本号 {name} 不符合 主.次.修订（如 0.4.0）")
        if name in seen:
            errors.append(f"版本号重复：{name}")
        seen.add(name)
        if v.get("status") not in statuses:
            errors.append(
                f"版本 {name} 的 status「{v.get('status')}」不在 statuses 里"
                f"（可选：{', '.join(statuses)}）"
            )
        ch = v.get("changes")
        if not isinstance(ch, dict):
            errors.append(f"版本 {name} 的 changes 必须是对象（按「{DEFAULT_GROUPS}」或 features/fixes/changes 分组）")
        else:
            for g, items in ch.items():
                if not isinstance(items, list):
                    errors.append(f"版本 {name} 的 changes.{g} 必须是数组")
                    continue
                for i, x in enumerate(items, 1):
                    if isinstance(x, str):
                        continue
                    if bilingual(x):
                        continue
                    errors.append(
                        f"版本 {name} 的 changes.{g}[{i}] 只能是字符串，"
                        f'或 {{"en": "...", "zh": "..."}} 双语对象'
                    )
        biz = v.get("business")
        if not isinstance(biz, str) and not bilingual(biz):
            errors.append(f"版本 {name} 的 business 必须是字符串或双语对象")
        if v.get("pending"):
            warnings.append(f"版本 {name} 是骨架，business / changes 还没补完")
        if v.get("status") in ("released", "demo"):
            if not v.get("date"):
                warnings.append(f"版本 {name} 已交付/已演示，但没有 date")
            if not v.get("tag"):
                warnings.append(f"版本 {name} 已交付/已演示，但没有记 tag（代码版本找不回来）")
    return errors, warnings


def payload(data: dict) -> str:
    """看板数据：双语条目按看板语言拍平成字符串，分组名换成看板语言的叫法。"""
    lang = data.get("board_lang") or "zh"
    out = json.loads(json.dumps(data, ensure_ascii=False))
    for v in out.get("versions", []):
        v["business"] = pick(v.get("business"), lang)
        ch = {}
        for g, items in ordered_changes(v.get("changes") or {}):
            name = section_name(g, lang, data.get("sections"))
            ch[name] = [x for x in (pick(i, lang) for i in items) if x]
        v["changes"] = ch
    return json.dumps(out, ensure_ascii=False, indent=2).replace("</", "<\\/")


def render(ctx: Ctx) -> str:
    data = ctx.load()
    errors, _ = validate(data)
    if errors:
        die("台账有问题，先修再生成看板：\n" + "\n".join(f"  - {e}" for e in errors))
    if not ctx.tpl_path.exists():
        die(f"找不到看板模板 {ctx.rel(ctx.tpl_path)}（init 时应该一起复制过来）")
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    html = (
        ctx.tpl_path.read_text(encoding="utf-8")
        .replace("__DATA__", payload(data))
        .replace("__GENERATED__", stamp)
    )
    ctx.board_path.write_text(html, encoding="utf-8")
    return stamp


def read_board_payload(path: Path) -> str | None:
    if not path.exists():
        return None
    txt = path.read_text(encoding="utf-8")
    m = re.search(r'<script type="application/json" id="data">(.*?)</script>', txt, re.S)
    return m.group(1).strip() if m else None


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #

RELEASE_STEPS = {
    "github": """```bash
NEW=0.4.0    # 换成这一版要发的号

# 1. 开一个新版本（改 VERSION + 往台账插一条骨架 + 重生成看板）
python docs/version_ledger.py new $NEW "这一版的主题"

# 2. 写业务账：把业务原话和拆解填进 docs/versions.json
#    business  = 业务为什么要改（业务听得懂的话）
#    changes   = 按分组写，一条一句
python docs/version_ledger.py build          # 重生成看板，确认没写错

# 3. 提交 + 打标签（标签只在两个时刻打：给业务演示那天、上线那天）
git add -A
git commit -m "feat: v$NEW 这一版的主题"
git tag -a v$NEW -m "v$NEW 这一版的主题"
git push origin HEAD --tags

# 4. 在 GitHub 上留一份可检索的发布记录（可选但推荐）
#    --lang both 是默认值：英文块在前、中文块在后，一次粘进 Release 正文
python docs/version_ledger.py notes $NEW --out notes.md
gh release create v$NEW --title "v$NEW" --notes-file notes.md

# 5. 产物 / 数据快照（有数据库或镜像时）
#    镜像带版本号，不要只留 latest；数据库结构变了就存一份
mkdir -p _releases/v$NEW
docker build -t <应用>:$NEW . && docker tag <应用>:$NEW <应用>:latest
cp <数据文件> _releases/v$NEW/

# 6. 部署并确认版本
#    服务器 .env 写 APP_VERSION=$NEW（或等价的版本变量），再重启
curl -s <健康检查地址>     # 看到 version = $NEW 才算完
```""",
    "gitlab": """```bash
NEW=0.4.0    # 换成这一版要发的号

# 1. 开一个新版本（改 VERSION + 往台账插一条骨架 + 重生成看板）
python docs/version_ledger.py new $NEW "这一版的主题"

# 2. 写业务账：把业务原话和拆解填进 docs/versions.json
#    business  = 业务为什么要改（业务听得懂的话）
#    changes   = 按分组写，一条一句
python docs/version_ledger.py build          # 重生成看板，确认没写错

# 3. 提交 + 打标签（标签只在两个时刻打：给业务演示那天、上线那天）
git add -A
git commit -m "feat: v$NEW 这一版的主题"
git tag -a v$NEW -m "v$NEW 这一版的主题"
git push origin HEAD --tags

# 4. 在 GitLab 上留一份可检索的发布记录（可选但推荐）
#    --lang both 是默认值：英文块在前、中文块在后，一次粘进 Release 正文
python docs/version_ledger.py notes $NEW --out notes.md
glab release create v$NEW --name "v$NEW" --notes-file notes.md

# 5. 产物 / 数据快照（有数据库或镜像时）
#    镜像带版本号，不要只留 latest；数据库结构变了就存一份
mkdir -p _releases/v$NEW
docker build -t <应用>:$NEW . && docker tag <应用>:$NEW <应用>:latest
cp <数据文件> _releases/v$NEW/

# 6. 部署并确认版本
#    服务器 .env 写 APP_VERSION=$NEW（或等价的版本变量），再重启
curl -s <健康检查地址>     # 看到 version = $NEW 才算完
```""",
}

RELEASE_STEPS["gitea"] = RELEASE_STEPS["github"].replace("GitHub", "Gitea").replace(
    "gh release create", "tea release create"
)
RELEASE_STEPS["other"] = RELEASE_STEPS["github"].replace(
    "在 GitHub 上留一份可检索的发布记录（可选但推荐）", "在远端仓库留一份可检索的发布记录（可选）"
).replace("gh release create v$NEW --title \"v$NEW\" --notes-file notes.md", "# 手动在平台网页上按 notes.md 建 Release")

PLATFORM_NAMES = {"github": "GitHub", "gitlab": "GitLab", "gitea": "Gitea", "other": "通用 Git"}


def cmd_init(args: argparse.Namespace) -> None:
    ctx = Ctx(args.root, args.docs)
    src_dir = Path(__file__).resolve().parent
    asset_tpl = src_dir.parent / "assets" / "board-template.html"
    doc_tpl = src_dir.parent / "references" / "versioning-doc-template.md"

    if ctx.json_path.exists() and not args.force:
        die(f"{ctx.rel(ctx.json_path)} 已存在。要重建加 --force（会覆盖台账）")
    if not asset_tpl.exists():
        die(f"技能资源缺失：{asset_tpl}")

    product = args.product or ctx.root.name
    detected_platform, detected_url = detect_platform(ctx.root)
    platform = args.platform or detected_platform
    repo_url = args.repo_url or detected_url
    statuses = dict(DEFAULT_STATUSES)

    ctx.docs.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(asset_tpl, ctx.tpl_path)
    if not ctx.installed_script.exists() or args.force:
        shutil.copyfile(Path(__file__).resolve(), ctx.installed_script)

    version_file = ctx.root / args.version_file
    if version_file.exists():
        ver = version_file.read_text(encoding="utf-8").strip().lstrip("vV") or "0.1.0"
        info(f"保留已有 {args.version_file}：{ver}")
    elif args.version:
        ver = args.version.strip().lstrip("vV")
        if not SEMVER.match(ver):
            die(f"--version 要写成 主.次.修订（收到的是 {ver}）")
        version_file.parent.mkdir(parents=True, exist_ok=True)
        version_file.write_text(ver + "\n", encoding="utf-8")
        ok(f"新建 {args.version_file}（{ver}）")
    else:
        found = detect_version(ctx.root)
        ver = found[0] if found else "0.1.0"
        version_file.parent.mkdir(parents=True, exist_ok=True)
        version_file.write_text(ver + "\n", encoding="utf-8")
        if found:
            ok(f"新建 {args.version_file}（{ver}，沿用 {found[1]} 里的版本）")
        else:
            ok(f"新建 {args.version_file}（{ver}）")
        if found:
            warn(f"{found[1]} 里也写着版本号，建议改成读 {args.version_file}，版本号只能有一个来源")

    other = detect_version(ctx.root)
    if other and other[0] != ver and other[1] != args.version_file.replace("\\", "/"):
        warn(f"{other[1]} 里是 {other[0]}，{args.version_file} 是 {ver}：先把这两处统一，否则台账对不上线上")

    data = {
        "product": product,
        "subtitle": args.subtitle or "每一版为什么改、改了什么、什么时候上线",
        "platform": platform,
        "repo_url": repo_url,
        "version_file": args.version_file.replace("\\", "/"),
        "artifact": args.artifact or "",
        "health_url": args.health_url or "",
        "lang": args.lang,
        "board_lang": "zh",
        "statuses": statuses,
        "updated": date.today().isoformat(),
        "versions": [{
            "version": ver,
            "status": "iterating",
            "date": "",
            "theme": "首个可运行版本",
            "business": "待补充：业务为什么要做这一版（用业务听得懂的话）。",
            "feedback": [],
            "changes": {},
            "pending": True,
            "next": (
                f"把这一版的改动按「{DEFAULT_GROUPS}」或 features/fixes/changes 写进 changes，"
                f"然后删掉 pending 和 next 两个字段。"
            ),
        }],
    }
    if args.install:
        data["install"] = list(args.install)
    if args.update:
        data["update"] = list(args.update)
    order = ["product", "subtitle", "platform", "repo_url", "version_file", "artifact",
             "health_url", "lang", "board_lang", "install", "update", "sections",
             "statuses", "updated", "versions"]
    data = {k: data[k] for k in order if k in data}
    ctx.data = data
    ctx.save()
    ok(f"写入 {ctx.rel(ctx.json_path)}")

    if doc_tpl.exists():
        docs_rel = ctx.rel(ctx.docs) or "."
        steps = (
            RELEASE_STEPS[platform]
            .replace("docs/version_ledger.py", f"{docs_rel}/version_ledger.py")
            .replace("docs/versions.json", f"{docs_rel}/versions.json")
        )
        doc = doc_tpl.read_text(encoding="utf-8")
        for k, v in {
            "{{PRODUCT}}": product,
            "{{VERSION_FILE}}": args.version_file.replace("\\", "/"),
            "{{DOCS_DIR}}": docs_rel,
            "{{PLATFORM}}": PLATFORM_NAMES[platform],
            "{{REPO_URL}}": repo_url or "（未配置远端）",
            "{{RELEASE_STEPS}}": steps,
            "{{DATE}}": date.today().isoformat(),
        }.items():
            doc = doc.replace(k, v)
        ctx.doc_path.write_text(doc, encoding="utf-8")
        ok(f"写入 {ctx.rel(ctx.doc_path)}")

    stamp = render(ctx)
    ok(f"生成 {ctx.rel(ctx.board_path)}（{stamp}）")

    print()
    script_rel = ctx.rel(ctx.installed_script)
    print("还需要手动做两件事：")
    print(f"  1. 补 {ctx.rel(ctx.json_path)} 里的 business 和 changes，删掉 pending / next")
    print("  2. 确认 .gitignore 里没有把台账和看板忽略掉，并加上发版快照目录：")
    print("        _releases/")
    print()
    print("之后每次发版：")
    print(f'  python {script_rel} new <版本号> "<主题>"')
    print(f"  python {script_rel} check --remote")


# --------------------------------------------------------------------------- #
# new / build / check / notes / set-status
# --------------------------------------------------------------------------- #

def cmd_new(args: argparse.Namespace) -> None:
    ctx = Ctx(args.root, args.docs)
    ctx.load()
    version = args.version.strip().lstrip("vV")
    theme = args.theme.strip()
    if not SEMVER.match(version):
        die(f"版本号要写成 主.次.修订，比如 0.4.0（收到的是 {version}）")
    if any(v.get("version") == version for v in ctx.data["versions"]):
        die(f"{ctx.rel(ctx.json_path)} 里已经有 {version} 了")

    cur = ctx.version_file()
    old = cur.read_text(encoding="utf-8").strip() if cur.exists() else "（无）"
    top = ctx.max_version()
    if top and vkey(version) < vkey(top):
        die(f"{version} 比台账里最高的 {top} 还小。要补历史版本用 --force，要改历史用 set-status")
    if top and vkey(version) == vkey(top) and not args.force:
        die(f"台账里最高的就是 {top}，没有升版本。确认要重开同一版加 --force")

    info(f"{ctx.rel(cur)}: {old}  →  {version}")

    ctx.data["versions"].insert(0, {
        "version": version,
        "status": "iterating",
        "date": "",
        "theme": theme,
        "business": "待补充：业务为什么要改这一版（用业务听得懂的话）。",
        "feedback": [],
        "changes": {},
        "pending": True,
        "next": f"把这一版的改动按「{DEFAULT_GROUPS}」写进 changes，然后删掉 pending 和 next 两个字段。",
    })
    ctx.save()
    cur.parent.mkdir(parents=True, exist_ok=True)
    cur.write_text(version + "\n", encoding="utf-8")
    ok(f"{ctx.rel(ctx.json_path)} 已插入 {version} 骨架，{ctx.rel(cur)} 已更新")

    if args.build:
        stamp = render(ctx)
        ok(f"看板已重生成（{stamp}）")

    print()
    print("接下来：")
    print(f"  1. 补 {ctx.rel(ctx.json_path)} 里的 business / changes，删掉 pending 和 next")
    print(f"  2. python {ctx.rel(ctx.installed_script)} build")
    print(f'  3. git commit -m "feat: v{version} {theme}" && git tag -a v{version} -m "v{version} {theme}"')
    print("  4. git push origin HEAD --tags  然后在平台上建 Release（notes 命令可生成说明）")


def cmd_build(args: argparse.Namespace) -> None:
    ctx = Ctx(args.root, args.docs)
    stamp = render(ctx)
    ctx.load()
    ok(f"已生成 {ctx.rel(ctx.board_path)}（{len(ctx.data['versions'])} 个版本，{stamp}）")
    for v in sorted(ctx.data["versions"], key=lambda x: vkey(x["version"]), reverse=True):
        n = sum(len(x) for x in (v.get("changes") or {}).values())
        print(f"    v{v['version']:<9} {v.get('status', ''):<10} "
              f"{v.get('date') or '未发布':<12} {v.get('theme', '')}  ({n} 条改动)")


def cmd_check(args: argparse.Namespace) -> None:
    ctx = Ctx(args.root, args.docs)
    ctx.load()
    errors, warnings = validate(ctx.data)

    # 版本号唯一来源是否跟台账一致
    cur = ctx.version_file()
    top = ctx.max_version()
    if not cur.exists():
        hint = "" if ctx.data.get("version_file") else (
            f'；若版本号在子目录里，在 {ctx.rel(ctx.json_path)} 顶层加 '
            f'"version_file": "<相对仓库根的路径>"'
        )
        errors.append(f"版本号文件不存在：{ctx.rel(cur)}{hint}")
    elif top:
        on_disk = cur.read_text(encoding="utf-8").strip().lstrip("vV")
        if on_disk != top:
            warnings.append(
                f"{ctx.rel(cur)} 是 {on_disk}，台账最高版本是 {top}；"
                f"发版前两者必须一致（版本号只写一个地方）"
            )

    # 看板是否过期
    board_payload = read_board_payload(ctx.board_path)
    if board_payload is None:
        warnings.append(f"{ctx.rel(ctx.board_path)} 不存在或读不出数据，跑一次 build")
    elif board_payload != payload(ctx.data).strip():
        warnings.append(f"{ctx.rel(ctx.board_path)} 与台账不一致（过期），跑一次 build")

    # 标签
    _, tags = git(ctx.root, "tag", "-l")
    local_tags = set(tags.splitlines()) if tags else set()
    for v in ctx.data["versions"]:
        tag = v.get("tag")
        if tag and tag not in local_tags:
            warnings.append(f"版本 {v['version']} 记了 tag {tag}，但本地没有这个标签")
        if not tag and v.get("status") in ("released", "demo"):
            warnings.append(f"版本 {v['version']} 没有 tag，代码版本无法追溯")
    if args.remote:
        rc, out = git(ctx.root, "ls-remote", "--tags", "origin")
        if rc != 0:
            reason = (out.splitlines() or ["未知错误"])[0][:120]
            warnings.append(f"读不到远端标签：{reason}")
        else:
            remote_tags = {
                line.split("refs/tags/")[-1].replace("^{}", "")
                for line in out.splitlines() if "refs/tags/" in line
            }
            for v in ctx.data["versions"]:
                tag = v.get("tag")
                if tag and tag not in remote_tags:
                    warnings.append(f"tag {tag} 只在本地，远端没有（git push origin {tag}）")

    # 未提交
    rc, dirty = git(ctx.root, "status", "--porcelain", "--", ctx.rel(ctx.docs), str(ctx.version_file()))
    if rc == 0 and dirty:
        warnings.append(f"台账 / 版本号有未提交改动：\n" + "\n".join(f"      {l}" for l in dirty.splitlines()))

    if args.json:
        print(json.dumps(
            {"ok": not errors, "errors": errors, "warnings": warnings,
             "version": top, "versions": len(ctx.data["versions"])},
            ensure_ascii=False, indent=2,
        ))
    else:
        for e in errors:
            print(f"[x] {e}")
        for w in warnings:
            print(f"[!] {w}")
        if errors:
            print(f"\n失败：{len(errors)} 个错误，{len(warnings)} 个提醒")
        elif warnings:
            print(f"\n通过，但有 {len(warnings)} 个提醒")
        else:
            print(f"[+] 三本账一致（版本 {top}，共 {len(ctx.data['versions'])} 条记录）")

    if errors or (args.strict and warnings):
        sys.exit(1)


def expand(s: str, v: dict, ctx: "Ctx") -> str:
    return (
        str(s)
        .replace("{version}", v["version"])
        .replace("{tag}", v.get("tag") or f"v{v['version']}")
        .replace("{product}", ctx.data.get("product") or "")
    )


def prev_of(ctx: "Ctx", v: dict) -> tuple:
    """按语义版本排在当前版前一位的那条记录，返回 (tag, version)。"""
    vers = sorted(
        [x for x in ctx.data["versions"] if SEMVER.match(str(x.get("version", "")))],
        key=lambda x: vkey(x["version"]),
    )
    idx = next((i for i, x in enumerate(vers) if x.get("version") == v.get("version")), None)
    if not idx:
        return "", ""
    prev = vers[idx - 1]
    return (prev.get("tag") or f"v{prev['version']}"), prev["version"]


def gh_handle(email: str) -> str:
    m = re.match(r"^(?:\d+\+)?([A-Za-z0-9][A-Za-z0-9-]*)@users\.noreply\.github\.com$", email or "")
    return m.group(1) if m else ""


def git_history(ctx: "Ctx", tag: str, prev_tag: str) -> tuple:
    """两个标签之间的提交和作者。标签不存在时静默返回空。"""
    if not tag:
        return [], []
    rng = f"{prev_tag}..{tag}" if prev_tag else tag
    rc, out = git(ctx.root, "log", "--no-merges", "--pretty=format:%h%x09%s%x09%an%x09%ae", rng)
    if rc != 0 or not out:
        return [], []
    commits, people, seen = [], [], set()
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or not parts[0]:
            continue
        commits.append((parts[0], parts[1]))
        author = parts[2] if len(parts) > 2 else ""
        handle = gh_handle(parts[3] if len(parts) > 3 else "")
        name = handle or author
        if name and name not in seen:
            seen.add(name)
            people.append((name, bool(handle)))
    return commits, people


def link_refs(text: str, item, repo: str) -> str:
    """按 dsh 的排法挂引用：issue 紧跟加粗标题，commit 落在句末。"""
    if not isinstance(item, dict):
        return text
    issues, commits = [], []
    for n in item.get("issues") or []:
        s = str(n).strip()
        num = s.lstrip("#")
        issues.append(f"[{s}]({repo}/issues/{num})" if repo and num.isdigit() else s)
    for c in item.get("commits") or []:
        c = str(c).strip()
        commits.append(f"[{c[:7]}]({repo}/commit/{c})" if repo else c[:7])
    if not issues and not commits:
        return text

    head_ref = f" ({', '.join(issues)})" if issues else ""
    tail_ref = f" ({', '.join(commits)})" if commits else ""

    m = re.match(r"^(\*\*.+?\*\*)(\s*)(——|—|-)(\s*)(.+)$", text, re.S)
    if m:
        return f"{m.group(1)}{head_ref} {m.group(3)}{m.group(4)}{m.group(5).rstrip()}{tail_ref}"
    if head_ref:
        return f"{text.rstrip()}{tail_ref or head_ref}"
    return f"{text.rstrip()}{tail_ref}"


def render_block(ctx: "Ctx", v: dict, lang: str, hist: dict, prev_tag: str, tag: str) -> list:
    L = LABELS[lang]
    repo = (ctx.data.get("repo_url") or "").rstrip("/")
    host = (re.match(r"https?://([^/]+)/", repo) or [None, ""])[1]
    out = []

    install = [expand(x, v, ctx) for x in (ctx.data.get("install") or []) if str(x).strip()]
    update = [expand(x, v, ctx) for x in (ctx.data.get("update") or []) if str(x).strip()]
    health = ctx.data.get("health_url") or ""
    if install or update:
        out += [f"## {L['install']}", ""]
        if install:
            out += [f"**{L['install_cmd']}**", "", "```bash", *install, "```", ""]
        if update:
            out += [f"**{L['update_cmd']}**", "", "```bash", *update, "```", ""]
    elif health:
        out += [f"## {L['install']}", "", f"{L['deployed']} {health}", ""]

    out += [f"## {L['whats_new']}", ""]
    groups = ordered_changes(v.get("changes") or {})
    if not groups:
        out += [L["empty"], ""]
    for key, items in groups:
        rows = []
        for it in items:
            text = pick(it, lang)
            if not text:
                continue
            rows.append("- " + link_refs(text, it, repo))
        if not rows:
            continue
        out += [f"### {section_name(key, lang, ctx.data.get('sections'))}", "", *rows, ""]

    biz = pick(v.get("business"), lang)
    if biz and not biz.startswith("待补充"):
        out += [f"## {L['business']}", "", biz, ""]

    fb = v.get("feedback") or []
    if fb:
        out += [f"## {L['feedback']}", ""] + [f"- {pick(x, lang) or x}" for x in fb] + [""]

    if v.get("note"):
        out += [f"## {L['note']}", "", pick(v["note"], lang) or str(v["note"]), ""]

    if hist.get("commits"):
        out += [f"## {L['commits']}", ""]
        for h, subj in hist["commits"]:
            out.append(f"- {subj}" + (f" ([{h}]({repo}/commit/{h}))" if repo else f" ({h})"))
        out.append("")

    if hist.get("people"):
        out += [f"## {L['contributors']}", ""]
        for name, is_handle in hist["people"]:
            if is_handle and host:
                out.append(f"- [@{name}](https://{host}/{name})")
            else:
                out.append(f"- {name}")
        out.append("")

    n = len(hist.get("commits") or [])
    if prev_tag and tag and repo and hist.get("ok"):
        foot = f"[{prev_tag}...{tag}]({repo}/compare/{prev_tag}...{tag})"
        if n:
            foot += f" · {n} {L['commit_unit'] if n == 1 else L['commits_unit']}"
        out += [foot, ""]

    return out


def cmd_notes(args: argparse.Namespace) -> None:
    ctx = Ctx(args.root, args.docs)
    ctx.load()
    version = args.version.strip().lstrip("vV")
    v = next((x for x in ctx.data["versions"] if x.get("version") == version), None)
    if not v:
        die(f"台账里没有 {version}")

    want = args.lang or ctx.data.get("lang") or "both"
    if want not in ("en", "zh", "both"):
        die(f"--lang 只能是 en / zh / both（收到 {want}）")
    langs = ["en", "zh"] if want == "both" else [want]

    tag = v.get("tag") or f"v{version}"
    prev_tag, _ = prev_of(ctx, v)

    commits, people = [], []
    if not args.no_git:
        commits, people = git_history(ctx, tag, prev_tag)
    hist = {"commits": commits, "people": people, "ok": bool(commits) or args.no_git}

    blocks = ["\n".join(render_block(ctx, v, lg, hist, prev_tag, tag)).strip() for lg in langs]
    text = "\n\n---\n\n".join(b for b in blocks if b).strip() + "\n"

    if args.title:
        head = f"# v{version}" + (f" — {pick(v.get('theme'), 'en')}" if v.get("theme") else "")
        text = head + "\n\n" + text

    if args.out:
        out = Path(args.out)
        if not out.is_absolute():
            out = ctx.root / out
        out.write_text(text, encoding="utf-8")
        detail = f"{len(commits)} 个提交" if commits else "无提交信息（标签可能还没打）"
        ok(f"发布说明写入 {out}（{'+'.join(langs)}，{detail}）")
    else:
        print(text)


def cmd_set_status(args: argparse.Namespace) -> None:
    ctx = Ctx(args.root, args.docs)
    ctx.load()
    version = args.version.strip().lstrip("vV")
    v = next((x for x in ctx.data["versions"] if x.get("version") == version), None)
    if not v:
        die(f"台账里没有 {version}")
    statuses = ctx.data.get("statuses") or DEFAULT_STATUSES
    if args.status not in statuses:
        die(f"status 只能是：{', '.join(statuses)}")

    v["status"] = args.status
    if args.date is not None:
        v["date"] = args.date
    elif args.status in ("released", "demo") and not v.get("date"):
        v["date"] = date.today().isoformat()
    if args.tag is not None:
        v["tag"] = args.tag
    elif args.status in ("released", "demo") and not v.get("tag"):
        v["tag"] = f"v{version}"
    ctx.save()
    stamp = render(ctx)
    ok(f"{version} → {statuses[args.status]}（{v.get('date') or '无日期'}，tag {v.get('tag') or '无'}），看板已重生成 {stamp}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main() -> None:
    # --root / --docs 既能写在子命令前，也能写在子命令后。
    # 子命令里的同名参数用 SUPPRESS 做默认值，避免把主命令已解析的值覆盖成 None。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=argparse.SUPPRESS,
                        help="仓库根目录（默认从当前目录往上找 .git）")
    common.add_argument("--docs", default=argparse.SUPPRESS,
                        help="台账目录（默认 <root>/docs）")

    p = argparse.ArgumentParser(prog="version_ledger", description="版本台账：代码 / 发布 / 业务三本账")
    p.add_argument("--root", default=None, help="仓库根目录（默认从当前目录往上找 .git）")
    p.add_argument("--docs", default=None, help="台账目录（默认 <root>/docs）")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init", parents=[common], help="在仓库里初始化一套版本台账")
    pi.add_argument("--product", help="产品名（默认取目录名）")
    pi.add_argument("--subtitle", help="看板副标题")
    pi.add_argument("--version-file", default="VERSION", help="版本号文件，相对仓库根（默认 VERSION）")
    pi.add_argument("--version", help="初始版本号（默认沿用 package.json / pyproject.toml 里的，读不到用 0.1.0）")
    pi.add_argument("--platform", choices=["github", "gitlab", "gitea", "other"], help="默认按 origin 自动识别")
    pi.add_argument("--repo-url", help="仓库地址，默认取 origin")
    pi.add_argument("--artifact", help="产物名模板，如 app:{version}（可留空）")
    pi.add_argument("--health-url", help="部署后的健康检查地址（可留空）")
    pi.add_argument("--lang", choices=["en", "zh", "both"], default="both",
                    help="发布说明默认语言，both = 先英后中（默认 both）")
    pi.add_argument("--install", action="append", metavar="CMD",
                    help="安装命令，可重复；支持 {product} {version}")
    pi.add_argument("--update", action="append", metavar="CMD",
                    help="升级命令，可重复；支持 {product} {version}")
    pi.add_argument("--force", action="store_true", help="覆盖已有台账")
    pi.set_defaults(func=cmd_init)

    pn = sub.add_parser("new", parents=[common], help="开一个新版本")
    pn.add_argument("version")
    pn.add_argument("theme")
    pn.add_argument("--force", action="store_true", help="允许补历史版本或重开同一版")
    pn.add_argument("--no-build", dest="build", action="store_false", help="不重生成看板")
    pn.set_defaults(func=cmd_new, build=True)

    pb = sub.add_parser("build", parents=[common], help="versions.json → version-board.html")
    pb.set_defaults(func=cmd_build)

    pc = sub.add_parser("check", parents=[common], help="检查三本账是否一致")
    pc.add_argument("--remote", action="store_true", help="顺带核对远端标签")
    pc.add_argument("--strict", action="store_true", help="有提醒也算失败")
    pc.add_argument("--json", action="store_true", help="输出 JSON")
    pc.set_defaults(func=cmd_check)

    pnt = sub.add_parser("notes", parents=[common], help="按台账生成发布说明（Release / changelog 用）")
    pnt.add_argument("version")
    pnt.add_argument("--out", help="写到文件，默认打印")
    pnt.add_argument("--lang", choices=["en", "zh", "both"],
                     help="输出语言，默认读 versions.json 的 lang，没有则 both（先英后中）")
    pnt.add_argument("--no-git", action="store_true",
                     help="不读 git 日志（默认会补 Commits / Contributors / 对比链接）")
    pnt.add_argument("--title", action="store_true", help="在开头加一级标题「# v<版本> — <主题>」")
    pnt.set_defaults(func=cmd_notes)

    ps = sub.add_parser("set-status", parents=[common], help="改版本状态 / 日期 / tag")
    ps.add_argument("version")
    ps.add_argument("status")
    ps.add_argument("--date", help="YYYY-MM-DD；省略时已交付/已演示自动填今天")
    ps.add_argument("--tag", help="默认 v<版本号>")
    ps.set_defaults(func=cmd_set_status)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
