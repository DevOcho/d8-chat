// Unit tests for the browser JavaScript in app/static/js.
//
// Each test builds its own jsdom window (tests/js/helpers/env.js) and loads the
// real scripts into it, so the Vitest environment itself is plain Node.
import { defineConfig } from 'vitest/config';

// Timestamp tests assert on browser-local times; pin the zone and locale so
// they don't depend on the machine running them. Set before the test workers
// start so they inherit it.
process.env.TZ = 'America/New_York';
process.env.LC_ALL = 'en_US.UTF-8';

export default defineConfig({
    test: {
        include: ['tests/js/**/*.test.js'],
        environment: 'node',
        setupFiles: ['tests/js/helpers/setup.js'],
        // Fail fast on a hung page rather than at the 5s default per test.
        testTimeout: 10000,
    },
});
