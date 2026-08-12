#!/usr/bin/env node
/**
 * Deterministic extension packaging.
 *
 * The ZIP is built by hand rather than by shelling out to `zip`, so the byte
 * stream is fully controlled: entries are sorted, timestamps are pinned to a
 * fixed epoch and no OS metadata is embedded. Building the same commit twice
 * therefore produces the same SHA-256, which is what lets IT verify that the
 * artifact they push through Chrome Enterprise is the one CI built.
 */

import { createHash } from 'node:crypto';
import { deflateRawSync } from 'node:zlib';
import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '..');
const dist = join(root, 'dist');
const outputDir = resolve(root, '../../artifacts');

// 2020-01-01T00:00:00Z in DOS date/time form. Any fixed value works; what
// matters is that it never varies between builds.
const DOS_TIME = 0;
const DOS_DATE = ((2020 - 1980) << 9) | (1 << 5) | 1;

function walk(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...walk(full));
    else out.push(full);
  }
  return out;
}

function crc32(buffer) {
  let table = crc32.table;
  if (!table) {
    table = new Int32Array(256);
    for (let index = 0; index < 256; index += 1) {
      let value = index;
      for (let bit = 0; bit < 8; bit += 1) {
        value = value & 1 ? 0xedb88320 ^ (value >>> 1) : value >>> 1;
      }
      table[index] = value;
    }
    crc32.table = table;
  }
  let crc = -1;
  for (let index = 0; index < buffer.length; index += 1) {
    crc = (crc >>> 8) ^ table[(crc ^ buffer[index]) & 0xff];
  }
  return (crc ^ -1) >>> 0;
}

function buildZip(files) {
  const localParts = [];
  const centralParts = [];
  let offset = 0;

  for (const { name, data } of files) {
    const nameBytes = Buffer.from(name, 'utf8');
    const compressed = deflateRawSync(data, { level: 9 });
    const useStore = compressed.length >= data.length;
    const payload = useStore ? data : compressed;
    const method = useStore ? 0 : 8;
    const checksum = crc32(data);

    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4); // version needed
    local.writeUInt16LE(0, 6); // flags
    local.writeUInt16LE(method, 8);
    local.writeUInt16LE(DOS_TIME, 10);
    local.writeUInt16LE(DOS_DATE, 12);
    local.writeUInt32LE(checksum, 14);
    local.writeUInt32LE(payload.length, 18);
    local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(nameBytes.length, 26);
    local.writeUInt16LE(0, 28);
    localParts.push(local, nameBytes, payload);

    const central = Buffer.alloc(46);
    central.writeUInt32LE(0x02014b50, 0);
    central.writeUInt16LE(20, 4); // version made by
    central.writeUInt16LE(20, 6); // version needed
    central.writeUInt16LE(0, 8);
    central.writeUInt16LE(method, 10);
    central.writeUInt16LE(DOS_TIME, 12);
    central.writeUInt16LE(DOS_DATE, 14);
    central.writeUInt32LE(checksum, 16);
    central.writeUInt32LE(payload.length, 20);
    central.writeUInt32LE(data.length, 24);
    central.writeUInt16LE(nameBytes.length, 28);
    central.writeUInt16LE(0, 30); // extra
    central.writeUInt16LE(0, 32); // comment
    central.writeUInt16LE(0, 34); // disk
    central.writeUInt16LE(0, 36); // internal attrs
    central.writeUInt32LE(0o644 << 16, 38); // external attrs: rw-r--r--
    central.writeUInt32LE(offset, 42);
    centralParts.push(central, nameBytes);

    offset += local.length + nameBytes.length + payload.length;
  }

  const centralDirectory = Buffer.concat(centralParts);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(0, 4);
  end.writeUInt16LE(0, 6);
  end.writeUInt16LE(files.length, 8);
  end.writeUInt16LE(files.length, 10);
  end.writeUInt32LE(centralDirectory.length, 12);
  end.writeUInt32LE(offset, 16);
  end.writeUInt16LE(0, 20);

  return Buffer.concat([...localParts, centralDirectory, end]);
}

if (!existsSync(dist)) {
  console.error('dist/ not found - run `npm run build` first');
  process.exit(1);
}

const manifest = JSON.parse(readFileSync(join(dist, 'manifest.json'), 'utf8'));
const files = walk(dist)
  .map((full) => ({ name: relative(dist, full).split('\\').join('/'), full }))
  .sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))
  .map(({ name, full }) => ({ name, data: readFileSync(full) }));

if (files.length === 0) {
  console.error('dist/ is empty');
  process.exit(1);
}

const zip = buildZip(files);
mkdirSync(outputDir, { recursive: true });

const zipName = `techsara-chatgpt-archive-extension-${manifest.version}.zip`;
const zipPath = join(outputDir, zipName);
writeFileSync(zipPath, zip);

const digest = createHash('sha256').update(zip).digest('hex');
writeFileSync(join(outputDir, `${zipName}.sha256`), `${digest}  ${zipName}\n`);

console.log(`packaged  ${zipPath}`);
console.log(`files     ${files.length}`);
console.log(`bytes     ${zip.length}`);
console.log(`sha256    ${digest}`);
