#!/usr/bin/env node
import { readFileSync } from "node:fs";
import path from "node:path";

function parseArgs(argv) {
  const args = { path: "docs/superpowers/registries/signal_trust_registry.v1.json" };
  for (let i = 2; i < argv.length; i += 1) {
    const token = argv[i];
    if (token === "--path") {
      const value = argv[i + 1];
      if (!value) throw new Error("--path requires a value");
      args.path = value;
      i += 1;
      continue;
    }
    if (token === "--help" || token === "-h") {
      args.help = true;
      continue;
    }
    throw new Error(`unknown arg: ${token}`);
  }
  return args;
}

function fail(errors) {
  for (const error of errors) process.stderr.write(`ERROR: ${error}\n`);
  process.exit(2);
}

function assert(condition, message, errors) {
  if (!condition) errors.push(message);
}

function isPlainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

const args = parseArgs(process.argv);
if (args.help) {
  process.stdout.write("Usage: node scripts/validate_signal_trust_registry.mjs [--path <file>]\n");
  process.exit(0);
}

const filePath = args.path;
const raw = readFileSync(filePath, "utf8");
let doc;
try {
  doc = JSON.parse(raw);
} catch (error) {
  fail([`invalid JSON: ${error.message}`]);
}

const errors = [];
assert(isPlainObject(doc), "top-level must be an object", errors);
assert(doc.schema_version === "signal_trust_registry.v1", "schema_version must be signal_trust_registry.v1", errors);
assert(doc.visibility_only === true, "visibility_only must be true", errors);
assert(doc.not_for_pruning === true, "not_for_pruning must be true", errors);
assert(doc.not_for_auto_disable === true, "not_for_auto_disable must be true", errors);
assert(typeof doc.notes === "string" && doc.notes.length > 0, "notes must be a non-empty string", errors);
assert(Array.isArray(doc.maturity_states), "maturity_states must be an array", errors);
assert(Array.isArray(doc.entries), "entries must be an array", errors);

const maturityStates = new Set(Array.isArray(doc.maturity_states) ? doc.maturity_states : []);
for (const requiredState of ["trusted_experimental", "context_only", "data_insufficient"]) {
  assert(maturityStates.has(requiredState), `maturity_states must include ${requiredState}`, errors);
}

if (Array.isArray(doc.entries)) {
  const seenSignalTypes = new Set();
  doc.entries.forEach((entry, index) => {
    const prefix = `entries[${index}]`;
    assert(isPlainObject(entry), `${prefix} must be an object`, errors);
    if (!isPlainObject(entry)) return;

    assert(typeof entry.signal_type === "string" && entry.signal_type.length > 0, `${prefix}.signal_type must be a non-empty string`, errors);
    if (typeof entry.signal_type === "string" && entry.signal_type.length > 0) {
      if (seenSignalTypes.has(entry.signal_type)) errors.push(`${prefix}.signal_type must be unique (duplicate: ${entry.signal_type})`);
      seenSignalTypes.add(entry.signal_type);
    }
    assert(typeof entry.maturity_state === "string" && maturityStates.has(entry.maturity_state), `${prefix}.maturity_state must be one of maturity_states`, errors);
    assert(isPlainObject(entry.data_quality), `${prefix}.data_quality must be an object`, errors);
    if (isPlainObject(entry.data_quality) && Object.prototype.hasOwnProperty.call(entry.data_quality, "warning")) {
      assert(typeof entry.data_quality.warning === "string" && entry.data_quality.warning.length > 0, `${prefix}.data_quality.warning must be a non-empty string when present`, errors);
    }
    assert(Array.isArray(entry.operator_gate), `${prefix}.operator_gate must be an array`, errors);
    if (Array.isArray(entry.operator_gate)) {
      const gates = new Set(entry.operator_gate);
      for (const requiredGate of ["visibility_only", "not_for_pruning", "not_for_auto_disable"]) {
        assert(gates.has(requiredGate), `${prefix}.operator_gate must include ${requiredGate}`, errors);
      }
    }
    assert(isPlainObject(entry.next_gate), `${prefix}.next_gate must be an object`, errors);
    if (isPlainObject(entry.next_gate)) {
      assert(typeof entry.next_gate.type === "string" && entry.next_gate.type.length > 0, `${prefix}.next_gate.type must be a non-empty string`, errors);
      assert(typeof entry.next_gate.threshold === "string" && entry.next_gate.threshold.length > 0, `${prefix}.next_gate.threshold must be a non-empty string`, errors);
    }
  });
}

if (errors.length > 0) fail(errors);

process.stdout.write(`OK: ${path.normalize(filePath)}\n`);
