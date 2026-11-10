#!/usr/bin/env node
/**
 * 从 C16 冻结契约生成前端 TypeScript 类型与路径表。
 *
 * 契约唯一真源：contracts/api/product_api.openapi.v1.json（由 apps/api/contract.py 产出）。
 * 本脚本不引入任何第三方依赖，只用 Node 标准库。
 *
 *   node scripts/gen-api-types.mjs           # 重新生成 src/api/generated/contract.ts
 *   node scripts/gen-api-types.mjs --check   # 校验生成物是否与契约一致（漂移即非零退出）
 */

import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const WEB_ROOT = resolve(HERE, '..');
const REPO_ROOT = resolve(WEB_ROOT, '..');
const CONTRACT_PATH = resolve(REPO_ROOT, 'contracts/api/product_api.openapi.v1.json');
const OUTPUT_PATH = resolve(WEB_ROOT, 'src/api/generated/contract.ts');

const HTTP_METHODS = ['get', 'post', 'put', 'patch', 'delete'];

function refName(ref) {
  return sanitizeName(String(ref).split('/').pop() ?? '');
}

function sanitizeName(raw) {
  const cleaned = String(raw).replace(/[^A-Za-z0-9]+/g, ' ').trim();
  const parts = cleaned.split(/\s+/).filter(Boolean);
  const name = parts.map((part) => part[0].toUpperCase() + part.slice(1)).join('');
  if (!/^[A-Za-z_$]/.test(name)) return `T${name}`;
  return name || 'UnknownSchema';
}

function union(types) {
  const unique = [...new Set(types.filter(Boolean))];
  if (unique.length === 0) return 'unknown';
  if (unique.length === 1) return unique[0];
  return unique.map((type) => (type.includes(' | ') ? `(${type})` : type)).join(' | ');
}

function literal(value) {
  if (typeof value === 'string') return JSON.stringify(value);
  return String(value);
}

function tsType(schema, nameMap) {
  if (!schema || typeof schema !== 'object') return 'unknown';

  if (schema.$ref) {
    const target = refName(schema.$ref);
    return nameMap.get(target) ?? target;
  }

  if (Array.isArray(schema.enum)) {
    return union(schema.enum.map(literal));
  }

  if (Array.isArray(schema.anyOf) || Array.isArray(schema.oneOf)) {
    const variants = schema.anyOf ?? schema.oneOf;
    return union(variants.map((variant) => tsType(variant, nameMap)));
  }

  if (Array.isArray(schema.allOf)) {
    return schema.allOf.map((variant) => tsType(variant, nameMap)).join(' & ') || 'unknown';
  }

  const type = schema.type;

  if (Array.isArray(type)) {
    return union(type.map((item) => tsType({ ...schema, type: item }, nameMap)));
  }

  if (type === 'array') {
    const inner = tsType(schema.items, nameMap);
    return /[|&]/.test(inner) ? `(${inner})[]` : `${inner}[]`;
  }

  if (type === 'object' || schema.properties) {
    const properties = schema.properties ?? {};
    const required = new Set(schema.required ?? []);
    const keys = Object.keys(properties);
    if (keys.length === 0) {
      const extra = schema.additionalProperties;
      if (extra === true || (extra && typeof extra === 'object')) {
        return 'Record<string, unknown>';
      }
      return 'Record<string, never>';
    }
    const body = keys
      .sort()
      .map((key) => {
        const value = properties[key];
        const optional = required.has(key) ? '' : '?';
        const comment = value?.description ? ` /** ${String(value.description).replace(/\*\//g, '*\\/')} */` : '';
        return `  ${safeKey(key)}${optional}: ${tsType(value, nameMap)};${comment}`;
      })
      .join('\n');
    return `{\n${body}\n}`;
  }

  switch (type) {
    case 'string':
      return 'string';
    case 'integer':
    case 'number':
      return 'number';
    case 'boolean':
      return 'boolean';
    case 'null':
      return 'null';
    default:
      return 'unknown';
  }
}

function safeKey(key) {
  return /^[A-Za-z_$][A-Za-z0-9_$]*$/.test(key) ? key : JSON.stringify(key);
}

function schemaComment(schema) {
  const text = schema?.description;
  if (!text) return '';
  return `/**\n * ${String(text).replace(/\*\//g, '*\\/').replace(/\n/g, '\n * ')}\n */\n`;
}

function buildNameMap(schemas) {
  const map = new Map();
  const used = new Map();
  for (const rawName of Object.keys(schemas)) {
    const name = sanitizeName(rawName);
    if (used.has(name) && used.get(name) !== rawName) {
      throw new Error(`契约 schema 名称冲突：${rawName} 与 ${used.get(name)} 都映射到 ${name}`);
    }
    used.set(name, rawName);
    map.set(rawName, name);
  }
  return map;
}

