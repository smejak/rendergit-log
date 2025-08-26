# rendergit-log
Render the commit history of any repo in a single static HTML (inspired by Karpathy's rendergit).

> Just show me the diffs.

`rendergit-log` is based on [rendergit](https://github.com/karpathy/rendergit), but instead of flattening files, it flattens a repository’s **commit history** into a single static HTML page. Get an instant, clickable list of commits on the left; click any commit to see the **diff against its previous commit** on the right.

Perfect for quick historical code review, skimming what changed, and instant Ctrl+F across patches.

## Basic usage

Install and run (using [uv](https://docs.astral.sh/uv/) or pip):

```bash
# local editable install
pip install -e .

# or with uv
uv tool install .
```
### Examples

```bash
# last 100 commits (skip merges)
rendergit-log https://github.com/karpathy/nanoGPT --max-commits 100

# include merge commits (diff vs first parent)
rendergit-log https://github.com/owner/repo --include-merges

# shallow clone roughly matching your window (faster on huge repos)
rendergit-log https://github.com/owner/repo --max-commits 300 --clone-depth 350

# increase context lines in unified diff
rendergit-log https://github.com/owner/repo -U 6
```

```bash
usage: rendergit-log [-h] [--out OUT] [--max-commits N] [--include-merges]
                     [--clone-depth N] [-U CONTEXT] [--max-diff-bytes BYTES]
                     [--no-open]
                     repo_url
```
