// emoji-picker-element styles its shadow root with one <style> element, whose
// text is fixed per version. The page CSP allows exactly that text by hash
// (EMOJI_PICKER_STYLE_HASH in app/__init__.py). Upgrading the vendored bundle
// changes the text: this fails until the hash is updated, instead of the
// picker silently losing its styles in production.
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { JSDOM } from 'jsdom';
import { describe, expect, it } from 'vitest';
import { ROOT, readSource } from './helpers/env.js';

const BUNDLE = path.join(ROOT, 'app/static/js/local/emoji-picker-element.bundle.min.js');

function pickerStyleText() {
    const dom = new JSDOM('<!doctype html><html><body></body></html>', { runScripts: 'outside-only', pretendToBeVisual: true });
    // The picker opens its emoji database on connect; it never gets that far
    // here, but keep a stray lookup from throwing after the test.
    dom.window.indexedDB = { open: () => ({}) };
    dom.window.eval(fs.readFileSync(BUNDLE, 'utf8'));
    const picker = dom.window.document.createElement('emoji-picker');
    const styles = [...picker.shadowRoot.querySelectorAll('style')].map((s) => s.textContent);
    dom.window.close();
    return styles;
}

describe('emoji-picker shadow <style> and the CSP hash', () => {
    it('is a single static stylesheet whose hash the policy allows', () => {
        const styles = pickerStyleText();
        expect(styles).toHaveLength(1);
        const hash = `sha256-${crypto.createHash('sha256').update(styles[0], 'utf8').digest('base64')}`;
        const configured = readSource('app/__init__.py').match(/EMOJI_PICKER_STYLE_HASH = "([^"]+)"/);
        expect(configured, 'EMOJI_PICKER_STYLE_HASH in app/__init__.py').not.toBeNull();
        expect(configured[1], `set EMOJI_PICKER_STYLE_HASH = "${hash}"`).toBe(hash);
    });
});
