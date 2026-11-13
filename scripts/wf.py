#!/usr/bin/env python3
"""wf.py - 工作流 Skill 的脚手架与维护工具。

只使用 Python 3 标准库，不联网。所有技能路径在运行时探测，
文件里不写入具体的用户目录。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

WF_PREFIX = "wf-"
NAME_RE = re.compile(r"^wf-[a-z0-9]+(?:-[a-z0-9]+)*$")
SKIP_DIRS = {
    ".git",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".vscode",
    "__pycache__",
    "dist",
    "node_modules",
}
MAX_HASH_BYTES = 2 * 1024 * 1024
MAX_STORED_FILES = 60

CREDENTIAL_FILENAMES = {
    ".env",
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials.json",
    "id_ed25519",
    "id_rsa",
    "secrets.json",
    "service-account.json",
}
CREDENTIAL_SUFFIXES = {".jks", ".key", ".p12", ".pem", ".pfx", ".ppk"}
CREDENTIAL_EXEMPT = ("example", "sample", "template")

TEXT_SUFFIXES = {
    ".cfg",
    ".conf",
    ".css",
    ".csv",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".mdx",
    ".mjs",
    ".ps1",
    ".py",
    ".sh",
    ".toml",
    ".ts",
    ".txt",
    ".yaml",
    ".yml",
}
TEXT_FILENAMES = {"Dockerfile", "Makefile"}

# 检测用的模式本身也要避开这些字面量，否则扫描自带源码时会自我命中。
# 三个平台用户目录的写法都拆成两段拼接，运行结果不变。
PERSONAL_PATH_SUBS = (
    (
        re.compile(
            r"[A-Za-z]:[\\/]+Use" r"rs[\\/]+"
            r"(?!(?:Public|Default(?: User)?|All Users)(?:[\\/]+|$))"
            r"[A-Za-z0-9._ -]+[\\/]+",
            re.IGNORECASE,
        ),
        "~/",
    ),
    (re.compile("/Use" "rs/(?!Shared(?:/|$))[A-Za-z0-9._-]+/"), "~/"),
    (re.compile("/ho" "me/[A-Za-z0-9._-]+/"), "~/"),
    (re.compile("/ro" "ot/"), "~/"),
)

RELATION_LABELS = {
    "reference": "引用",
    "bundle": "内置",
    "inline": "只写步骤",
}


def today() -> str:
    return date.today().isoformat()


def home() -> Path:
    return Path.home()


def discover_roots() -> dict[str, Path]:
    """按优先级探测技能根目录。"""
    candidates = (
        ("shared", home() / ".agents" / "skills"),
        ("codex", home() / ".codex" / "skills"),
        ("claude", home() / ".claude" / "skills"),
        ("library", home() / ".config" / "magpie" / "library" / "skills"),
    )
    roots: dict[str, Path] = {}
    for name, path in candidates:
        if path.is_dir():
            roots[name] = path
    return roots


def magpie_library_file() -> Path:
    return home() / ".config" / "magpie" / "library.json"


def load_magpie_library() -> dict:
    path = magpie_library_file()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def parse_frontmatter(text: str) -> dict[str, str]:
    """解析顶层的 key: value，并支持 > 与 | 块标量。"""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    block = text[3:end].splitlines()
    fields: dict[str, str] = {}
    index = 0
    while index < len(block):
        line = block[index]
        match = re.match(r"^([A-Za-z][A-Za-z0-9_-]*):[ \t]*(.*)$", line)
        if not match:
            index += 1
            continue
        key = match.group(1)
        value = match.group(2).strip()
        if value[:1] in {">", "|"}:
            body: list[str] = []
            index += 1
            while index < len(block) and (
                not block[index].strip() or block[index][:1] in {" ", "\t"}
            ):
                body.append(block[index].strip())
                index += 1
            fields[key] = " ".join(part for part in body if part)
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        fields[key] = value
        index += 1
    return fields


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def is_credential(rel: Path) -> bool:
    name = rel.name.lower()
    if name.startswith(".env"):
        return not any(marker in name for marker in CREDENTIAL_EXEMPT)
    if name in CREDENTIAL_FILENAMES:
        return True
    return rel.suffix.lower() in CREDENTIAL_SUFFIXES


def is_text_file(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SUFFIXES or path.name in TEXT_FILENAMES


def iter_files(skill_dir: Path):
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(skill_dir)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        yield path, rel


def file_digests(skill_dir: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path, rel in iter_files(skill_dir):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > MAX_HASH_BYTES:
            out[rel.as_posix()] = f"size:{size}"
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        out[rel.as_posix()] = "sha256:" + hashlib.sha256(data).hexdigest()
    return out


def digest_of(files: dict[str, str]) -> str:
    hasher = hashlib.sha256()
    for key in sorted(files):
        hasher.update(key.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(files[key].encode("utf-8"))
        hasher.update(b"\n")
    return "sha256:" + hasher.hexdigest()


def skill_digest(skill_dir: Path) -> tuple[str, dict[str, str]]:
    files = file_digests(skill_dir)
    return digest_of(files), files


def scan_skills() -> list[dict]:
    """列出本机所有技能，按真实路径去重。"""
    roots = discover_roots()
    library_root = roots.get("library")
    library = load_magpie_library()
    library_entries = {
        item.get("name"): item
        for item in library.get("skills", [])
        if isinstance(item, dict) and item.get("name")
    }
    seen: set[Path] = set()
    records: list[dict] = []
    for root_name, root in roots.items():
        for child in sorted(root.iterdir(), key=lambda item: item.name):
            if child.name.startswith(".") or not child.is_dir():
                continue
            doc = child / "SKILL.md"
            if not doc.is_file():
                continue
            try:
                real = child.resolve()
            except OSError:
                continue
            if real in seen:
                continue
            seen.add(real)
            text = read_text(doc) or ""
            fields = parse_frontmatter(text)
            entry = None
            record_root = root_name
            record_rel = child.name
            if library_root is not None:
                try:
                    inside = real.relative_to(library_root.resolve())
                except (OSError, ValueError):
                    inside = None
                if inside is not None:
                    # 库里的技能可能被链接到别的 Agent 目录下，路径一律按库里的算
                    record_root = "library"
                    record_rel = inside.as_posix()
                    entry = library_entries.get(child.name)
            records.append(
                {
                    "name": child.name,
                    "root": record_root,
                    "rel": record_rel,
                    "path": str(child),
                    "description": fields.get("description", ""),
                    "provider": fields.get("x-provider", ""),
                    "source": describe_source(entry, fields, child),
                    "agents": list(entry.get("agents", [])) if entry else [],
                }
            )
    records.sort(key=lambda item: (item["name"]))
    return records


def describe_source(entry: dict | None, fields: dict, path: Path) -> dict:
    if entry:
        source = entry.get("source", {}) or {}
        kind = source.get("kind", "")
        if kind == "github":
            return {
                "kind": "github",
                "repo": source.get("repo", ""),
                "subpath": source.get("path", ""),
                "commit": entry.get("commit", ""),
                "label": "magpie 库",
            }
        if kind == "folder":
            return {"kind": "folder", "dir": source.get("dir", ""), "label": "magpie 库"}
        if not kind:
            # 库里有些记录把来源写成 from: owner/repo（例如手工移交进库的技能）。
            origin = entry.get("from", "") or ""
            if "/" in origin:
                return {
                    "kind": "github",
                    "repo": origin,
                    "subpath": "",
                    "commit": entry.get("commit", ""),
                    "label": "magpie 库",
                }
        return {"kind": kind or "library", "label": "magpie 库"}
    provider = fields.get("x-provider", "")
    if provider:
        return {"kind": "plugin", "provider": provider, "label": f"插件 {provider}"}
    return {"kind": "local", "label": "本机目录"}


def find_skill(name: str) -> dict | None:
    for record in scan_skills():
        if record["name"] == name:
            return record
    return None


def resolve_entry(entry: dict) -> Path | None:
    source = entry.get("source", {}) or {}
    roots = discover_roots()
    root = roots.get(source.get("root") or entry.get("root", ""))
    if root is None:
        return None
    rel = source.get("rel") or entry.get("rel") or entry.get("name", "")
    return root / rel


def find_wf(name: str) -> Path | None:
    for record in scan_skills():
        if record["name"] != name:
            continue
        path = Path(record["path"])
        if (path / "sources.json").is_file():
            return path
    return None


def list_workflows() -> list[Path]:
    found: list[Path] = []
    for record in scan_skills():
        if not record["name"].startswith(WF_PREFIX):
            continue
        path = Path(record["path"])
        if (path / "sources.json").is_file():
            found.append(path)
    return found


def load_sources(wf_dir: Path) -> dict:
    path = wf_dir / "sources.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"无法读取 {path}：{exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"{path} 不是对象")
    data.setdefault("workflow", wf_dir.name)
    data.setdefault("sources", [])
    return data


def write_sources(wf_dir: Path, data: dict) -> None:
    data["updated"] = today()
    (wf_dir / "sources.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def source_by_name(data: dict, name: str) -> dict | None:
    for item in data["sources"]:
        if item.get("name") == name:
            return item
    return None


def record_of(record: dict) -> dict[str, str]:
    payload = dict(record["source"])
    payload["root"] = record["root"]
    payload["rel"] = record["rel"]
    return payload


def refresh_env(wf_dir: Path, quiet: bool = False) -> dict:
    data = load_sources(wf_dir)
    text, plan = render_env(wf_dir, data)
    target = wf_dir / "references" / "environment.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    data["env_digest"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    write_sources(wf_dir, data)
    if not quiet:
        print(f"已刷新 {target}")
        for line in plan:
            print(f"  {line}")
    return data


def install_command(entry: dict) -> str:
    source = entry.get("source", {})
    if source.get("kind") == "github":
        repo = source.get("repo", "")
        skill = entry.get("name", "")
        if not repo:
            return f"从 magpie 图库安装 {skill}"
        if source.get("subpath"):
            return f"npx skills add {repo} --skill {skill}"
        return f"npx skills add {repo}"
    if source.get("kind") == "folder":
        return f"来自本机目录 {source.get('dir', '')}"
    if source.get("kind") == "plugin":
        return f"安装插件 {source.get('provider', '')}"
    return "随本机技能目录同步（无上游仓库）"


def render_env(wf_dir: Path, data: dict) -> tuple[str, list[str]]:
    rows: list[str] = []
    plan: list[str] = []
    lines: list[str] = [
        f"# {data.get('workflow', wf_dir.name)} 环境清单",
        "",
        "> 由 wf-maker 生成，请勿手改；要改就改 `sources.json` 后重跑 `wf.py env`。",
        "",
        f"更新时间：{data.get('updated', today())}",
        "",
        "## 技能依赖",
        "",
        "| 技能 | 关系 | 来源 | 安装方式 |",
        "| --- | --- | --- | --- |",
    ]
    if not data["sources"]:
        lines.append("| （尚未登记） | | | |")
    for entry in data["sources"]:
        source = entry.get("source", {})
        label = source.get("kind", "")
        if source.get("kind") == "github":
            if source.get("repo"):
                label = f"GitHub {source['repo']}"
                if source.get("subpath"):
                    label += f" › {source['subpath']}"
            else:
                label = "magpie 库（未记录上游仓库）"
        elif source.get("kind") == "folder":
            label = f"本机目录 {source.get('dir', '')}"
        elif source.get("kind") == "plugin":
            label = f"插件 {source.get('provider', '')}"
        else:
            label = "本机目录"
        lines.append(
            "| {name} | {relation} | {source} | {install} |".format(
                name=entry.get("name", ""),
                relation=RELATION_LABELS.get(entry.get("mode", ""), entry.get("mode", "")),
                source=label,
                install=install_command(entry),
            )
        )
        plan.append(f"{entry.get('name', '')}: {install_command(entry)}")
        if entry.get("mode") == "bundle":
            bundle = entry.get("bundle", {})
            rows.append(
                f"- `{bundle.get('dir', '')}`（入口 {bundle.get('entry', '')}）："
                f"排除 {len(bundle.get('excluded', []))} 个凭据文件，"
                f"改写 {bundle.get('rewrites', 0)} 处本机绝对路径"
            )
    lines += [
        "",
        "## 凭据与配置",
        "",
        "本清单不记录任何密钥值。需要你自行准备的项目：",
        "",
    ]
    credentials = [item for item in data["sources"] if item.get("credentials")]
    if credentials:
        for item in credentials:
            lines.append(f"- {item['name']}：{item['credentials']}")
    else:
        lines.append("- 暂无登记；如果用到了需要密钥的步骤，用 `wf.py link <工作流> <技能> --credentials \"需要什么、放在哪\"` 补上。")
    if rows:
        lines += ["", "## 内置副本", ""] + rows
    lines += [
        "",
        "## 恢复步骤",
        "",
        "1. 按上表逐项安装或确认技能已就位。",
        "2. 用 `wf.py check <工作流>` 核对每个技能的版本是否和登记时一致。",
        "3. 凭据按「凭据与配置」一节自行准备，不要写进本目录。",
        "",
    ]
    return "\n".join(lines), plan


def cmd_roots(args) -> int:
    roots = discover_roots()
    if args.json:
        print(json.dumps({k: str(v) for k, v in roots.items()}, ensure_ascii=False, indent=2))
        return 0
    for name, path in roots.items():
        print(f"{name:<8} {path}")
    return 0


def cmd_scan(args) -> int:
    records = scan_skills()
    if args.match:
        keywords = [word for word in args.match.lower().split() if word]
        records = [
            item
            for item in records
            if any(
                word in item["name"].lower() or word in item["description"].lower()
                for word in keywords
            )
        ]
    if args.json:
        print(json.dumps(records, ensure_ascii=False, indent=2))
        return 0
    if not records:
        print("没有匹配的技能。")
        return 0
    width = max(len(item["name"]) for item in records)
    for item in records:
        source = item["source"].get("label", "")
        print(f"{item['name']:<{width}}  {item['root']:<8} [{source}]  {item['description'][:60]}")
    print(f"\n共 {len(records)} 个。")
    return 0


def slug_title(name: str) -> str:
    return name[len(WF_PREFIX) :].replace("-", " ")


def cmd_new(args) -> int:
    name = args.name
    if not NAME_RE.fullmatch(name) or len(name) > 64:
        raise SystemExit("工作流名字必须是小写 kebab-case 且以 wf- 开头，例如 wf-price-ops。")
    roots = discover_roots()
    shared = roots.get("shared") or (home() / ".agents" / "skills")
    target = shared / name
    if target.exists():
        raise SystemExit(f"{target} 已存在，先换名字或手动处理。")
    title = args.title or slug_title(name)
    (target / "references").mkdir(parents=True)
    with_sops = bool(getattr(args, "with_sops", False))
    (target / "SKILL.md").write_text(new_skill_md(name, title, with_sops), encoding="utf-8")
    (target / "references" / "playbook.md").write_text(new_playbook(title), encoding="utf-8")
    (target / "references" / "habits.md").write_text(new_habits(title), encoding="utf-8")
    if with_sops:
        sops = target / "references" / "sops"
        sops.mkdir(parents=True)
        (sops / "index.md").write_text(new_sops_index(title), encoding="utf-8")
        (sops / "_template.md").write_text(new_sops_template(), encoding="utf-8")
    data = {
        "workflow": name,
        "title": title,
        "status": "draft",
        "created": today(),
        "updated": today(),
        "sources": [],
    }
    write_sources(target, data)
    refresh_env(target, quiet=True)
    if args.json:
        print(
            json.dumps(
                {"path": str(target), "status": "draft", "sops": with_sops},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    print(f"已创建 {target}")
    print("下一步：先补 references/playbook.md 的 SOP 大纲，再登记技能依赖。")
    if with_sops:
        print("场景 SOP 骨架在 references/sops/；默认不加具体场景，用户点名后再补。")
    print(f"新技能会在下一个会话出现在技能列表里。")
    return 0


def new_skill_md(name: str, title: str, with_sops: bool = False) -> str:
    sops = (
        "\n## 场景 SOP\n\n"
        "主体的通用流程在本页和 [playbook.md](references/playbook.md)；"
        "只有特定场景需要细化时，才读 [sops/index.md](references/sops/index.md)。"
        "默认不加具体 SOP，用户点名场景后再补。\n"
        if with_sops
        else ""
    )
    return f"""---