function renderSchemas(schemas, nameMap) {
  return Object.keys(schemas)
    .sort()
    .map((rawName) => {
      const schema = schemas[rawName];
      const name = nameMap.get(rawName);
      if (schema.type === 'object' || schema.properties) {
        const required = new Set(schema.required ?? []);
        const keys = Object.keys(schema.properties ?? {}).sort();
        const body = keys
          .map((key) => {
            const value = schema.properties[key];
            const optional = required.has(key) ? '' : '?';
            const comment = value?.description
              ? ` /** ${String(value.description).replace(/\*\//g, '*\\/')} */`
              : '';
            return `  ${safeKey(key)}${optional}: ${tsType(value, nameMap)};${comment}`;
          })
          .join('\n');
        return `${schemaComment(schema)}export interface ${name} {\n${body}\n}`;
      }
      return `${schemaComment(schema)}export type ${name} = ${tsType(schema, nameMap)};`;
    })
    .join('\n\n');
}

function parameterType(parameter, nameMap) {
  return tsType(parameter.schema, nameMap);
}

function renderOperation(method, path, operation, nameMap) {
  const query = [];
  for (const parameter of operation.parameters ?? []) {
    if (parameter.in !== 'query') continue;
    const optional = parameter.required ? '' : '?';
    query.push(`    ${safeKey(parameter.name)}${optional}: ${parameterType(parameter, nameMap)};`);
  }

  const queryType = query.length > 0 ? `{\n${query.join('\n')}\n  }` : 'never';

  let bodyType = 'never';
  const content = operation.requestBody?.content ?? {};
  const json = content['application/json'];
  if (json?.schema) bodyType = tsType(json.schema, nameMap);

  const responses = operation.responses ?? {};
  const successKey = ['201', '200', '202', '204'].find((key) => responses[key]);
  const responseSchema = successKey ? responses[successKey]?.content?.['application/json']?.schema : undefined;
  const responseType = responseSchema ? tsType(responseSchema, nameMap) : 'unknown';

  return [
    `  ${JSON.stringify(`${method.toUpperCase()} ${path}`)}: {`,
    `    query: ${queryType};`,
    `    body: ${bodyType};`,
    `    response: ${responseType};`,
    '  };',
  ].join('\n');
}

function renderOperations(spec, nameMap) {
  const rows = [];
  for (const path of Object.keys(spec.paths).sort()) {
    for (const method of HTTP_METHODS) {
      const operation = spec.paths[path][method];
      if (!operation) continue;
      rows.push(renderOperation(method, path, operation, nameMap));
    }
  }
  return rows.join('\n');
}

export function generate(sourceText) {
  const spec = JSON.parse(sourceText);
  const schemas = spec.components?.schemas ?? {};
  const nameMap = buildNameMap(schemas);
  const sha256 = createHash('sha256').update(sourceText).digest('hex');
  const operations = renderOperations(spec, nameMap);
  const writeOperations = Object.entries(spec.paths)
    .filter(([, item]) => item.post)
    .map(([path]) => path);

  return `/**
 * 本文件由 \`node scripts/gen-api-types.mjs\` 从 C16 冻结契约生成，请勿手改。
 * 源文件：contracts/api/product_api.openapi.v1.json
 * 契约 sha256：${sha256}
 * 契约版本：${spec.info?.version ?? 'unknown'} · ${spec.info?.['x-contract-id'] ?? 'C16'}
 *
 * 契约变更流程：后端重新生成契约 → 执行 \`npm run gen:api\` → 评审类型差异。
 */

export const CONTRACT_SHA256 = ${JSON.stringify(sha256)};
export const CONTRACT_VERSION = ${JSON.stringify(spec.info?.version ?? 'unknown')};
export const CONTRACT_PATH_COUNT = ${Object.keys(spec.paths).length};

/** 写接口（POST）路径；前端统一补 Idempotency-Key。 */
export const WRITE_PATHS = ${JSON.stringify(writeOperations)} as const;

${renderSchemas(schemas, nameMap)}

/**
 * 契约路径表：键为 \`METHOD /path\`，值给出 query / body / response 类型。
 * 页面与 client 只从这里取返回类型，避免手写类型与契约脱节。
 */
export interface ApiOperations {
${operations}
}
`;
}

function main() {
  const sourceText = readFileSync(CONTRACT_PATH, 'utf8');
  const output = generate(sourceText);
  const check = process.argv.includes('--check');
  if (check) {
    let current = '';
    try {
      current = readFileSync(OUTPUT_PATH, 'utf8');
    } catch {
      console.error(`缺少生成物 ${OUTPUT_PATH}，请执行 npm run gen:api`);
      process.exit(1);
    }
    if (current !== output) {
      console.error('生成物与 C16 契约漂移，请执行 npm run gen:api 后重新评审');
      process.exit(1);
    }
    console.log('前端类型与 C16 契约一致');
    return;
  }
  mkdirSync(dirname(OUTPUT_PATH), { recursive: true });
  writeFileSync(OUTPUT_PATH, output, 'utf8');
  console.log(`已写入 ${OUTPUT_PATH}`);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main();
}
