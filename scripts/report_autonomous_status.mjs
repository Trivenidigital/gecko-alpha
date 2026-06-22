#!/usr/bin/env node
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

function parseArgs(argv) {
  const args = { since: null, out: null };
  for (let i = 2; i < argv.length; i += 1) {
    const token = argv[i];
    if (token === "--since") {
      const value = argv[i + 1];
      if (!value) throw new Error("--since requires a value (ISO 8601)");
      args.since = value;
      i += 1;
      continue;
    }
    if (token === "--out") {
      const value = argv[i + 1];
      if (!value) throw new Error("--out requires a value");
      args.out = value;
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

function runGit(args) {
  return execFileSync("git", args, { encoding: "utf8" }).trim();
}

function isPathWithinRoot(rootAbs, targetAbs) {
  const rel = path.relative(rootAbs, targetAbs);
  if (rel === "") return true;
  if (rel.startsWith("..") || rel.includes(`..${path.sep}`)) return false;
  return !path.isAbsolute(rel);
}

function isTrackedPath(repoRelativePath) {
  try {
    execFileSync("git", ["ls-files", "--error-unmatch", repoRelativePath], {
      stdio: "ignore"
    });
    return true;
  } catch {
    return false;
  }
}

function validateOutPath(repoRootAbs, outArg) {
  if (!outArg) return { ok: true, outAbs: null, outRel: null };

  const outAbs = path.resolve(repoRootAbs, outArg);
  if (!isPathWithinRoot(repoRootAbs, outAbs)) {
    return { ok: false, error: `--out must be within repo root: ${repoRootAbs}` };
  }

  const outRel = path.relative(repoRootAbs, outAbs);
  const tasksPrefix = `tasks${path.sep}`;
  if (!outRel.startsWith(tasksPrefix) || !outRel.endsWith(".md")) {
    return { ok: false, error: "--out must target a Markdown file under tasks/ (e.g. tasks/autonomous_status_report_YYYY_MM_DD.md)" };
  }

  if (isTrackedPath(outRel)) {
    return { ok: false, error: `refusing to overwrite tracked file: ${outRel}` };
  }

  return { ok: true, outAbs, outRel };
}

function safeReadText(filePath) {
  return readFileSync(filePath, "utf8");
}

function findBacklogStatus(backlogText, id) {
  const lines = backlogText.split("\n");
  const indices = [];
  for (let i = 0; i < lines.length; i += 1) {
    if (lines[i].includes(id)) indices.push(i);
  }
  if (indices.length === 0) return null;

  function pickBestIndex() {
    const anchored = indices.filter((idx) => {
      const line = lines[idx];
      if (line.startsWith("###") && line.includes(id)) return true;
      if (line.startsWith("## Active Work:") && line.includes(id)) return true;
      return false;
    });

    const scanList = anchored.length ? anchored : indices;
    for (const idx of scanList) {
      const window = lines.slice(idx, Math.min(lines.length, idx + 40));
      const statusLine =
        window.find((l) => l.startsWith("**Status:**")) ??
        window.find((l) => l.includes("**Status:**"));
      if (statusLine) return { idx, statusLine };
    }
    return { idx: indices[0], statusLine: null };
  }

  const best = pickBestIndex();
  return {
    id,
    found_at_line: best.idx + 1,
    status_line: best.statusLine ?? "(status line not found near any occurrence window)"
  };
}

function checkTemplates() {
  const dir = "docs/superpowers/templates";
  const required = [
    "README.md",
    "implementation_session.md",
    "findings_only_session.md",
    "runtime_state_verification.md",
    "vendor_probe_packet.md",
    "pr_review.md",
    "no_build_decision.md",
    "closeout_report.md"
  ];

  const exists = existsSync(dir);
  const missing = required.filter((f) => !existsSync(path.join(dir, f)));
  return { dir, exists, missing };
}

function searchLoopRunnerHints() {
  const candidates = [
    "scripts",
    "cron",
    "systemd",
    ".github",
    "docs",
    "tasks"
  ];
  const needles = [
    "gecko-overnight-autonomous-closeout",
    "overnight autonomous closeout",
    "autonomous closeout loop"
  ];

  function trackedTextFiles(base) {
    const output = execFileSync(
      "git",
      ["ls-files", base],
      { encoding: "utf8" }
    ).trim();
    if (!output) return [];
    return output
      .split("\n")
      .filter(Boolean)
      .filter((filePath) => {
        if (filePath === "scripts/report_autonomous_status.mjs") return true;
        if (filePath.startsWith("cron/")) return true;
        if (filePath.endsWith(".service") || filePath.endsWith(".timer")) return true;
        return filePath.endsWith(".md") ||
          filePath.endsWith(".txt") ||
          filePath.endsWith(".yml") ||
          filePath.endsWith(".yaml") ||
          filePath.endsWith(".json") ||
          filePath.endsWith(".sh") ||
          filePath.endsWith(".ps1") ||
          filePath.endsWith(".py") ||
          filePath.endsWith(".mjs") ||
          filePath.endsWith(".js");
      });
  }

  function hasLauncherSemantics(filePath, text) {
    if (filePath === "scripts/report_autonomous_status.mjs") return false;
    if (filePath.startsWith("tasks/") && filePath.endsWith(".md")) return false;
    if (filePath.startsWith("docs/") && filePath.endsWith(".md")) return false;

    if (filePath.startsWith("cron/")) {
      const name = path.basename(filePath);
      const maybeCronFile = filePath.endsWith(".crontab") || !name.includes(".");
      const cronScheduleLine = /(^|\n)\s*(?:@\w+|(?:[*0-9,\-/]+\s+){5})\S+/m.test(text);
      return maybeCronFile &&
        text.includes("gecko-overnight-autonomous-closeout") &&
        cronScheduleLine;
    }
    if (filePath.startsWith("systemd/") && (filePath.endsWith(".service") || filePath.endsWith(".timer"))) {
      return text.includes("gecko-overnight-autonomous-closeout");
    }
    if (filePath.startsWith(".github/workflows/") && (filePath.endsWith(".yml") || filePath.endsWith(".yaml"))) {
      return text.includes("gecko-overnight-autonomous-closeout");
    }
    if (filePath.endsWith(".json")) {
      return text.includes("gecko-overnight-autonomous-closeout") && /schedule|cron|automation|runner|command/i.test(text);
    }
    if (filePath.endsWith(".sh") || filePath.endsWith(".ps1") || filePath.endsWith(".py") || filePath.endsWith(".mjs") || filePath.endsWith(".js")) {
      return /closeout|overnight|automation|runner|schedule/i.test(filePath) && /spawn|exec|Start-Process|subprocess|node |python |bash |git /i.test(text);
    }
    return false;
  }

  // Cheap check: only scan known text files in likely repo-local locations.
  const runnerCandidates = [];
  const referenceMentions = [];
  for (const base of candidates) {
    if (!existsSync(base)) continue;
    const list = trackedTextFiles(base);

    for (const filePath of list.slice(0, 1200)) {
      // Bound scan time: skip large files.
      try {
        const text = safeReadText(filePath);
        for (const needle of needles) {
          if (text.includes(needle)) {
            const hit = {
              file: filePath,
              needle,
              kind: filePath === "scripts/report_autonomous_status.mjs"
                ? "reporter-self-reference"
                : hasLauncherSemantics(filePath, text)
                  ? "runner-candidate"
                  : "reference-only"
            };
            if (hit.kind === "runner-candidate") runnerCandidates.push(hit);
            else referenceMentions.push(hit);
            break;
          }
        }
      } catch {
        // ignore unreadable files
      }
      if (runnerCandidates.length + referenceMentions.length >= 40) break;
    }
    if (runnerCandidates.length + referenceMentions.length >= 40) break;
  }

  return { runnerCandidates, referenceMentions };
}

function bestEffortChangesSince(sinceIso) {
  const commits = runGit(["log", `--since=${sinceIso}`, "--pretty=format:%h %ad %s", "--date=iso-strict"]);
  const list = commits.length ? commits.split("\n") : [];

  let beforeCommit = "";
  try {
    beforeCommit = runGit(["rev-list", "-n", "1", `--before=${sinceIso}`, "HEAD"]);
  } catch {
    beforeCommit = "";
  }

  let changedFiles = [];
  if (beforeCommit) {
    const diff = runGit(["diff", "--name-status", `${beforeCommit}..HEAD`]);
    changedFiles = diff.length ? diff.split("\n") : [];
  }

  return { beforeCommit: beforeCommit || null, commits: list, changedFiles };
}

const args = parseArgs(process.argv);
if (args.help) {
  process.stdout.write("Usage: node scripts/report_autonomous_status.mjs [--since <iso>] [--out <path>]\n");
  process.exit(0);
}

const repoRoot = runGit(["rev-parse", "--show-toplevel"]);
process.chdir(repoRoot);

const outCheck = validateOutPath(repoRoot, args.out);
if (!outCheck.ok) {
  process.stderr.write(`ERROR: ${outCheck.error}\n`);
  process.exit(2);
}

const head = runGit(["log", "-1", "--pretty=format:%h %ad %s", "--date=iso-strict"]);
const branch = runGit(["rev-parse", "--abbrev-ref", "HEAD"]);

const backlogPath = "backlog.md";
const todoPath = "tasks/todo.md";
const backlogExists = existsSync(backlogPath);
const todoExists = existsSync(todoPath);

const templateStatus = checkTemplates();

const backlogText = backlogExists ? safeReadText(backlogPath) : "";
const anchors = [
  "BL-NEW-HERMES-CODEX-OPERATING-MODEL",
  "BL-NEW-LIVE-DECISION-COCKPIT",
  "BL-NEW-SIGNAL-TRUST-ROADMAP"
].map((id) => (backlogExists ? findBacklogStatus(backlogText, id) : null));

const loopRunnerHits = searchLoopRunnerHints();

let changes = null;
if (args.since) changes = bestEffortChangesSince(args.since);

const lines = [];
lines.push("# Gecko-Alpha autonomous status (local, read-only)");
lines.push("");
lines.push(`- Repo root: \`${repoRoot}\``);
lines.push(`- Branch: \`${branch}\``);
lines.push(`- HEAD: \`${head}\``);
lines.push("");

lines.push("## Key files present");
lines.push("");
lines.push(`- \`${backlogPath}\`: ${backlogExists ? "present" : "MISSING"}`);
lines.push(`- \`${todoPath}\`: ${todoExists ? "present" : "MISSING"}`);
lines.push("");

lines.push("## Backlog anchors (best-effort)");
lines.push("");
if (!backlogExists) {
  lines.push("- backlog.md missing; cannot extract statuses.");
} else {
  for (const anchor of anchors) {
    if (!anchor) continue;
    lines.push(`- \`${anchor.id}\` @ backlog.md:${anchor.found_at_line} - ${anchor.status_line}`);
  }
}
lines.push("");

lines.push("## Template coverage");
lines.push("");
lines.push(`- \`${templateStatus.dir}\`: ${templateStatus.exists ? "present" : "MISSING"}`);
if (templateStatus.missing.length) {
  lines.push(`- Missing templates: ${templateStatus.missing.map((f) => `\`${f}\``).join(", ")}`);
} else if (templateStatus.exists) {
  lines.push("- All required templates present.");
}
lines.push("");

lines.push("## Closeout work-loop runner (drift-check)");
lines.push("");
lines.push("### Runner candidates");
lines.push("");
if (loopRunnerHits.runnerCandidates.length === 0) {
  lines.push("- No in-tree runner candidates found for `gecko-overnight-autonomous-closeout`.");
  lines.push("- First-run behavior: manual/runbook-driven until a concrete scheduler or launcher artifact is designed, reviewed, and operator-approved.");
} else {
  for (const hit of loopRunnerHits.runnerCandidates) {
    lines.push(`- \`${hit.file}\` (matched: ${hit.needle}; ${hit.kind})`);
  }
}
lines.push("");
lines.push("### Reference-only mentions");
lines.push("");
if (loopRunnerHits.referenceMentions.length === 0) {
  lines.push("- No reference-only mentions found.");
} else {
  for (const hit of loopRunnerHits.referenceMentions) {
    lines.push(`- \`${hit.file}\` (matched: ${hit.needle}; ${hit.kind})`);
  }
}
lines.push("");

