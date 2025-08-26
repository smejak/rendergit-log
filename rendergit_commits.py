#!/usr/bin/env python3
# file: rendergit_commits.py
"""
Render a repository's commit history to a single static HTML page with
a clickable sidebar of commits. Clicking a commit shows the diff against its
previous commit (first parent).

This is a companion to rendergit's "flatten files" view, but focused on history.
"""

from __future__ import annotations
import argparse
import dataclasses
import html
import pathlib
import shutil
import subprocess
import sys
import tempfile
import webbrowser
from typing import List, Optional, Tuple

from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers.diff import DiffLexer

# ---- constants & utilities ---------------------------------------------------

EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
DEFAULT_MAX_COMMITS = 200
DEFAULT_CONTEXT = 3
DEFAULT_MAX_DIFF_BYTES = 512 * 1024  # 512 KiB per-commit


def run(cmd: List[str], cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, capture_output=True)


def bytes_human(n: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    f = float(n)
    i = 0
    while f >= 1024.0 and i < len(units) - 1:
        f /= 1024.0
        i += 1
    return f"{int(f)} {units[i]}" if i == 0 else f"{f:.1f} {units[i]}"


def derive_temp_output_path(repo_url: str) -> pathlib.Path:
    parts = repo_url.rstrip("/").split("/")
    repo_name = parts[-1] if len(parts) >= 2 else "repo"
    if repo_name.endswith(".git"):
        repo_name = repo_name[:-4]
    return pathlib.Path(tempfile.gettempdir()) / f"{repo_name}-log.html"


def slug(s: str) -> str:
    out = []
    for ch in s:
        if ch.isalnum() or ch in "-_":
            out.append(ch)
        else:
            out.append("-")
    return "".join(out)


# ---- git helpers -------------------------------------------------------------

def git_clone(url: str, dst: str, depth: Optional[int] = None) -> None:
    cmd = ["git", "clone"]
    if depth is not None and depth > 0:
        cmd += ["--depth", str(depth)]
    cmd += [url, dst]
    run(cmd)


def git_head_commit(repo_dir: str) -> str:
    try:
        return run(["git", "rev-parse", "HEAD"], cwd=repo_dir).stdout.strip()
    except Exception:
        return "(unknown)"


@dataclasses.dataclass
class Commit:
    sha: str
    parents: List[str]
    subject: str
    author_name: str
    author_email: str
    author_date_iso: str
    is_merge: bool
    first_parent: Optional[str]


@dataclasses.dataclass
class CommitRender:
    commit: Commit
    files_changed: int
    insertions: int
    deletions: int
    name_status: List[Tuple[str, str]]  # e.g., [("M", "path/to/file.py")]
    patch_text: str           # raw unified diff text
    patch_truncated: bool
    patch_html: str           # highlighted HTML


def parse_commits(repo_dir: str, max_commits: int, include_merges: bool) -> List[Commit]:
    # field sep 0x1f, record sep 0x1e; ISO strict dates
    fmt = "%H%x1f%P%x1f%an%x1f%ae%x1f%ad%x1f%s%x1e"
    args = ["git", "log", f"--max-count={max_commits}", "--date=iso-strict"]
    if not include_merges:
        args.append("--no-merges")
    args += ["--pretty=format:" + fmt]
    out = run(args, cwd=repo_dir).stdout
    commits: List[Commit] = []
    for rec in out.strip("\n").split("\x1e"):
        rec = rec.strip()
        if not rec:
            continue
        h, p, an, ae, ad, s = rec.split("\x1f")
        parents = [x for x in p.split() if x]
        commits.append(
            Commit(
                sha=h,
                parents=parents,
                subject=s.strip(),
                author_name=an,
                author_email=ae,
                author_date_iso=ad,
                is_merge=len(parents) > 1,
                first_parent=(parents[0] if parents else None),
            )
        )
    return commits


def get_numstat(repo_dir: str, parent: str, sha: str) -> Tuple[int, int, int]:
    """
    Return (files_changed, insertions, deletions) using --numstat.
    """
    cp = run(["git", "diff", "--numstat", "-M", "-C", parent, sha], cwd=repo_dir)
    files_changed = insertions = deletions = 0
    for line in cp.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        a, d = parts[0], parts[1]
        if a.isdigit():
            insertions += int(a)
        if d.isdigit():
            deletions += int(d)
        files_changed += 1
    return files_changed, insertions, deletions


def get_name_status(repo_dir: str, parent: str, sha: str) -> List[Tuple[str, str]]:
    cp = run(["git", "diff", "--name-status", "-M", "-C", parent, sha], cwd=repo_dir)
    out: List[Tuple[str, str]] = []
    for line in cp.stdout.splitlines():
        # e.g. "M\tpath" or "R100\told\tnew"
        parts = line.split("\t")
        if not parts:
            continue
        status = parts[0]
        path = parts[-1]  # for R/C, last token is new path
        out.append((status, path))
    return out


def get_patch(repo_dir: str, parent: str, sha: str, context: int) -> str:
    cp = run(["git", "diff", "-M", "-C", f"-U{context}", "--no-color", parent, sha], cwd=repo_dir)
    return cp.stdout


def render_commit(repo_dir: str, commit: Commit, context: int, max_diff_bytes: int) -> CommitRender:
    parent = commit.first_parent if commit.first_parent else EMPTY_TREE_SHA
    files_changed, ins, dels = get_numstat(repo_dir, parent, commit.sha)
    name_status = get_name_status(repo_dir, parent, commit.sha)

    raw_patch = get_patch(repo_dir, parent, commit.sha, context)
    b = raw_patch.encode("utf-8", errors="ignore")
    truncated = False
    if max_diff_bytes > 0 and len(b) > max_diff_bytes:
        truncated = True
        b = b[:max_diff_bytes]
        raw_patch = b.decode("utf-8", errors="ignore") + "\n\n... [diff truncated]\n"

    formatter = HtmlFormatter(nowrap=False)
    patch_html = highlight(raw_patch, DiffLexer(), formatter)

    return CommitRender(
        commit=commit,
        files_changed=files_changed,
        insertions=ins,
        deletions=dels,
        name_status=name_status,
        patch_text=raw_patch,
        patch_truncated=truncated,
        patch_html=patch_html,
    )


# ---- HTML --------------------------------------------------------------------

def build_html(repo_url: str, repo_dir: str, head_commit: str, renders: List[CommitRender]) -> str:
    formatter = HtmlFormatter(nowrap=False)
    pygments_css = formatter.get_style_defs(".highlight")

    # Sidebar items
    def sidebar_item(r: CommitRender) -> str:
        c = r.commit
        short = c.sha[:8]
        meta = html.escape(c.author_date_iso)
        subject = html.escape(c.subject if c.subject else "(no subject)")
        cl = "merge" if c.is_merge else ""
        return (
            f'<li class="{cl}" data-sha="{c.sha}" data-author="{html.escape(c.author_name.lower())}" '
            f'data-subject="{html.escape(c.subject.lower())}" data-date="{html.escape(c.author_date_iso.lower())}">'
            f'<a href="#commit-{c.sha}" onclick="selectCommit(\'{c.sha}\')">'
            f'<code class="sha">{short}</code> <span class="subject">{subject}</span>'
            f'<div class="meta">{meta} &middot; {html.escape(c.author_name)}</div>'
            f"</a></li>"
        )

    sidebar_html = "\n".join(sidebar_item(r) for r in renders)

    # Commit sections
    def status_badge(s: str) -> str:
        # Reduce noise for rename/copy details like "R100" -> "R"
        label = s[0] if s and s[0].isalpha() else s
        title = {
            "A": "Added",
            "M": "Modified",
            "D": "Deleted",
            "R": "Renamed",
            "C": "Copied",
            "T": "Type change",
            "U": "Unmerged",
            "X": "Unknown",
            "B": "Broken",
        }.get(label, s)
        return f'<span class="badge badge-{label}" title="{html.escape(title)}">{html.escape(label)}</span>'

    def file_list(r: CommitRender) -> str:
        if not r.name_status:
            return "<em>No file changes</em>"
        items = []
        for st, path in r.name_status:
            items.append(f"<li>{status_badge(st)} <code>{html.escape(path)}</code></li>")
        return "<ul class='file-list'>" + "\n".join(items) + "</ul>"

    sections: List[str] = []
    for r in renders:
        c = r.commit
        short = c.sha[:8]
        parent_txt = c.first_parent[:8] if c.first_parent else "∅ (root)"
        subject = html.escape(c.subject) if c.subject else "(no subject)"
        header = (
            f"<h2><code class='sha'>{short}</code> {subject}</h2>"
            f"<div class='meta'>"
            f"<strong>Author:</strong> {html.escape(c.author_name)} &lt;{html.escape(c.author_email)}&gt; "
            f"&middot; <strong>Date:</strong> {html.escape(c.author_date_iso)} "
            f"&middot; <strong>Parent:</strong> {html.escape(parent_txt)} "
            f"{'&middot; <strong>Merge:</strong> yes' if c.is_merge else ''}"
            f"</div>"
        )
        stats = (
            f"<div class='stats'>"
            f"<span class='pill'>{r.files_changed} files</span>"
            f"<span class='pill plus'>+{r.insertions}</span>"
            f"<span class='pill minus'>-{r.deletions}</span>"
            f"{'<span class=\"pill warn\">truncated</span>' if r.patch_truncated else ''}"
            f"</div>"
        )

        body = (
            f"<div class='changed'>{file_list(r)}</div>"
            f"<div class='diff highlight'>{r.patch_html}</div>"
            f"<div class='back-top'><a href='#top'>↑ Back to top</a></div>"
        )

        sections.append(
            f"""
<section class="commit" id="commit-{c.sha}">
  {header}
  {stats}
  {body}
</section>
"""
        )

    html_out = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Commit history – {html.escape(repo_url)}</title>
<style>
  :root {{
    --bg:#fff; --muted:#666; --muted2:#777; --line:#eee;
    --brand:#0366d6; --pill:#f2f4f7; --plus:#0a7b34; --minus:#a01515; --warn:#8a6d3b;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font-family: -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial; line-height:1.45; }}
  code, pre {{ font-family: ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,'Liberation Mono','Courier New', monospace; }}
  a {{ color: var(--brand); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}

  .page {{ display:grid; grid-template-columns: 340px minmax(0,1fr); }}
  #sidebar {{
    position:sticky; top:0; height:100vh; overflow:auto; border-right:1px solid var(--line); background:#fafbfc;
  }}
  #sidebar .inner {{ padding: 0.75rem; }}
  #sidebar h2 {{ margin:0 0 .5rem; font-size:1rem; }}
  .filter {{ margin: .5rem 0 .75rem; }}
  .filter input {{ width:100%; padding:.5rem .65rem; border:1px solid #d1d9e0; border-radius:6px; font-size:0.95rem; }}

  #commit-list {{ list-style:none; padding:0; margin:0; }}
  #commit-list li {{ border-bottom:1px solid #f0f1f2; padding:.5rem .25rem; }}
  #commit-list li a {{ display:block; }}
  #commit-list li .sha {{ background:#eef2f7; padding:.05rem .35rem; border-radius:4px; }}
  #commit-list li .subject {{ margin-left:.35rem; }}
  #commit-list li .meta {{ color: var(--muted); font-size: .85rem; margin-top: .1rem; }}

  #commit-list li.selected {{ background: #e8f1fd; }}
  #commit-list li.merge .subject::after {{ content:"  (merge)"; color: var(--muted2); font-weight: normal; }}

  main.container {{ padding: 1rem; }}

  .repo-meta {{ margin-bottom: .75rem; }}
  .repo-meta small {{ color: var(--muted); }}

  .commit {{ padding: 1rem 0; border-top: 1px solid var(--line); }}
  .commit h2 {{ margin: 0 0 .4rem 0; font-size: 1.1rem; }}
  .commit .meta {{ color: var(--muted); font-size: .9rem; margin-bottom: .3rem; }}
  .stats {{ display:flex; gap:.5rem; align-items:center; margin: .25rem 0 .5rem; flex-wrap: wrap; }}
  .pill {{ background: var(--pill); border:1px solid #e1e5ea; padding:.15rem .5rem; border-radius: 999px; font-size:.85rem; }}
  .pill.plus {{ color: var(--plus); border-color: #dfeee6; background:#f6fbf7; }}
  .pill.minus {{ color: var(--minus); border-color: #f1d8d8; background:#fdf7f7; }}
  .pill.warn {{ color: var(--warn); border-color: #efe3c0; background: #fdf8e7; }}
  .changed {{ margin: .5rem 0 .5rem; }}
  .file-list {{ list-style: none; padding: 0; margin: .25rem 0 .25rem; }}
  .file-list li {{ padding: .12rem 0; }}
  .badge {{ display:inline-block; font-size:.75rem; padding:.05rem .4rem; border-radius:999px; margin-right:.35rem; border:1px solid #d1d9e0; background:#fff; }}
  .badge-A {{ background:#eefbf2; border-color:#dbeee0; }}
  .badge-M {{ background:#eef2fb; border-color:#dfe3f6; }}
  .badge-D {{ background:#fdf0f0; border-color:#f3dcdc; }}
  .badge-R {{ background:#fff6ea; border-color:#f1e3c9; }}
  .badge-C {{ background:#f2f9ff; border-color:#dbe9f6; }}

  pre {{ background:#f6f8fa; padding:.75rem; overflow:auto; border-radius:6px; }}
  .highlight {{ overflow-x: auto; }}
  .back-top {{ margin-top: .4rem; font-size: .9rem; }}

  @media (max-width: 900px) {{
    .page {{ grid-template-columns: 1fr; }}
    #sidebar {{ position:static; height:auto; }}
  }}

  /* Pygments */
  {pygments_css}
</style>
</head>
<body>
<a id="top"></a>
<div class="page">
  <nav id="sidebar">
    <div class="inner">
      <h2>Commits ({len(renders)})</h2>
      <div class="filter">
        <input type="text" id="filter" placeholder="Filter by message, author, SHA..." oninput="filterCommits()" />
      </div>
      <ul id="commit-list">
        {sidebar_html}
      </ul>
    </div>
  </nav>

  <main class="container">
    <section class="repo-meta">
      <div><strong>Repository:</strong> <a href="{html.escape(repo_url)}">{html.escape(repo_url)}</a></div>
      <small><strong>HEAD commit:</strong> {html.escape(head_commit)}</small>
    </section>

    {"".join(sections)}
  </main>
</div>

<script>
function selectCommit(sha) {{
  // highlight in sidebar
  document.querySelectorAll('#commit-list li').forEach(li => {{
    li.classList.toggle('selected', li.getAttribute('data-sha') === sha);
  }});
  // anchor navigation scrolls to the commit section
}}

function filterCommits() {{
  const q = (document.getElementById('filter').value || '').toLowerCase();
  const items = document.querySelectorAll('#commit-list li');
  items.forEach(li => {{
    const hay = (li.getAttribute('data-subject') + ' ' + li.getAttribute('data-author') + ' ' + li.getAttribute('data-sha')).toLowerCase();
    li.style.display = hay.indexOf(q) >= 0 ? '' : 'none';
  }});
}}

// Auto-select first commit when page loads (nice default)
window.addEventListener('DOMContentLoaded', () => {{
  const first = document.querySelector('#commit-list li a');
  if (first) first.click();
}});
</script>
</body>
</html>
"""
    return html_out


# ---- main --------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Render a repo's commit history as a single HTML page")
    ap.add_argument("repo_url", help="GitHub repo URL (https://github.com/owner/repo[.git])")
    ap.add_argument("--out", "-o", help="Output HTML file path (default: <repo>-log.html in temp dir)")
    ap.add_argument("--max-commits", type=int, default=DEFAULT_MAX_COMMITS, help="Maximum number of commits to render")
    ap.add_argument("--include-merges", action="store_true", help="Include merge commits (diff vs first parent)")
    ap.add_argument("--clone-depth", type=int, default=None, help="Shallow clone depth (default: full)")
    ap.add_argument("-U", "--context", type=int, default=DEFAULT_CONTEXT, help="Diff context lines")
    ap.add_argument("--max-diff-bytes", type=int, default=DEFAULT_MAX_DIFF_BYTES, help="Truncate per-commit diff after this many bytes (0 to disable)")
    ap.add_argument("--no-open", action="store_true", help="Don't open the HTML file after generation")
    args = ap.parse_args()

    out_path = pathlib.Path(args.out) if args.out else derive_temp_output_path(args.repo_url)

    tmpdir = tempfile.mkdtemp(prefix="rendergit_log_")
    repo_dir = pathlib.Path(tmpdir, "repo")

    try:
        print(f"📁 Cloning {args.repo_url} → {repo_dir}", file=sys.stderr)
        git_clone(args.repo_url, str(repo_dir), depth=args.clone_depth)
        head = git_head_commit(str(repo_dir))
        print(f"✓ Clone complete (HEAD: {head[:8]})", file=sys.stderr)

        print(f"📜 Reading history (max {args.max_commits}{' including merges' if args.include_merges else ''})...", file=sys.stderr)
        commits = parse_commits(str(repo_dir), args.max_commits, include_merges=args.include_merges)
        if not commits:
            print("No commits found.", file=sys.stderr)
            return 1

        print(f"🧮 Rendering diffs with -U {args.context} (per-commit cap: {bytes_human(args.max_diff_bytes) if args.max_diff_bytes else 'unlimited'})", file=sys.stderr)
        renders: List[CommitRender] = []
        for c in commits:
            renders.append(render_commit(str(repo_dir), c, context=args.context, max_diff_bytes=args.max_diff_bytes))

        print("🔨 Building HTML...", file=sys.stderr)
        html_out = build_html(args.repo_url, str(repo_dir), head, renders)

        print(f"💾 Writing: {out_path.resolve()}", file=sys.stderr)
        out_path.write_text(html_out, encoding="utf-8")

        if not args.no_open:
            print("🌐 Opening in browser...", file=sys.stderr)
            webbrowser.open(f"file://{out_path.resolve()}")

        print(f"🗑️  Cleaning up {tmpdir}", file=sys.stderr)
        return 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