name: {name}
description: "个人工作流（大纲态，待补充）：{title}。触发场景：TODO。不要用于 TODO。"
metadata:
  version: "0.1.0"
---

# {title}

这是个人工作流，当前是**大纲态**：阶段骨架已就位，细节待填充。
要改阶段、增减依赖或隐藏某个技能，交给 wf-maker。

## 什么时候用

TODO：写清哪些场景整段按这里走，以及单个细分任务直接找对应技能、不必走这里的边界。

## 阶段

| 阶段 | 做什么 | 用哪个技能 | 产出 |
| --- | --- | --- | --- |
| 1 | TODO | | |

## 我的习惯与红线

见 [habits.md](references/habits.md)。

## 依赖与环境

见 [environment.md](references/environment.md)，由 wf-maker 生成。
{sops}
## 详细步骤

需要具体命令、参数和判据时读 [playbook.md](references/playbook.md)。
"""


def new_playbook(title: str) -> str:
    return f"""# {title} · 详细步骤

先写大纲，再逐段填细节。每段写清：目标、输入、步骤、判据、失败时怎么办。

## 1. TODO

TODO：这一段的目标。

- 步骤：
- 判据：
"""


def new_sops_index(title: str) -> str:
    return f"""# {title} · 场景 SOP

通用流程在 `SKILL.md` 和 `playbook.md`；这里只放特定场景的细化，不要重复主体流程。

