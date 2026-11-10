#!/usr/bin/env node
// Port of the supplied APK's m0.b.b() and m0.a.a(); decrypts the outer container.
import fs from 'node:fs';
import { createDecipheriv } from 'node:crypto';

const [input, output] = process.argv.slice(2);
if (!input || !output) throw new Error('Usage: node decode_ghm.mjs INPUT.ghm OUTPUT.zip');
const base = Buffer.from('1234567890121234567890121234', 'utf8');
const key = Buffer.alloc(base.length + 4);
base.copy(key);
let source;
let destination;
let created = false;
let success = false;
try {
  source = fs.openSync(input, 'r');
  destination = fs.openSync(output, 'wx');
  created = true;
  const block = Buffer.alloc(16384);
  for (let index = 1; ; index++) {
    let length = 0;
    while (length < block.length) {
      const count = fs.readSync(source, block, length, block.length - length, null);
      if (count === 0) break;
      length += count;
    }
    if (!length) break;
    if (length % 16) throw new Error('Invalid GHM block length');
    key.writeUInt32BE(index, base.length);
    const cipher = createDecipheriv('aes-256-cbc', key, key.subarray(0, 16));
    const data = Buffer.concat([cipher.update(block.subarray(0, length)), cipher.final()]);
    fs.writeFileSync(destination, data);
  }
  success = true;
} finally {
  if (source !== undefined) fs.closeSync(source);
  if (destination !== undefined) fs.closeSync(destination);
  if (created && !success) fs.unlinkSync(output);
}
