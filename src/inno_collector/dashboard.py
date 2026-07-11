from __future__ import annotations

import json
import re
from pathlib import Path

from .package import _open_regular
from .vault import _atomic_write


class DashboardError(ValueError):
    pass


_REPORT_NUMBER = re.compile(r"^- (项目数|失败项目数|文章总数)：(\d+)\s*$", re.MULTILINE)


def _load_data(vault: Path) -> dict[str, object]:
    try:
        manifest = json.loads(
            _open_regular(vault / "90-系统/manifest.json").decode("utf-8")
        )
        report = _open_regular(vault / "90-系统/collection-report.md").decode("utf-8")
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise DashboardError("unable to read dashboard source data") from None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("articles"), dict):
        raise DashboardError("invalid dashboard manifest")
    numbers = {name: int(value) for name, value in _REPORT_NUMBER.findall(report)}
    if set(numbers) != {"项目数", "失败项目数", "文章总数"}:
        raise DashboardError("invalid dashboard report")

    articles: list[dict[str, str]] = []
    for key, raw in manifest["articles"].items():
        if not isinstance(key, str) or not isinstance(raw, dict):
            raise DashboardError("invalid dashboard article")
        row = {
            "id": key,
            "project": str(raw.get("project", "")),
            "account": str(raw.get("account", "")),
            "title": str(raw.get("title", "")),
            "published": str(raw.get("published", "")),
            "sourceUrl": str(raw.get("source_url", "")),
            "path": str(raw.get("path", "")),
        }
        articles.append(row)
    articles.sort(
        key=lambda row: (row["published"], row["title"], row["id"]),
        reverse=True,
    )
    projects = sorted({row["project"] for row in articles if row["project"]})
    return {
        "projectCount": numbers["项目数"],
        "failedProjects": numbers["失败项目数"],
        "articleCount": numbers["文章总数"],
        "projects": projects,
        "articles": articles,
    }


def _safe_embedded_json(value: object) -> str:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _render(data: dict[str, object]) -> str:
    status = "部分成功" if int(data["failedProjects"]) else "全部成功"
    embedded = _safe_embedded_json(data)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>离线资讯看板</title>
<style>
:root{{--bg:#f6f7f9;--card:#fff;--ink:#1f2937;--muted:#667085;--line:#e4e7ec;--accent:#2563eb}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}}
main{{max-width:1100px;margin:auto;padding:32px 20px}}h1{{margin:0 0 8px}}.subtitle{{color:var(--muted);margin-bottom:24px}}
.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px}}.card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px}}
.value{{font-size:26px;font-weight:700}}.label{{color:var(--muted);margin-top:4px}}.controls{{display:flex;gap:12px;margin:16px 0}}
input,select{{width:100%;border:1px solid var(--line);border-radius:10px;padding:10px;background:#fff}}table{{width:100%;border-collapse:collapse;background:#fff;border-radius:14px;overflow:hidden}}
th,td{{text-align:left;padding:12px;border-bottom:1px solid var(--line)}}th{{color:var(--muted);font-weight:600}}a{{color:var(--accent);text-decoration:none}}.empty{{padding:28px;text-align:center;color:var(--muted)}}
@media(max-width:700px){{.stats{{grid-template-columns:1fr 1fr}}.controls{{flex-direction:column}}th:nth-child(2),td:nth-child(2){{display:none}}}}
</style>
</head>
<body><main>
<h1>离线资讯看板</h1><p class="subtitle">当前资料状态：{status}</p>
<section class="stats">
<div class="card"><div class="value">{data['projectCount']}</div><div class="label">项目数</div></div>
<div class="card"><div class="value">{data['articleCount']}</div><div class="label">文章数</div></div>
<div class="card"><div class="value">{data['failedProjects']}</div><div class="label">部分失败项目</div></div>
<div class="card"><div class="value" id="visible-count">{data['articleCount']}</div><div class="label">当前结果</div></div>
</section>
<section class="controls"><input id="search" type="search" placeholder="搜索标题、项目或公众号"><select id="project-filter"><option value="">全部项目</option></select></section>
<table><thead><tr><th>文章</th><th>项目</th><th>公众号</th><th>发布日期</th></tr></thead><tbody id="rows"></tbody></table>
<div id="empty" class="empty" hidden>没有匹配的文章</div>
</main>
<script>
const DATA={embedded};
const search=document.getElementById("search"),filter=document.getElementById("project-filter"),rows=document.getElementById("rows"),empty=document.getElementById("empty"),count=document.getElementById("visible-count");
for(const project of DATA.projects){{const option=document.createElement("option");option.value=project;option.textContent=project;filter.appendChild(option)}}
function render(){{const query=search.value.trim().toLocaleLowerCase();const project=filter.value;const visible=DATA.articles.filter(item=>(!project||item.project===project)&&(!query||[item.title,item.project,item.account].join(" ").toLocaleLowerCase().includes(query)));rows.replaceChildren();for(const item of visible){{const tr=document.createElement("tr");const title=document.createElement("td"),link=document.createElement("a");link.textContent=item.title;link.href=item.sourceUrl;link.rel="noreferrer";title.appendChild(link);for(const value of [item.project,item.account,item.published]){{const td=document.createElement("td");td.textContent=value;tr.appendChild(td)}}tr.prepend(title);rows.appendChild(tr)}}count.textContent=String(visible.length);empty.hidden=visible.length!==0}}
search.addEventListener("input",render);filter.addEventListener("change",render);render();
</script></body></html>
"""


def build_dashboard(vault: Path) -> Path:
    root = Path(vault).resolve()
    output = root / "80-离线看板/index.html"
    payload = _render(_load_data(root)).encode("utf-8")
    _atomic_write(output, payload)
    return output