if (changes) {
  lines.push("## Changes since `--since`");
  lines.push("");
  lines.push(`- Since: \`${args.since}\``);
  lines.push(`- Commit before since (best-effort): ${changes.beforeCommit ? `\`${changes.beforeCommit}\`` : "(none found)"}`);
  lines.push("");
  if (changes.commits.length === 0) {
    lines.push("- No commits since that timestamp.");
  } else {
    lines.push("- Commits:");
    for (const c of changes.commits.slice(0, 60)) lines.push(`  - \`${c}\``);
    if (changes.commits.length > 60) lines.push(`  - ... (${changes.commits.length - 60} more)`);
  }
  lines.push("");
  if (changes.changedFiles.length) {
    lines.push("- Changed files (best-effort diff):");
    for (const f of changes.changedFiles.slice(0, 120)) lines.push(`  - \`${f}\``);
    if (changes.changedFiles.length > 120) lines.push(`  - ... (${changes.changedFiles.length - 120} more)`);
  }
  lines.push("");
}

lines.push("## Operator-only gates (reminder)");
lines.push("");
lines.push("- Paid APIs/vendor calls, live execution/sizing, pruning/suppression/auto-disable, destructive DB writes/migrations, secrets/external account state require explicit operator approval.");
lines.push("");

const report = lines.join("\n") + "\n";
if (args.out) {
  writeFileSync(outCheck.outAbs, report, "utf8");
}
process.stdout.write(report);
