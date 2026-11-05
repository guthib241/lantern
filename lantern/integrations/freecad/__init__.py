"""S17 FreeCAD connector: Lantern does real CAD work in FreeCAD.

  ops.py        the typed operations and their argument rules (no "run Python" operation)
  tree.py       the document as a parametric feature tree (shared by both backends)
  worker.py     runs inside FreeCAD (FreeCADCmd) and evaluates the tree with the Part kernel
  fake.py       a stand-in with voxel geometry, for tests and the cloud sandbox
  backend.py    finds FreeCAD and talks to the worker; picks real or fake
  render.py     three-view PNG renders for the visual check (stdlib only)
  server.py     the MCP server Lantern's MCP client starts (python -m lantern.integrations.freecad.server)
  tiers.py      green / yellow / red risk tiers, privacy-zone checks, approval summaries
  verify.py     numeric checks from FreeCAD, then Muse Spark looks at the render (structured output)
  connector.py  config for mcp.json, the Toolbox hook target, and the one-fix-then-honest loop
Kept light on import: FreeCAD's own Python loads tree.py and ops.py through this package.
"""