| 场景 | 细化哪一步 | 入口 | 状态 |
| --- | --- | --- | --- |
| 还没有具体场景 | 用户明确指定场景时再补 | — | — |

要新增一份 SOP：复制 [_template.md](_template.md)，按八节填完，再在表里加一行。
"""


def new_sops_template() -> str:
    return """# SOP · <场景>

> 所属主体：<工作流或技能名> 的通用流程
> 细化范围：<细化哪一步或哪几节>
> 状态：待验证
> 入口：`references/sops/<文件名>.md`

## 1. 适用与不适用

- 适用：
- 不适用：

## 2. 输入与输出

- 输入：
- 默认输出：
- 目标位置：

## 3. 分档

| 档 | 条件 | 行为 |
| --- | --- | --- |
| L1 |  |  |
| L2 |  |  |
| L3 |  |  |

## 4. 步骤

1. 定目标：
2. 读输入：
3. 做第一版：
4. 写入：
5. 回读：

## 5. 停机点

- 

## 6. 成稿模板

```markdown
<可直接套用的模板>
```

## 7. 自检

- 

## 8. 失败与回退

- 目标找不到：
- 写入失败：
"""


def new_habits(title: str) -> str:
    return f"""# {title} · 习惯与注意事项

每踩一次坑就往这里追加一条，写清「什么情况、怎么错、正确做法」。

