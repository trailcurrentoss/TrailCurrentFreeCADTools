# MCP server backlog

Open work for `mcp/`, in dependency order. Every item names the file to
change and the check that proves it done. Checks are run against a live
FreeCAD 1.1 instance through `mcp/tests/smoke_test.py`, the same way the
defects were found.

An item is removed from this file when it ships; the version history in
`package.xml` and the git log record what shipped when. Bump `<version>` on
every release: minor for a phase, patch for a fix. The Addon Manager ignores
anything that is not newer.

---

Nothing is open. Add the next item below this line when it is found, with
the file to change and the check that proves it done.
