# Phase p7

- status: running
- wave: 4
- branch: `plan/di-container-split/phase-p7`
- base: `plan/di-container-split/integ-p7`
- depends on: `p2`, `p4`
- harness: claude-code
- gates: `lint`, `typecheck`, `test`
