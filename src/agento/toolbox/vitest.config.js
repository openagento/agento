import { defineConfig } from 'vitest/config';

// The suite mostly waits on git and file I/O, not on the CPU, so it gains from more
// forks than vitest's default (CPUs - 1, i.e. one fork on a 2-CPU host).
export default defineConfig({
  test: {
    maxWorkers: 10,
    minWorkers: 1,
  },
});