## 通用

- TODO
"""


def need_wf(name: str) -> Path:
    path = find_wf(name)
    if path is None:
        raise SystemExit(f"没找到工作流 {name}（需要带 sources.json 的 wf- 目录）。")
    return path


def need_skill(name: str) -> dict:
    record = find_skill(name)
    if record is None:
        raise SystemExit(f"没找到技能 {name}。先跑 `wf.py scan` 看有哪些。")
    return record


def cmd_link(args) -> int:
    wf_dir = need_wf(args.workflow)
    data = load_sources(wf_dir)
    entry = source_by_name(data, args.skill)
    if args.provider:
        # 由插件提供、不在本机技能目录里的技能：只登记来源与用途，没有本地路径可对哈希。
        mode = "inline"
        source = {
            "kind": "plugin",
            "provider": args.provider,
            "label": f"插件 {args.provider}",
        }
        record = None
    else:
        record = need_skill(args.skill)
        mode = args.mode
        source = record_of(record)
    payload = {
        "name": args.skill,
        "mode": mode,
        "source": source,
        "recorded": today(),
        "uses": args.uses or "",
    }
    digest = ""
    if record is not None and mode != "inline":
        digest, files = skill_digest(Path(record["path"]))
        payload["digest"] = digest
        if len(files) <= MAX_STORED_FILES:
            payload["files"] = files
        else:
            payload["file_count"] = len(files)
    if args.credentials:
        payload["credentials"] = args.credentials
    if entry is not None:
        entry.clear()
    if entry is None:
        data["sources"].append(payload)
    else:
        entry.update(payload)
    data["sources"].sort(key=lambda item: item.get("name", ""))
    write_sources(wf_dir, data)
    refresh_env(wf_dir, quiet=True)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    relation = RELATION_LABELS.get(mode, mode)
    print(f"已登记 {args.skill}（{relation}）到 {data['workflow']}。")
    if digest:
        print(f"当时哈希：{digest}")
    if args.provider:
        print(f"来源记为插件 {args.provider}：只登记来源与用途，不比对哈希。")
    return 0


def cmd_unlink(args) -> int:
    wf_dir = need_wf(args.workflow)
    data = load_sources(wf_dir)
    entry = source_by_name(data, args.skill)
    if entry is None:
        raise SystemExit(f"{args.skill} 没有登记在 {data['workflow']} 里。")
    if entry.get("mode") == "bundle":
        raise SystemExit("这是内置副本，请用 `wf.py unbundle` 移除。")
    data["sources"].remove(entry)
    write_sources(wf_dir, data)
    refresh_env(wf_dir, quiet=True)
    if args.json:
        print(json.dumps({"removed": args.skill, "workflow": data["workflow"]}, ensure_ascii=False, indent=2))
        return 0
    print(f"已从 {data['workflow']} 移除引用 {args.skill}。")
    return 0


def rewrite_personal_paths(path: Path) -> int:
    if not is_text_file(path):
        return 0
    text = read_text(path)
    if text is None:
        return 0
    count = 0
    for pattern, replacement in PERSONAL_PATH_SUBS:
        text, hits = pattern.subn(replacement, text)
        count += hits
    if count:
        path.write_text(text, encoding="utf-8")
    return count


def bundle_skill(wf_dir: Path, record: dict) -> dict:
    source_dir = Path(record["path"])
    target = wf_dir / "vendor" / record["name"]
    if target.exists():
        shutil.rmtree(target)
    excluded: list[str] = []
    renames: dict[str, str] = {}
    rewrites = 0
    entry_name = "ENTRY.md"
    for path, rel in iter_files(source_dir):
        if is_credential(rel):
            excluded.append(rel.as_posix())
            continue
        name = rel.name
        if name == "SKILL.md":
            candidate = entry_name
            if (source_dir / rel.parent / candidate).exists() and rel.name != candidate:
                candidate = f"ENTRY-{rel.parent.name or record['name']}.md"
            renames[rel.as_posix()] = (rel.parent / candidate).as_posix()
            name = candidate
        destination = target / rel.parent / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        rewrites += rewrite_personal_paths(destination)
    digest, files = skill_digest(target)
    entry = next(iter(renames.values()), "ENTRY.md")
    return {
        "dir": (Path("vendor") / record["name"]).as_posix(),
        "entry": entry,
        "digest": digest,
        "file_count": len(files),
        "excluded": excluded,
        "renames": renames,
        "rewrites": rewrites,
    }


def cmd_bundle(args) -> int:
    wf_dir = need_wf(args.workflow)
    record = need_skill(args.skill)
    if record["name"].startswith(WF_PREFIX):
        raise SystemExit("不能把另一个工作流内置进来，改成引用。")
    existing = wf_dir / "vendor" / record["name"]
    if existing.is_dir() and not args.replace:
        raise SystemExit(
            f"{existing} 已经存在内置副本。要整个重写就加 --replace；"
            "只想看差异就跑 check。"
        )
    data = load_sources(wf_dir)
    source_digest, _ = skill_digest(Path(record["path"]))
    bundle = bundle_skill(wf_dir, record)
    payload = {
        "name": args.skill,
        "mode": "bundle",
        "source": record_of(record),
        "recorded": today(),
        "digest": source_digest,
        "uses": args.uses or "",
        "bundle": bundle,
    }
    if args.credentials:
        payload["credentials"] = args.credentials
    entry = source_by_name(data, args.skill)
    if entry is None:
        data["sources"].append(payload)
    else:
        entry.update(payload)
    data["sources"].sort(key=lambda item: item.get("name", ""))
    write_sources(wf_dir, data)
    refresh_env(wf_dir, quiet=True)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(f"已内置 {args.skill} 到 {bundle['dir']}（入口 {bundle['entry']}）。")
    if bundle["excluded"]:
        print(f"排除的凭据文件：{', '.join(bundle['excluded'])}")
    print(f"改写本机绝对路径 {bundle['rewrites']} 处；源哈希 {source_digest}")
    return 0


def cmd_unbundle(args) -> int:
    wf_dir = need_wf(args.workflow)
    data = load_sources(wf_dir)
    entry = source_by_name(data, args.skill)
    if entry is None or entry.get("mode") != "bundle":
        raise SystemExit(f"{args.skill} 不是 {data['workflow']} 的内置副本。")
    target = wf_dir / entry.get("bundle", {}).get("dir", "")
    if target.is_dir():
        shutil.rmtree(target)
    data["sources"].remove(entry)
    vendor = wf_dir / "vendor"
    if vendor.is_dir() and not any(vendor.iterdir()):
        vendor.rmdir()
    write_sources(wf_dir, data)
    refresh_env(wf_dir, quiet=True)
    if args.json:
        print(json.dumps({"removed_bundle": args.skill, "workflow": data["workflow"]}, ensure_ascii=False, indent=2))
        return 0
    print(f"已移除内置副本 {args.skill}。")
    return 0


def cmd_env(args) -> int:
    wf_dir = need_wf(args.workflow)
    data = refresh_env(wf_dir, quiet=True)
    text, plan = render_env(wf_dir, data)
    if args.json:
        print(json.dumps({"path": str(wf_dir / "references" / "environment.md"), "text": text}, ensure_ascii=False, indent=2))
        return 0
    print(text)
    if args.install_plan:
        print("可执行的恢复命令：")
        for line in plan:
            print(f"  {line}")
    return 0


def diff_files(old: dict[str, str], new: dict[str, str]) -> tuple[list[str], list[str], list[str]]:
    changed = sorted(k for k in old if k in new and old[k] != new[k])
    removed = sorted(k for k in old if k not in new)
    added = sorted(k for k in new if k not in old)
    return changed, removed, added


def agent_skill_present(agent: str, name: str) -> bool:
    roots = discover_roots()
    root = roots.get(agent)
    if root is not None and (root / name).exists():
        return True
    # 共享目录里的技能每个 Agent 都读得到，不算隐藏。
    shared = roots.get("shared")
    return bool(shared is not None and (shared / name).exists())


def cmd_show(args) -> int:
    wf_dir = need_wf(args.workflow)
    data = load_sources(wf_dir)
    rows = []
    for entry in data["sources"]:
        resolved = resolve_entry(entry)
        inline = entry.get("mode") == "inline"
        plugin = entry.get("source", {}).get("kind") == "plugin"
        rows.append(
            {
                "name": entry.get("name", ""),
                "mode": RELATION_LABELS.get(entry.get("mode", ""), entry.get("mode", "")),
                "path": "（由插件提供）" if plugin else (str(resolved) if resolved else ""),
                "exists": inline or bool(resolved and resolved.exists()),
                "hidden": False
                if plugin
                else not agent_skill_present(args.agent, entry.get("name", "")),
                "uses": entry.get("uses", ""),
            }
        )
    if args.json:
        print(json.dumps({"workflow": data["workflow"], "status": data.get("status"), "sources": rows}, ensure_ascii=False, indent=2))
        return 0
    print(f"{data['workflow']}（{data.get('status', '')}）")
    if not rows:
        print("  还没有登记任何技能。")
        return 0
    for row in rows:
        flag = "隐藏" if row["hidden"] else "显示"
        state = "" if row["exists"] else "  [路径不存在]"
        print(f"  {row['name']:<24} {row['mode']:<6} {flag}  {row['path']}{state}")
    return 0


def cmd_check(args) -> int:
    if args.workflow:
        targets = [need_wf(args.workflow)]
    else:
        targets = list_workflows()
    if not targets:
        print("还没有任何工作流。用 `wf.py new wf-名字` 建一个。")
        return 0
    reports = []
    blocking = 0
    for wf_dir in targets:
        data = load_sources(wf_dir)
        notes: list[str] = []
        for entry in data["sources"]:
            name = entry.get("name", "")
            if entry.get("mode") == "inline":
                continue
            resolved = resolve_entry(entry)
            if resolved is None or not resolved.exists():
                notes.append(f"[问题] {name}：登记的位置找不到了，引用失效。")
                blocking += 1
                continue
            if entry.get("mode") == "bundle":
                bundle_dir = wf_dir / entry.get("bundle", {}).get("dir", "")
                if not bundle_dir.is_dir():
                    notes.append(f"[问题] {name}：内置副本目录丢失。")
                    blocking += 1
                    continue
                bundle_digest, _ = skill_digest(bundle_dir)
                if bundle_digest != entry.get("bundle", {}).get("digest"):
                    notes.append(f"[注意] {name}：内置副本被改过。")
                source_digest, files = skill_digest(resolved)
                if source_digest != entry.get("digest"):
                    changed, removed, added = diff_files(entry.get("files", {}), files)
                    detail = describe_diff(changed, removed, added)
                    notes.append(f"[更新] {name}：原技能已改动（{detail}），看看要不要刷新内置副本。")
                continue
            current, files = skill_digest(resolved)
            if current != entry.get("digest"):
                changed, removed, added = diff_files(entry.get("files", {}), files)
                detail = describe_diff(changed, removed, added)
                notes.append(f"[更新] {name}：原技能已改动（{detail}），读一遍变更再决定工作流里哪几条要改。")
            if not agent_skill_present(args.agent, name) and entry["source"]["root"] == "library":
                notes.append(f"[提示] {name}：已对 {args.agent} 隐藏，工作流仍能按登记路径读到。")
        if data.get("status") == "draft":
            playbook = wf_dir / "references" / "playbook.md"
            text = read_text(playbook) or ""
            todos = text.count("TODO")
            if todos:
                notes.append(f"[大纲] playbook.md 还有 {todos} 处 TODO 未填。")
            skill_md = read_text(wf_dir / "SKILL.md") or ""
            if "TODO" in skill_md:
                notes.append("[大纲] SKILL.md 里还没写清触发场景与边界。")
            if not (wf_dir / "references" / "environment.md").is_file():
                notes.append("[大纲] 还没有环境清单，跑 `wf.py env`。")
        notes.extend(check_sops(wf_dir))
        reports.append((data["workflow"], notes))
    if args.json:
        print(json.dumps({"reports": [{"workflow": n, "notes": x} for n, x in reports], "blocking": blocking}, ensure_ascii=False, indent=2))
        return 1 if blocking else 0
    for name, notes in reports:
        print(f"{name}")
        if not notes:
            print("  一切正常。")
            continue
        for note in notes:
            print(f"  {note}")
    return 1 if blocking else 0


def describe_diff(changed: list[str], removed: list[str], added: list[str]) -> str:
    parts = []
    if changed:
        parts.append("改动 " + ", ".join(changed[:6]))
    if added:
        parts.append("新增 " + ", ".join(added[:4]))
    if removed:
        parts.append("删除 " + ", ".join(removed[:4]))
    return "；".join(parts) if parts else "文件集合不同"


SOP_LINK_RE = re.compile(r"\]\(([^)]+\.md)\)")


def check_sops(wf_dir: Path) -> list[str]:
    sops = wf_dir / "references" / "sops"
    if not sops.is_dir():
        return []
    notes: list[str] = []
    index = sops / "index.md"
    if not index.is_file():
        notes.append("[注意] references/sops 存在但没有 index.md，补一个场景索引。")
    else:
        text = read_text(index) or ""
        for link in SOP_LINK_RE.findall(text):
            if link.startswith(("http:", "https:", "#")):
                continue
            if not (sops / link).resolve().exists():
                notes.append(f"[注意] SOP 索引指向不存在的文件：{link}")
        if "_template.md" not in text and "模板" not in text:
            notes.append("[提示] SOP 索引没提模板，新增场景容易各写各的。")
    if not (sops / "_template.md").is_file():
        notes.append("[提示] 没有 _template.md，新增场景 SOP 会缺统一骨架。")
    skill = read_text(wf_dir / "SKILL.md") or ""
    if "sops/index.md" not in skill and "SOP" not in skill:
        notes.append("[提示] SKILL.md 没提场景 SOP，SOP 多了会找不到入口。")
    return notes


def cmd_links(args) -> int:
    index: dict[str, list[str]] = {}
    for wf_dir in list_workflows():
        data = load_sources(wf_dir)
        for entry in data["sources"]:
            index.setdefault(entry.get("name", ""), []).append(data["workflow"])
    if args.skill:
        users = index.get(args.skill, [])
        if args.json:
            print(json.dumps({"skill": args.skill, "workflows": users}, ensure_ascii=False, indent=2))
            return 0
        if not users:
            print(f"{args.skill} 没有被任何工作流引用，可以安全地隐藏或删除。")
            return 0
        print(f"{args.skill} 被 {len(users)} 个工作流引用：")
        for name in users:
            print(f"  {name}")
        return 0
    if args.json:
        print(json.dumps(index, ensure_ascii=False, indent=2))
        return 0
    if not index:
        print("还没有登记任何引用。")
        return 0
    for name in sorted(index):
        print(f"{name}: {', '.join(index[name])}")
    return 0


def magpie_command() -> str | None:
    return shutil.which("magpie")


def library_agents(name: str) -> list[str] | None:
    for item in load_magpie_library().get("skills", []):
        if isinstance(item, dict) and item.get("name") == name:
            return list(item.get("agents", []))
    return None


def cmd_hide(args) -> int:
    return set_visibility(args, hide=True)


def cmd_unhide(args) -> int:
    return set_visibility(args, hide=False)


def set_visibility(args, hide: bool) -> int:
    name = args.skill
    agents = library_agents(name)
    if agents is None:
        raise SystemExit(
            f"{name} 不在 magpie 库里（本机自建技能），本工具不动它。"
            "要收起它只能移动目录，那属于删除操作，需要你手动确认。"
        )
    target = args.agent
    if hide:
        new_agents = [item for item in agents if item != target]
    else:
        new_agents = agents if target in agents else agents + [target]
    command = "magpie library skill agents {name} {agents}".format(
        name=name, agents=",".join(new_agents) if new_agents else "none"
    )
    workflows = [
        load_sources(wf_dir)["workflow"]
        for wf_dir in list_workflows()
        if source_by_name(load_sources(wf_dir), name)
    ]
    if args.json:
        print(
            json.dumps(
                {
                    "skill": name,
                    "agent": target,
                    "action": "hide" if hide else "unhide",
                    "before": agents,
                    "after": new_agents,
                    "command": command,
                    "workflows": workflows,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    verb = "隐藏" if hide else "恢复显示"
    print(f"{verb} {name}（对 {target}）：{','.join(agents) or '无'} -> {','.join(new_agents) or '无'}")
    if workflows:
        print(f"被这些工作流引用：{', '.join(workflows)}")
    print(f"要执行的命令：{command}")
    if not args.apply:
        print("（确认后再加 --apply 执行；本工具不会自己动 magpie 配置）")
        return 0
    binary = magpie_command()
    if binary is None:
        raise SystemExit("没找到 magpie 命令，请手动执行上面的命令。")
    result = subprocess.run(
        [binary, "library", "skill", "agents", name, ",".join(new_agents) if new_agents else "none"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"magpie 命令失败：{result.stderr.strip() or result.stdout.strip()}")
    present = agent_skill_present(target, name)
    print(f"已执行。{target} 现在{'能看到' if present else '看不到'} {name}。")
    for wf_dir in list_workflows():
        data = load_sources(wf_dir)
        entry = source_by_name(data, name)
        if entry is not None:
            entry["hidden"] = hide
            write_sources(wf_dir, data)
    return 0


def creator_script(name: str) -> Path | None:
    """在已安装的技能里找 oil-skill-creator 的脚本。"""
    record = find_skill("oil-skill-creator")
    if record is None:
        return None
    path = Path(record["path"]) / "scripts" / name
    return path if path.is_file() else None


def resolve_target(target: str) -> Path:
    """可以用工作流名、已安装技能名，或直接给目录。"""
    wf_dir = find_wf(target)
    if wf_dir is not None:
        return wf_dir
    record = find_skill(target)
    if record is not None:
        return Path(record["path"])
    path = Path(target).expanduser()
    if path.is_dir():
        return path
    raise SystemExit(f"没找到 {target}：它不是工作流，也不是已安装的技能，也不是目录。")


def run_creator_script(args, target: str, script_name: str, extra: list[str]) -> int:
    skill_dir = resolve_target(target)
    script = creator_script(script_name)
    if script is None:
        raise SystemExit(
            f"没找到 oil-skill-creator 的 {script_name}。装回那个技能即可；"
            "暂时不方便时，先把整个工作流目录复制一份到工作区再改。"
        )
    command = [sys.executable, str(script), str(skill_dir), *extra]
    if args.json:
        command.append("--json")
    print("执行：" + " ".join(command))
    result = subprocess.run(command, capture_output=True, text=True)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


def cmd_snapshot(args) -> int:
    extra = ["--dry-run"] if args.dry_run else []
    return run_creator_script(args, args.target, "snapshot_skill.py", extra)


def cmd_bump(args) -> int:
    extra = ["--bump", args.level]
    if args.write:
        extra.append("--write")
    return run_creator_script(args, args.target, "version_skill.py", extra)


def cmd_validate(args) -> int:
    validator = creator_script("validate_skill.py")
    if validator is None:
        raise SystemExit(
            "没找到 oil-skill-creator 的校验脚本。可以装回该技能，或先手动检查："
            "目录名与 frontmatter 的 name 一致、description 写清不触发场景、"
            "SKILL.md 不超过 300 行、references 能从 SKILL.md 链接到。"
        )
    path = Path(args.target)
    if not path.is_dir():
        record = find_skill(args.target)
        if record is None:
            raise SystemExit(f"没找到技能或目录：{args.target}")
        path = Path(record["path"])
    command = [sys.executable, str(validator), str(path), "--strict", "--weak-model", "--universal"]
    if args.json:
        command.append("--json")
    result = subprocess.run(command, capture_output=True, text=True)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wf.py", description="工作流 Skill 的脚手架与维护工具")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("roots", help="列出探测到的技能根目录").set_defaults(func=cmd_roots)

    scan = sub.add_parser("scan", help="列出本机技能及其来源")
    scan.add_argument("--match", help="按名字或描述粗筛")
    scan.set_defaults(func=cmd_scan)

    new = sub.add_parser("new", help="新建工作流骨架（默认 draft）")
    new.add_argument("name")
    new.add_argument("--title", help="工作流标题")
    new.add_argument(
        "--with-sops",
        action="store_true",
        help="额外生成 references/sops/ 骨架；默认不加，用户点名场景后才需要",
    )
    new.set_defaults(func=cmd_new)

    link = sub.add_parser("link", help="登记一个引用")
    link.add_argument("workflow")
    link.add_argument("skill")
    link.add_argument("--mode", default="reference", choices=["reference", "inline"])
    link.add_argument(
        "--provider",
        help="技能由插件提供、不在本机技能目录时填插件名（记为 inline，只进环境清单）",
    )
    link.add_argument("--uses", help="在哪一步用它")
    link.add_argument("--credentials", help="需要什么凭据、放在哪（不写值）")
    link.set_defaults(func=cmd_link)

    unlink = sub.add_parser("unlink", help="移除一个引用")
    unlink.add_argument("workflow")
    unlink.add_argument("skill")
    unlink.set_defaults(func=cmd_unlink)

    bundle = sub.add_parser("bundle", help="把没有上游来源的技能内置进来")
    bundle.add_argument("workflow")
    bundle.add_argument("skill")
    bundle.add_argument("--uses", help="在哪一步用它")
    bundle.add_argument("--credentials", help="需要什么凭据、放在哪（不写值）")
    bundle.add_argument("--replace", action="store_true", help="副本已存在时整个重写")
    bundle.set_defaults(func=cmd_bundle)

    unbundle = sub.add_parser("unbundle", help="移除内置副本")
    unbundle.add_argument("workflow")
    unbundle.add_argument("skill")
    unbundle.set_defaults(func=cmd_unbundle)

    env = sub.add_parser("env", help="刷新环境清单")
    env.add_argument("workflow")
    env.add_argument("--install-plan", action="store_true", help="附带恢复命令")
    env.set_defaults(func=cmd_env)

    show = sub.add_parser("show", help="列出工作流的依赖与解析后的路径")
    show.add_argument("workflow")
    show.add_argument("--agent", default="codex", help="用哪个宿主的目录判断是否被隐藏")
    show.set_defaults(func=cmd_show)

    check = sub.add_parser("check", help="漂移检查与大纲缺项")
    check.add_argument("workflow", nargs="?", help="不填则检查全部工作流")
    check.add_argument("--agent", default="codex")
    check.set_defaults(func=cmd_check)

    links = sub.add_parser("links", help="反查某个技能被哪些工作流引用")
    links.add_argument("skill", nargs="?")
    links.set_defaults(func=cmd_links)

    hide = sub.add_parser("hide", help="对某个宿主隐藏技能（默认只打印命令）")
    hide.add_argument("skill")
    hide.add_argument("--agent", default="codex")
    hide.add_argument("--apply", action="store_true", help="执行 magpie 命令")
    hide.set_defaults(func=cmd_hide)

    unhide = sub.add_parser("unhide", help="恢复显示")
    unhide.add_argument("skill")
    unhide.add_argument("--agent", default="codex")
    unhide.add_argument("--apply", action="store_true")
    unhide.set_defaults(func=cmd_unhide)

    validate = sub.add_parser("validate", help="用 oil-skill-creator 的校验器检查")
    validate.add_argument("target", help="工作流名或路径")
    validate.set_defaults(func=cmd_validate)

    snapshot = sub.add_parser("snapshot", help="改工作流或技能前留一份不再修改的旧版快照")
    snapshot.add_argument("target", help="工作流名、技能名或目录")
    snapshot.add_argument("--dry-run", action="store_true", help="只看计划，不写入")
    snapshot.set_defaults(func=cmd_snapshot)

    bump = sub.add_parser("bump", help="按 SemVer 升工作流或技能的版本号（默认只预览）")
    bump.add_argument("target", help="工作流名、技能名或目录")
    bump.add_argument("level", choices=["major", "minor", "patch"])
    bump.add_argument("--write", action="store_true", help="写入 SKILL.md")
    bump.set_defaults(func=cmd_bump)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
