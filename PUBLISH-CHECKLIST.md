# Publish checklist for idemkey 0.3.0 (for the owner - nothing here has been run)

**Version to release: 0.3.0.** PyPI currently has 0.1.1 only (checked 2026-11-07).
0.2.0 was never released, so 0.3.0 includes everything in the CHANGELOG under
"0.3.0 - unreleased". It is a minor bump for a 0.x package, but it is **not fully
backward compatible with 0.1.1**: stored keys are now namespaced
(`scope|tool|prefix|key`). Someone with a 0.1.1 store keeps working only if they
pass `namespace=False`. The CHANGELOG says so at the top of the 0.3.0 entry.

## Decide first

1. Decide the public repo address, so `project.urls` (Repository, Issues,
   Changelog) can be added. Without it the PyPI page has no links.
2. Change the heading "0.3.0 - unreleased" in CHANGELOG.md to "0.3.0 - <date>"
   on release day (the version test only checks that "## 0.3.0" appears).
3. You need a PyPI account with a token (or "trusted publishing" set up). Uploading
   is free; nothing in this project costs money.

## Check before you upload (in a scratch folder, not in the repo)

```bash
python -m venv /tmp/idemkey-check && source /tmp/idemkey-check/bin/activate
pip install build twine pytest
cd <your checkout of the repo>
python -m pytest -q                   # expect: all passed (a few MCP tests skip if mcp is not installed)
python -m build --outdir /tmp/idemkey-dist .
twine check /tmp/idemkey-dist/*       # expect: PASSED twice
unzip -l /tmp/idemkey-dist/*.whl      # expect: LICENSE inside licenses/, py.typed present
pip install /tmp/idemkey-dist/*.whl   # a normal, non-editable install
idemkey --version                     # expect: idemkey 0.3.0
idemkey demo                          # expect: "idemkey works on this machine."
```

Also look at the GitHub "tests" run on a pushed branch: the Windows and macOS
jobs have never run for this project. If they fail, fix before releasing or say
"Linux tested" on the PyPI page (the README already says so).

## Upload (only after the checks above pass)

```bash
twine upload --repository testpypi /tmp/idemkey-dist/*    # optional dry run on TestPyPI
twine upload /tmp/idemkey-dist/*                          # the real one
git tag v0.3.0 && git push origin v0.3.0                  # after the upload works
```

After the upload: in a new empty venv run `pip install idemkey==0.3.0`, then
`idemkey demo`. Then remove the sentence "PyPI currently has 0.1.1 only ..." from
the README Install section.
