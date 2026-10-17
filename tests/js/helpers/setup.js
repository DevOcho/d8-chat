import { afterEach } from 'vitest';
import { closeAllPages } from './env.js';

// Tear down every page a test created; this also fails the test if app code
// threw an uncaught exception that the test didn't explicitly allow.
afterEach(async () => {
    await closeAllPages();
});
