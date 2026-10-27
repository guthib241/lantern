"""Self-compiling routines (S4): a verified task becomes a small program that runs with no model calls.

  contract.py  the Routine Contract (what it does, as data) and its rules
  checker.py   the static whitelist every routine.py must pass
  runtime.py   the only API a routine may use (browser, files, vault, check, step, uia)
  store.py     versions on disk: ~/.lantern/routines/<name>/v<N>/
  runner.py    runs a routine: plan approval, steps, postconditions, healing hooks
The compiler (lantern/compiler.py) turns a task into a contract and code, verifies it and heals it.
"""
