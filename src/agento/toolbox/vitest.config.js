import { availableParallelism } from 'node:os';
import { defineConfig } from 'vitest/config';

// The suite mostly waits on git and file I/O, so it gains from one fork per CPU (vitest's
// default is CPUs - 1, i.e. one fork on a 2-CPU host). Capped at 10: more forks than CPUs
// push the git-heavy tests past their 5s timeout on a small host.
export default defineConfig({
  test: {
    maxWorkers: Math.min(10, availableParallelism()),
    minWorkers: 1,
  },
});
